"""The recovery ladder (ADR-020): triage -> known fix | repair agent + reflection -> guards -> hard
gates -> blind validator -> done, or a human with an escalation packet.

Everything the ladder decides is deterministic code; the agents only propose. Every step is
written to the run database (recovery_* tables and the audit log), so a scorer can read the
outcome from durable state alone.

A revised call runs as a new child run, `<root>-r<attempt>`, whose plan differs from the root
run only in that call. Plan digests, approvals, hard gates and knowledge ingestion apply to it
unchanged. The validator runs inside the workflow's review hook, before a child run is marked
completed, so nothing a repair produced is ingested before the validator accepts it.
"""
from dataclasses import dataclass
import json
import time

from finquint.autonomous import Call, Request, RetryableError, Source
from finquint.autonomous.models import canonical

from ..gate.decide import GatePolicy, ProposedAction
from ..verify.paths import DEFAULT_PROTECTED_PATHS
from ..models.core import ModelEscalation
from .agents import (default_card, packet_digest, repair_request, sampled, validator_request,
                     verdict_problems)
from .guards import call_fingerprint, check_proposal, flatten, param_diff
from .triage import triage

ESCALATION_ACTION = "recovery_escalation"
ROUTES = ("recovered", "escalated", "blocked", "awaiting_approval")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_runs (
    root_run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, method TEXT NOT NULL, tier TEXT NOT NULL,
    signature TEXT, status TEXT NOT NULL, final_run_id TEXT, candidate_run_id TEXT, reason TEXT NOT NULL,
    packet TEXT NOT NULL, request_id TEXT, action TEXT, spent_usd REAL NOT NULL, model_calls INTEGER NOT NULL,
    created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_attempts (
    root_run_id TEXT NOT NULL, attempt INTEGER NOT NULL, kind TEXT NOT NULL, method TEXT, parameters TEXT,
    reflection TEXT, problems TEXT NOT NULL, child_run_id TEXT, outcome TEXT NOT NULL, failure TEXT,
    fingerprint TEXT, model TEXT, cost_usd REAL NOT NULL, PRIMARY KEY (root_run_id, attempt));
CREATE TABLE IF NOT EXISTS recovery_children (
    child_run_id TEXT PRIMARY KEY, root_run_id TEXT NOT NULL, attempt INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_reviews (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, root_run_id TEXT NOT NULL, kind TEXT NOT NULL,
    verdict TEXT NOT NULL, reasons TEXT NOT NULL, findings TEXT NOT NULL, problems TEXT NOT NULL,
    model TEXT, cost_usd REAL NOT NULL, accepted INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_method_runs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, method TEXT NOT NULL, needed_ladder INTEGER NOT NULL,
    UNIQUE (run_id, method));
CREATE TABLE IF NOT EXISTS recovery_lessons (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, root_run_id TEXT NOT NULL UNIQUE, method TEXT NOT NULL,
    signature TEXT NOT NULL, change TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_fix_proposals (
    method TEXT NOT NULL, signature TEXT NOT NULL, change TEXT NOT NULL, lessons INTEGER NOT NULL,
    PRIMARY KEY (method, signature, change));
CREATE TABLE IF NOT EXISTS recovery_cards (run_id TEXT PRIMARY KEY, card TEXT NOT NULL);
"""


@dataclass(frozen=True)
class LadderOutcome:
    """route is one of ROUTES, or None when triage leaves the run to the host's existing routing."""
    route: str
    root_run_id: str
    reason: str
    tier: str
    final_run_id: str = None
    request_id: str = None
    model_calls: int = 0


def _no_cost(reply):
    return 0.0


def set_path(params, path, value):
    """A copy of params with a dotted path set."""
    out = json.loads(canonical(params))
    node, keys = out, path.split(".")
    for key in keys[:-1]:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise ValueError(f"cannot set {path}: {key} is not an object")
    node[keys[-1]] = value
    return out


def child_request(state, child_run_id, task_id, method, parameters):
    """The root run's request with one call replaced."""
    plan = state["plan"]
    calls = tuple(Call(c["task_id"], method if c["task_id"] == task_id else c["method"],
                       parameters if c["task_id"] == task_id else c["parameters"], tuple(c["dependencies"]))
                  for c in plan["calls"])
    sources = tuple(Source(s["source_id"], s["uri"], s["version"], s["content"]) for s in plan["sources"])
    return Request(child_run_id, plan["objective"], plan["code_version"], calls, sources, plan["max_attempts"])


class RecoveryLadder:
    """workflow: AutonomousWorkflow (its review hook must be this ladder's review()). gate: HumanGate.
    repairer / validator: models.core.ModelCaller, or None (then fixable failures escalate).
    pricing(reply) -> USD for one model reply (default 0)."""

    def __init__(self, db, *, workflow, runs, registry, policy, gate, audit, repairer=None, validator=None,
                 pricing=None, clock=time.time):
        self.db, self.workflow, self.runs, self.registry = db, workflow, runs, registry
        self.policy, self.gate, self.audit = policy, gate, audit
        self.repairer, self.validator = repairer, validator
        self.pricing, self.clock = pricing or _no_cost, clock
        if policy.validator_model is not None and validator is not None and validator.plugs[0].name != policy.validator_model:
            raise ValueError(f"policy requires validator model {policy.validator_model!r}, "
                             f"got {validator.plugs[0].name!r}")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # --- cards --------------------------------------------------------------------------------

    def register_card(self, run_id, card):
        if card.run_id != run_id:
            raise ValueError("the card belongs to another run")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO recovery_cards VALUES (?,?)", (run_id, canonical(card.to_dict())))

    def card_for(self, state):
        from ..context.task_card import AcceptanceCriterion, TaskCard
        row = self.db.execute("SELECT card FROM recovery_cards WHERE run_id=?", (state["plan"]["run_id"],)).fetchone()
        if row is None:
            return default_card(state)
        data = json.loads(row[0])
        return TaskCard(data["run_id"], data["task_id"], data["goal"],
                        tuple(AcceptanceCriterion(c, t, x) for c, t, x in
                              ((c[0], c[2], c[1]) for c in data["criteria"])),
                        data["code_sha"], data["dataset_versions"], tuple(data["constraints"]), data["budget_limits"])

    # --- bookkeeping --------------------------------------------------------------------------

    def _spent(self, root):
        row = self.db.execute("SELECT spent_usd, model_calls FROM recovery_runs WHERE root_run_id=?", (root,)).fetchone()
        return (row[0], row[1]) if row else (0.0, 0)

    def _charge(self, root, result):
        cost = sum(self.pricing(r) for r in result.replies)
        with self.db:
            self.db.execute("UPDATE recovery_runs SET spent_usd=spent_usd+?, model_calls=model_calls+? "
                            "WHERE root_run_id=?", (cost, result.attempts, root))
        return cost

    def _count_failed_call(self, root, caller, exc):
        """A ModelEscalation carries no replies; the core's contract fixes how many were made:
        1 + max_repairs for a schema failure, 1 for a refusal, 0 when no plug could take it."""
        calls = (1 + caller.max_repairs) if exc.errors else (1 if exc.model else 0)
        with self.db:
            self.db.execute("UPDATE recovery_runs SET model_calls=model_calls+? WHERE root_run_id=?", (calls, root))

    def _over_cap(self, spent):
        """Spend has reached the per-escalation cap. Free (unpriced) calls never trip it."""
        return spent > 0 and spent >= self.policy.cost_cap_usd

    def note_run(self, state, failed_method=None):
        """Record one root run per execution method, for the circuit breaker."""
        if self._root_of(state["plan"]["run_id"]) is not None:
            return   # child runs do not count; their root already did
        with self.db:
            for method in sorted({c["method"] for c in state["plan"]["calls"]}):
                self.db.execute("INSERT OR IGNORE INTO recovery_method_runs (run_id, method, needed_ladder) "
                                "VALUES (?,?,?)", (state["plan"]["run_id"], method, int(method == failed_method)))

    def breaker_open(self, method):
        rows = self.db.execute("SELECT needed_ladder FROM recovery_method_runs WHERE method=? ORDER BY seq DESC "
                               "LIMIT ?", (method, self.policy.breaker_window)).fetchall()
        return sum(r[0] for r in rows) >= self.policy.breaker_trips

    def _root_of(self, run_id):
        row = self.db.execute("SELECT root_run_id, attempt, kind FROM recovery_children WHERE child_run_id=?",
                              (run_id,)).fetchone()
        return row

    def outcome(self, root):
        row = self.db.execute("SELECT status, reason, tier, final_run_id, request_id, model_calls FROM recovery_runs "
                              "WHERE root_run_id=?", (root,)).fetchone()
        if row is None:
            return None
        route = {"recovered": "recovered", "awaiting_approval": "awaiting_approval", "human_accepted": "recovered",
                 "human_closed": "blocked", "human_revise": "blocked", "human_rejected": "blocked"}.get(row[0], "escalated")
        return LadderOutcome(route, root, row[1], row[2], row[3], row[4], row[5])

    def attempts(self, root):
        rows = self.db.execute("SELECT attempt, kind, method, parameters, reflection, problems, child_run_id, outcome, "
                               "failure, fingerprint, model, cost_usd FROM recovery_attempts WHERE root_run_id=? "
                               "ORDER BY attempt", (root,)).fetchall()
        keys = ("attempt", "kind", "method", "parameters", "reflection", "problems", "child_run_id", "outcome",
                "failure", "fingerprint", "model", "cost_usd")
        out = []
        for row in rows:
            item = dict(zip(keys, row))
            for key in ("parameters", "reflection", "problems"):
                item[key] = json.loads(item[key]) if item[key] is not None else None
            out.append(item)
        return out

    def _record_attempt(self, root, attempt, kind, *, method, parameters, reflection, problems, child, outcome,
                        failure, fingerprint, model, cost):
        with self.db:
            self.db.execute("INSERT INTO recovery_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (root, attempt, kind, method, None if parameters is None else canonical(parameters),
                             None if reflection is None else canonical(reflection), canonical(list(problems)),
                             child, outcome, failure, fingerprint, model, cost))
            self.audit("recovery_attempt", root, {"attempt": attempt, "kind": kind, "outcome": outcome,
                                                  "child_run_id": child, "problems": list(problems)})

    # --- the ladder ---------------------------------------------------------------------------

    def handle(self, state):
        """Route a finished root run. Idempotent: a run already handled returns its stored outcome."""
        root = state["plan"]["run_id"]
        if self._root_of(root) is not None:
            raise ValueError("handle() takes root runs; child runs are driven by the ladder")
        done = self.outcome(root)
        if done is not None:
            return done
        t = triage(state, self.policy)
        self.note_run(state, t.call["method"] if t.tier in ("fixable", "known_fix") else None)
        if t.tier not in ("fixable", "known_fix"):
            return LadderOutcome(None, root, t.reason, t.tier)
        packet = {"task_id": t.task_id, "objective": state["plan"]["objective"],
                  "original_call": {"method": t.call["method"], "parameters": t.call["parameters"]},
                  "failure": t.failure, "rule": self._rule_dict(t.call["method"]), "attempts": []}
        with self.db:
            self.db.execute("INSERT INTO recovery_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (root, t.task_id, t.call["method"], t.tier, t.signature, "open", None, None, t.reason,
                             canonical(packet), None, None, 0.0, 0, self.clock()))
            self.audit("recovery_started", root, {"tier": t.tier, "signature": t.signature, "task_id": t.task_id})
        if t.tier == "known_fix":
            return self._known_fix(state, t, packet)
        if self.breaker_open(t.call["method"]):
            return self._escalate(root, packet, f"circuit breaker: {t.call['method']} needed recovery in "
                                  f"{self.policy.breaker_trips} of its last {self.policy.breaker_window} runs")
        if self.repairer is None:
            return self._escalate(root, packet, "no repair agent configured")
        return self._repair_loop(state, t, packet)

    def _rule_dict(self, method):
        rule = self.policy.rule(method)
        if rule is None:
            return {"tunable": {}, "frozen": [], "alternatives": []}
        return {"tunable": {p: b.to_dict() for p, b in sorted(rule.tunable.items())}, "frozen": list(rule.frozen),
                "alternatives": list(rule.alternatives)}

    def _run_child(self, state, root, attempt, kind, task_id, method, parameters):
        child_id = f"{root}-r{attempt}"
        with self.db:
            self.db.execute("INSERT INTO recovery_children VALUES (?,?,?,?)", (child_id, root, attempt, kind))
        self.workflow.submit(child_request(state, child_id, task_id, method, parameters))
        return child_id, self.workflow.run(child_id)

    def _known_fix(self, state, t, packet):
        root = state["plan"]["run_id"]
        fix = self.policy.known_fix(t.call["method"], t.signature)
        method = fix.change.get("method", t.call["method"])
        params = t.call["parameters"]
        for path, value in sorted(fix.change.get("parameters", {}).items()):
            params = set_path(params, path, value)
        # Known fixes are human-approved, so only the input contract and invariants are checked here.
        problems = []
        try:
            self.registry.get(method).validate_input(json.loads(canonical(params)))
        except Exception as exc:
            problems.append(f"known fix produces an invalid call: {exc}")
        if GatePolicy(allowed_actions=frozenset({method}),
                      protected_paths=DEFAULT_PROTECTED_PATHS).violates_invariant(
                ProposedAction(method, f"repair:{t.task_id}", params)):
            problems.append("known fix touches a protected or unreadable path")
        if problems:
            self._record_attempt(root, 1, "known_fix", method=method, parameters=params, reflection=None,
                                 problems=problems, child=None, outcome="guard_rejected", failure=None,
                                 fingerprint=call_fingerprint(method, params), model=None, cost=0.0)
            return self._escalate(root, packet, problems[0])
        child, cstate = self._run_child(state, root, 1, "known_fix", t.task_id, method, params)
        outcome = cstate["status"]
        self._record_attempt(root, 1, "known_fix", method=method, parameters=params, reflection=None, problems=(),
                             child=child, outcome=outcome, failure=cstate["tasks"][t.task_id].get("error"),
                             fingerprint=call_fingerprint(method, params), model=None, cost=0.0)
        if outcome == "completed":
            return self._finish(root, "recovered", f"known fix for {t.signature}", final=child)
        packet["attempts"].append({"attempt": 1, "kind": "known_fix", "proposal": {"method": method, "parameters": params},
                                   "outcome": outcome, "failure": cstate["tasks"][t.task_id].get("error") or outcome})
        return self._escalate(root, packet, f"known fix for {t.signature} did not recover the run")

    def _repair_loop(self, state, t, packet):
        root = state["plan"]["run_id"]
        card = self.card_for(state)
        rule = self.policy.rule(t.call["method"])
        earlier = set()
        texts = [t.failure]
        for attempt in range(1, self.policy.max_repair_attempts + 1):
            spent, _ = self._spent(root)
            if self._over_cap(spent):
                return self._escalate(root, packet, f"cost cap ${self.policy.cost_cap_usd:.2f} reached")
            request = repair_request(card, packet, attempt=attempt, max_attempts=self.policy.max_repair_attempts,
                                     budget_left_usd=max(self.policy.cost_cap_usd - spent, 0.0), task_id=t.task_id)
            try:
                result = self.repairer.call(request)
            except ModelEscalation as exc:
                self._count_failed_call(root, self.repairer, exc)
                self._record_attempt(root, attempt, "repair", method=None, parameters=None, reflection=None,
                                     problems=list(exc.errors), child=None, outcome="model_escalated",
                                     failure=exc.reason, fingerprint=None, model=exc.model, cost=0.0)
                packet["attempts"].append({"attempt": attempt, "reflection": None, "proposal": None,
                                           "outcome": "model_escalated", "failure": exc.reason})
                return self._escalate(root, packet, f"repair agent: {exc.reason}")
            except RetryableError as exc:
                return self._escalate(root, packet, f"repair agent unavailable: {exc}")
            cost = self._charge(root, result)
            reply = result.output
            reflection = reply.get("reflection")
            if reply.get("escalate"):
                self._record_attempt(root, attempt, "repair", method=None, parameters=None, reflection=reflection,
                                     problems=(), child=None, outcome="agent_escalated", failure=reply["escalate"],
                                     fingerprint=None, model=result.model, cost=cost)
                packet["attempts"].append({"attempt": attempt, "reflection": reflection, "proposal": None,
                                           "outcome": "agent_escalated", "failure": reply["escalate"]})
                return self._escalate(root, packet, f"repair agent escalated: {reply['escalate']}")
            proposal = reply.get("proposal")
            if not isinstance(proposal, dict):
                guard_problems = ["reply has neither a proposal nor an escalation"]
                guard = None
            else:
                guard = check_proposal(t.call, proposal, reflection, rule=rule, registry=self.registry,
                                       earlier=earlier, packet_text="\n".join(texts))
                guard_problems = list(guard.problems)
            spent_after = self._spent(root)[0]
            if self._over_cap(spent_after):
                guard_problems.append(f"cost cap ${self.policy.cost_cap_usd:.2f} exceeded")
            if guard_problems:
                fp = guard.fingerprint if guard else None
                self._record_attempt(root, attempt, "repair", method=(proposal or {}).get("method"),
                                     parameters=(proposal or {}).get("parameters"), reflection=reflection,
                                     problems=guard_problems, child=None, outcome="guard_rejected", failure=None,
                                     fingerprint=fp, model=result.model, cost=cost)
                if fp:
                    earlier.add(fp)
                feedback = "guard rejected the proposal: " + "; ".join(guard_problems)
                texts.append(feedback)
                packet["attempts"].append({"attempt": attempt, "reflection": reflection, "proposal": proposal,
                                           "outcome": "guard_rejected", "failure": feedback})
                if self._over_cap(spent_after):
                    return self._escalate(root, packet, f"cost cap ${self.policy.cost_cap_usd:.2f} exceeded")
                continue
            earlier.add(guard.fingerprint)
            child, cstate = self._run_child(state, root, attempt, "repair", t.task_id, proposal["method"],
                                            proposal["parameters"])
            task = cstate["tasks"][t.task_id]
            status = cstate["status"]
            failure = task.get("error") if status != "escalated" else cstate.get("error")
            self._record_attempt(root, attempt, "repair", method=proposal["method"], parameters=proposal["parameters"],
                                 reflection=reflection, problems=(), child=child, outcome=status, failure=failure,
                                 fingerprint=guard.fingerprint, model=result.model, cost=cost)
            if status == "completed":
                self._learn(root, t, guard, proposal)
                return self._finish(root, "recovered", f"repaired on attempt {attempt}", final=child)
            if status == "awaiting_approval":
                return self._finish(root, "awaiting_approval", "the revised plan needs a signed approval", final=child)
            packet["attempts"].append({"attempt": attempt, "reflection": reflection, "proposal": proposal,
                                       "outcome": status, "failure": failure or status, "child_run_id": child})
            if status == "escalated":   # the validator did not accept: a human decides (never back to repair)
                return self._escalate(root, packet, f"validator: {cstate.get('error')}", candidate=child)
            other = [k for k, v in cstate["tasks"].items() if v["status"] in ("failed", "needs_revision") and k != t.task_id]
            if other or task["status"] not in ("failed", "needs_revision"):
                return self._escalate(root, packet, f"revised run {child} ended {status} outside the repaired task")
            texts.append(failure or status)
        return self._escalate(root, packet, f"repair budget of {self.policy.max_repair_attempts} attempts spent")

    # --- validator (the workflow's review hook) -----------------------------------------------

    def review(self, state):
        """None to accept, or a reason to escalate. Called by AutonomousWorkflow before completion."""
        run_id = state["plan"]["run_id"]
        child = self._root_of(run_id)
        if child is None:
            if self.validator is None or not sampled(run_id, self.policy.validator_sample_rate):
                return None
            return self._validate(state, run_id, "sample", self._sample_packet(state))
        root, attempt, kind = child
        if kind == "known_fix":
            return None   # human-approved rule; the hard gates have passed
        if self.validator is None:
            return "no validator configured; a repaired result needs one"
        if self._over_cap(self._spent(root)[0]):
            return f"cost cap ${self.policy.cost_cap_usd:.2f} reached before validation"
        return self._validate(state, root, "repair", self._repair_packet(state, root))

    def _sample_packet(self, state):
        return {"mode": "sample", "objective": state["plan"]["objective"],
                "calls": [{"task_id": c["task_id"], "method": c["method"], "parameters": c["parameters"]}
                          for c in state["plan"]["calls"]],
                "outputs": {k: v["output"] for k, v in sorted(state["tasks"].items())},
                "gates": {k: v["gates"] for k, v in sorted(state["tasks"].items())}}

    def _repair_packet(self, state, root):
        row = self.db.execute("SELECT task_id, packet FROM recovery_runs WHERE root_run_id=?", (root,)).fetchone()
        task_id, packet = row[0], json.loads(row[1])
        call = next(c for c in state["plan"]["calls"] if c["task_id"] == task_id)
        original = packet["original_call"]
        changed = list(param_diff(original["parameters"], call["parameters"]))
        if call["method"] != original["method"]:
            changed.append("method")
        task = state["tasks"][task_id]
        # Blind: no reflections, no proposals' reasoning, no earlier attempts.
        return {"mode": "repair", "objective": packet["objective"], "original_call": original,
                "original_failure": packet["failure"],
                "final_call": {"method": call["method"], "parameters": call["parameters"]},
                "changed": sorted(changed), "output": task["output"], "gates": task["gates"]}

    def _validate(self, state, root, kind, review_packet):
        run_id = state["plan"]["run_id"]
        card_state = state if kind == "sample" else self.runs.load(root)
        card = self.card_for(card_state)
        request = validator_request(card, review_packet, request_run_id=root, task_id=run_id)
        try:
            result = self.validator.call(request)
        except ModelEscalation as exc:
            if kind == "repair":
                self._count_failed_call(root, self.validator, exc)
            return self._review_row(run_id, root, kind, "escalate", [f"validator: {exc.reason}"], [], [], None, 0.0)
        except RetryableError as exc:
            return self._review_row(run_id, root, kind, "escalate", [f"validator unavailable: {exc}"], [], [], None, 0.0)
        cost = sum(self.pricing(r) for r in result.replies)
        if kind == "repair":
            self._charge(root, result)
        verdict = result.output
        problems = verdict_problems(verdict, card)
        decision = "escalate" if problems else verdict["verdict"]
        return self._review_row(run_id, root, kind, decision, verdict["reasons"], verdict["criteria"], problems,
                                result.model, cost)

    def _review_row(self, run_id, root, kind, decision, reasons, findings, problems, model, cost):
        accepted = decision == "accept"
        with self.db:
            self.db.execute("INSERT INTO recovery_reviews (run_id, root_run_id, kind, verdict, reasons, findings, "
                            "problems, model, cost_usd, accepted) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (run_id, root, kind, decision, canonical(list(reasons)), canonical(list(findings)),
                             canonical(list(problems)), model, cost, int(accepted)))
            self.audit("recovery_reviewed", run_id, {"kind": kind, "verdict": decision, "problems": list(problems)})
        if accepted:
            return None
        return f"{decision}: " + "; ".join(list(problems) + list(reasons))

    def reviews(self, run_id=None):
        query = "SELECT run_id, root_run_id, kind, verdict, reasons, problems, model FROM recovery_reviews"
        rows = self.db.execute(query + (" WHERE run_id=?" if run_id else "") + " ORDER BY seq",
                               (run_id,) if run_id else ()).fetchall()
        return [{"run_id": r[0], "root_run_id": r[1], "kind": r[2], "verdict": r[3], "reasons": json.loads(r[4]),
                 "problems": json.loads(r[5]), "model": r[6]} for r in rows]

    # --- lessons ------------------------------------------------------------------------------

    def _learn(self, root, t, guard, proposal):
        change = {}
        if guard.swapped:
            change["method"] = proposal["method"]
        values = flatten(proposal["parameters"])
        if guard.diff:
            change["parameters"] = {p: values[p] for p in guard.diff if p in values}
        key = canonical(change)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO recovery_lessons (root_run_id, method, signature, change) "
                            "VALUES (?,?,?,?)", (root, t.call["method"], t.signature, key))
            count = self.db.execute("SELECT COUNT(*) FROM recovery_lessons WHERE method=? AND signature=? AND change=?",
                                    (t.call["method"], t.signature, key)).fetchone()[0]
            if count >= self.policy.lesson_threshold:
                cur = self.db.execute("INSERT OR IGNORE INTO recovery_fix_proposals VALUES (?,?,?,?)",
                                      (t.call["method"], t.signature, key, count))
                if cur.rowcount:
                    self.audit("known_fix_proposed", root, {"method": t.call["method"], "signature": t.signature,
                                                            "change": change, "lessons": count})

    def fix_proposals(self):
        rows = self.db.execute("SELECT method, signature, change, lessons FROM recovery_fix_proposals "
                               "ORDER BY method, signature").fetchall()
        return [{"method": r[0], "signature": r[1], "change": json.loads(r[2]), "lessons": r[3]} for r in rows]

    # --- endings ------------------------------------------------------------------------------

    def _finish(self, root, status, reason, *, final=None):
        with self.db:
            self.db.execute("UPDATE recovery_runs SET status=?, reason=?, final_run_id=? WHERE root_run_id=?",
                            (status, reason, final, root))
            self.audit("recovery_finished", root, {"status": status, "reason": reason, "final_run_id": final})
        return self.outcome(root)

    def escalation_action(self, root, packet, candidate):
        return ProposedAction(ESCALATION_ACTION, f"run:{root}",
                              {"root_run_id": root, "candidate_run_id": candidate or "",
                               "packet_digest": packet_digest(packet)})

    def _escalate(self, root, packet, reason, *, candidate=None):
        """Open a signed approval request carrying the escalation packet. The validator's verdict is
        added for the human; the repair agent never sees it."""
        full = dict(packet, reason=reason, reviews=self.reviews_for_root(root), candidate_run_id=candidate)
        action = self.escalation_action(root, full, candidate)
        request_id = None
        try:
            request_id = self.gate.open(root, "recovery", action, reason=reason[:500], evidence=full).request_id
        except ValueError as exc:   # no approval rule for escalations: still escalated, never silently dropped
            reason = f"{reason} (no approval request: {exc})"
        with self.db:
            self.db.execute("UPDATE recovery_runs SET status='escalated', reason=?, packet=?, request_id=?, action=?, "
                            "candidate_run_id=? WHERE root_run_id=?",
                            (reason, canonical(full), request_id, canonical(action.to_dict()), candidate, root))
            self.audit("recovery_escalated", root, {"reason": reason, "request_id": request_id,
                                                    "candidate_run_id": candidate})
        return self.outcome(root)

    def reviews_for_root(self, root):
        return [r for r in self.reviews() if r["root_run_id"] == root and r["kind"] == "repair"]

    def packet(self, root):
        row = self.db.execute("SELECT packet FROM recovery_runs WHERE root_run_id=?", (root,)).fetchone()
        return None if row is None else json.loads(row[0])

    # --- the human's answer -------------------------------------------------------------------

    def resolve(self, root, approval_record_id):
        """Apply a reviewer's signed decision on an escalation. Approve accepts the candidate run (if
        the validator held one back) or closes the escalation; revise and reject close it too."""
        row = self.db.execute("SELECT status, action, candidate_run_id, request_id FROM recovery_runs "
                              "WHERE root_run_id=?", (root,)).fetchone()
        if row is None or row[0] != "escalated" or row[1] is None:
            raise ValueError("this run has no open recovery escalation")
        record = self.gate.approvals.get_record(approval_record_id)
        if record is None or record.request_id != row[3]:
            raise ValueError("approval record does not belong to this escalation")
        result = self.gate.resume(ProposedAction.from_dict(json.loads(row[1])), {"approval_record_id": approval_record_id})
        route = result.route.value
        if route == "wait":
            return self.outcome(root)
        if route == "execute":
            candidate = row[2]
            if candidate:
                self.workflow.accept_reviewed(candidate, reviewer=record.reviewer,
                                              notes=record.notes or "accepted on recovery escalation")
                return self._finish(root, "human_accepted", f"accepted by {record.reviewer}", final=candidate)
            return self._finish(root, "human_closed", f"closed by {record.reviewer}: {record.notes}")
        status = {"replan": "human_revise", "block": "human_rejected"}.get(route, "escalated")
        return self._finish(root, status, f"{route} by {record.reviewer}: {record.notes or result.reason}")
