"""The planning layer (ADR-022): template match -> planner agent -> plan checks -> blind plan review
-> run on the existing path; or a human.

PlanningService.plan_and_run(card, objective_key=None, evidence=()) is idempotent per card.run_id.
Every proposal, check result, review and outcome is stored in planning_* tables and the audit log.
"""
from dataclasses import dataclass
import json
import time

from finquint.autonomous import RetryableError
from finquint.autonomous.models import canonical, digest
from finquint import strategies

from ..context.tags import evidence_line
from ..gate.decide import ProposedAction
from ..models.core import ModelEscalation
from ..models.socket import ModelRequest, Segment, SegmentRole
from ..recovery.agents import VALIDATOR_SCHEMA, verdict_problems
from ..verify.plans import check_plan

PLAN_ESCALATION = "plan_escalation"
TRUSTED_TIERS = ("authoritative", "verified")

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "calls": {"type": "array", "items": {
            "type": "object", "required": ["task_id", "method", "parameters", "dependencies", "criteria", "rationale"],
            "additionalProperties": False,
            "properties": {"task_id": {"type": "string", "minLength": 1}, "method": {"type": "string", "minLength": 1},
                           "parameters": {"type": "object"},
                           "dependencies": {"type": "array", "items": {"type": "string"}},
                           "criteria": {"type": "array", "items": {"type": "string"}},
                           "rationale": {"type": "string", "minLength": 1}}}},
        "escalate": {"type": "string", "description": "set only if the card cannot be met from the catalog"},
    },
}

METHOD_DOCS = {
    "source_review": {"role": "research", "parameters": {"source_ids": "list of dataset profile source_ids to review"},
                      "note": "Every plan needs this read-only research step over the profiles of the datasets it uses."},
    "parameter_search": {"role": "execution", "parameters": {
        "dataset": "pinned id@version from the card", "strategy": "a strategy template name",
        "window": "\"train\" only", "grid": "{param: [values]} within the template's bounds, at most 50 combinations",
        "objective": "\"sharpe_ratio\" (default) or \"total_return\"", "cost_bps": "optional, 0-50"},
        "output": "best_params, best_score, scores"},
    "backtest": {"role": "execution", "parameters": {
        "dataset": "pinned id@version", "strategy": "template name", "window": "\"train\" or \"test\"",
        "params": "template parameters, or instead", "params_from": "task_id of a parameter_search this call depends on",
        "cost_bps": "optional, 0-50"}, "output": "returns, total_return, fills, bars"},
    "performance_summary": {"role": "execution", "parameters": {
        "source_task": "task_id of a backtest this call depends on", "periods_per_year": "optional integer, default 252"},
        "output": "total_return, annualized_return, annualized_volatility, sharpe_ratio, maximum_drawdown, ..."},
}

PLANNER_SYSTEM = """You are the planner in a governed quant research pipeline. Turn the task card into a plan: a small graph of calls to registered methods on pinned datasets. Deterministic code checks the plan before anything runs; a plan that breaks a rule is rejected and counts as an attempt.

Rules:
- Use only the methods, strategy templates and datasets in the catalog. Use a dataset only at the version pinned on the card.
- Include one source_review call over the profile source_ids of the datasets you use.
- Tune (parameter_search) on the train window only. Evaluate out of sample with a backtest on the test window that takes its parameters from the search (params_from) and depends on it. Never tune after a test-window call.
- params_from and source_task must name a direct dependency.
- Every acceptance criterion on the card must be served by at least one call (criteria: list of criterion IDs), and every call must serve at least one.
- Stay within the call limit and the cost budget stated below. Parameter values never name files or paths.
- If the card cannot be met from the catalog, reply with "escalate" (a reason) and no calls.

Reply with only JSON matching the output schema."""

PLAN_REVIEW_SYSTEM = """You are the independent reviewer of research plans in a governed quant research pipeline. The plan has passed deterministic checks (allowed methods, pinned data, train/test discipline, coverage, budget). Decide before it runs whether executing it would answer the task as stated on the task card.

Look for plans that answer a different question: a different dataset, horizon, window or metric than the card asks for; evaluation in-sample when the card asks for out of sample; criteria served only nominally. You see the calls but not the planner's reasoning.

Give one finding per acceptance criterion on the card (its ID, whether the plan would satisfy it, and why). verdict: "accept" only if every criterion would be satisfied; "reject" if the plan answers a different question; "escalate" if you cannot tell.

Reply with only JSON matching the output schema."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS planning_runs (
    run_id TEXT PRIMARY KEY, objective_key TEXT, source TEXT, status TEXT NOT NULL, reason TEXT NOT NULL,
    plan TEXT, request_id TEXT, run_route TEXT, model_calls INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS planning_attempts (
    run_id TEXT NOT NULL, attempt INTEGER NOT NULL, proposal TEXT, problems TEXT NOT NULL, outcome TEXT NOT NULL,
    model TEXT, PRIMARY KEY (run_id, attempt));
CREATE TABLE IF NOT EXISTS planning_reviews (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, verdict TEXT NOT NULL, reasons TEXT NOT NULL,
    findings TEXT NOT NULL, problems TEXT NOT NULL, model TEXT);
CREATE TABLE IF NOT EXISTS planning_template_proposals (
    objective_key TEXT NOT NULL, shape TEXT NOT NULL, plans INTEGER NOT NULL, PRIMARY KEY (objective_key, shape));
CREATE TABLE IF NOT EXISTS planning_shapes (
    run_id TEXT PRIMARY KEY, objective_key TEXT NOT NULL, shape TEXT NOT NULL);
"""


@dataclass(frozen=True)
class PlanOutcome:
    """route: "ran" (the plan ran; run_route says how the run ended) or "escalated"."""
    route: str
    run_id: str
    reason: str
    source: str = None          # "template" or "planner"
    run_route: str = None
    model_calls: int = 0
    final_run_id: str = None


def catalog_view(card, catalog, policy, templates=None):
    """What the planner may use: card-pinned datasets, templates, methods and limits. templates:
    name -> StrategyTemplate (default: the package registry)."""
    templates = strategies.TEMPLATES if templates is None else templates
    pinned = dict(card.dataset_versions)
    datasets = [e.describe() for e in catalog.entries() if pinned.get(e.dataset_id) == e.version]
    return {"datasets": datasets, "strategies": [t.describe() for _, t in sorted(templates.items())],
            "methods": {m: METHOD_DOCS[m] for m in sorted(policy.planner_methods) if m in METHOD_DOCS},
            "limits": {"max_plan_calls": policy.max_plan_calls,
                       "cost_budget": dict(card.budget_limits).get("plan_cost", policy.default_plan_budget),
                       "method_costs": dict(policy.method_costs),
                       "parameter_search_cost": "method cost x grid combinations"}}


def trusted_evidence(evidence):
    """Only verified evidence reaches the planner (settled 2026-11-23)."""
    return [e for e in evidence if e.tier in TRUSTED_TIERS and not e.deprecated]


def planner_request(card, view, evidence, attempts, *, attempt, max_attempts):
    segments = [Segment(SegmentRole.SYSTEM, PLANNER_SYSTEM, stable=True),
                Segment(SegmentRole.TASK_CARD, card.render(), stable=True, ids=card.criterion_ids),
                Segment(SegmentRole.HANDOFF, "Catalog (JSON):\n" + json.dumps(view, sort_keys=True, indent=1), stable=True)]
    if evidence:
        lines = [evidence_line(e.evidence_id, e.version, e.run_id, e.topic, e.content, e.dataset) for e in evidence]
        segments.append(Segment(SegmentRole.EVIDENCE, "Verified evidence:\n" + "\n".join(lines),
                                ids=tuple(e.evidence_id for e in evidence)))
    if attempts:
        segments.append(Segment(SegmentRole.HANDOFF, "Previous attempts (JSON):\n" + json.dumps(attempts, sort_keys=True, indent=1)))
    segments.append(Segment(SegmentRole.VOLATILE, f"Plan attempt {attempt} of {max_attempts}. Propose the plan."))
    return ModelRequest(card.run_id, f"{card.task_id}-plan-{attempt}", tuple(segments), PLAN_SCHEMA, max_output_tokens=2048)


def review_request(card, view, proposal):
    calls = [{k: v for k, v in c.items() if k != "rationale"} for c in proposal["calls"]]   # blind to reasoning
    packet = {"mode": "plan", "catalog": {"datasets": view["datasets"], "strategies": view["strategies"]}, "calls": calls}
    return ModelRequest(card.run_id, f"{card.task_id}-plan-review", (
        Segment(SegmentRole.SYSTEM, PLAN_REVIEW_SYSTEM, stable=True),
        Segment(SegmentRole.TASK_CARD, card.render(), stable=True, ids=card.criterion_ids),
        Segment(SegmentRole.HANDOFF, "Review packet (JSON):\n" + json.dumps(packet, sort_keys=True, indent=1)),
        Segment(SegmentRole.VOLATILE, "Mode: plan. Give your verdict.")), VALIDATOR_SCHEMA, max_output_tokens=1024)


def fill_template(item, card):
    """A human-approved template: "{dataset}" becomes the card's single pinned dataset; criteria "all"."""
    pinned = sorted(dict(card.dataset_versions).items())
    if len(pinned) != 1:
        raise ValueError("a plan template needs exactly one dataset pinned on the card")
    ref = f"{pinned[0][0]}@{pinned[0][1]}"
    text = canonical(item["calls"]).replace("{dataset}", ref)
    calls = json.loads(text)
    for call in calls:
        if call.get("criteria") == "all":
            call["criteria"] = list(card.criterion_ids)
        call.setdefault("rationale", "human-approved template")
    return {"calls": calls}


def plan_shape(proposal, card):
    """The plan with the card-specific parts abstracted, for template proposals."""
    pinned = {f"{k}@{v}" for k, v in dict(card.dataset_versions).items()}
    calls = []
    for c in proposal["calls"]:
        params = json.loads(canonical(c["parameters"]))
        if params.get("dataset") in pinned:
            params["dataset"] = "{dataset}"
        criteria = "all" if sorted(c["criteria"]) == sorted(card.criterion_ids) else c["criteria"]
        calls.append({"task_id": c["task_id"], "method": c["method"], "parameters": params,
                      "dependencies": c.get("dependencies", []), "criteria": criteria})
    return canonical(calls)


class PlanningService:
    """stack: orchestration.host.Stack (its registry must be planning_registry(catalog)). planner /
    reviewer: ModelCallers (None: agent-written plans escalate). pricing(reply) -> USD (unused for
    limits here; recorded via the stack's spend where relevant). templates: the strategy templates
    the planner is shown (default: the package registry); pass the same table to planning_registry."""

    def __init__(self, stack, *, catalog, policy, planner=None, reviewer=None, clock=time.time, templates=None):
        self.stack, self.catalog, self.policy, self.templates = stack, catalog, policy, templates
        self.planner, self.reviewer, self.clock = planner, reviewer, clock
        self.db, self.audit = stack.db, stack.audit
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # --- state ----------------------------------------------------------------------------------

    def outcome(self, run_id):
        row = self.db.execute("SELECT status, reason, source, run_route, model_calls FROM planning_runs WHERE run_id=?",
                              (run_id,)).fetchone()
        if row is None or row[0] == "open":
            return None
        return PlanOutcome("ran" if row[0] == "ran" else "escalated", run_id, row[1], row[2], row[3], row[4])

    def attempts(self, run_id):
        rows = self.db.execute("SELECT attempt, proposal, problems, outcome, model FROM planning_attempts WHERE run_id=? "
                               "ORDER BY attempt", (run_id,)).fetchall()
        return [{"attempt": a, "proposal": json.loads(p) if p else None, "problems": json.loads(pr), "outcome": o,
                 "model": m} for a, p, pr, o, m in rows]

    def reviews(self, run_id):
        rows = self.db.execute("SELECT verdict, reasons, problems, model FROM planning_reviews WHERE run_id=? ORDER BY seq",
                               (run_id,)).fetchall()
        return [{"verdict": v, "reasons": json.loads(r), "problems": json.loads(p), "model": m} for v, r, p, m in rows]

    def template_proposals(self):
        return [{"objective_key": k, "calls": json.loads(s), "plans": n} for k, s, n in self.db.execute(
            "SELECT objective_key, shape, plans FROM planning_template_proposals ORDER BY objective_key")]

    def _calls(self, run_id, n):
        with self.db:
            self.db.execute("UPDATE planning_runs SET model_calls=model_calls+? WHERE run_id=?", (n, run_id))

    def _attempt(self, run_id, attempt, proposal, problems, outcome, model):
        with self.db:
            self.db.execute("INSERT INTO planning_attempts VALUES (?,?,?,?,?,?)",
                            (run_id, attempt, None if proposal is None else canonical(proposal),
                             canonical(list(problems)), outcome, model))
            self.audit("plan_attempt", run_id, {"attempt": attempt, "outcome": outcome, "problems": list(problems)})

    # --- the flow -------------------------------------------------------------------------------

    def plan_and_run(self, card, *, objective_key=None, evidence=()):
        run_id = card.run_id
        done = self.outcome(run_id)
        if done is not None:
            return done
        if self.db.execute("SELECT 1 FROM planning_runs WHERE run_id=?", (run_id,)).fetchone() is None:
            with self.db:
                self.db.execute("INSERT INTO planning_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (run_id, objective_key, None, "open", "", None, None, None, 0, self.clock()))
                self.audit("plan_started", run_id, {"objective_key": objective_key})
        item = self.policy.template(objective_key)
        if item is not None:
            try:
                proposal = fill_template(item, card)
            except ValueError as exc:
                return self._escalate(run_id, f"template {objective_key}: {exc}", {})
            check = check_plan(proposal, card=card, catalog=self.catalog, registry=self.stack.registry, policy=self.policy)
            self._attempt(run_id, 1, proposal, check.problems, "template" if check.passed else "rejected", None)
            if not check.passed:
                return self._escalate(run_id, f"template {objective_key} failed the plan checks", {"problems": list(check.problems)})
            return self._run(card, check, proposal, "template", objective_key)
        if self.planner is None:
            return self._escalate(run_id, "no planner agent configured and no template matches", {})
        view = catalog_view(card, self.catalog, self.policy, self.templates)
        evidence = trusted_evidence(evidence)
        history = []
        for attempt in range(1, self.policy.max_plan_attempts + 1):
            request = planner_request(card, view, evidence, history, attempt=attempt,
                                      max_attempts=self.policy.max_plan_attempts)
            try:
                result = self.planner.call(request)
            except ModelEscalation as exc:
                self._calls(run_id, (1 + self.planner.max_repairs) if exc.errors else (1 if exc.model else 0))
                self._attempt(run_id, attempt, None, exc.errors, "model_escalated", exc.model)
                return self._escalate(run_id, f"planner: {exc.reason}", {"attempts": history})
            except RetryableError as exc:
                return self._escalate(run_id, f"planner unavailable: {exc}", {"attempts": history})
            self._calls(run_id, result.attempts)
            proposal = result.output
            if proposal.get("escalate"):
                self._attempt(run_id, attempt, proposal, (), "agent_escalated", result.model)
                return self._escalate(run_id, f"planner escalated: {proposal['escalate']}", {"attempts": history})
            check = check_plan(proposal, card=card, catalog=self.catalog, registry=self.stack.registry, policy=self.policy)
            self._attempt(run_id, attempt, proposal, check.problems, "passed" if check.passed else "rejected", result.model)
            if check.passed:
                verdict = self._review(card, view, proposal)
                if verdict is not None:
                    return self._escalate(run_id, f"plan review: {verdict}", {"plan": proposal, "attempts": history})
                return self._run(card, check, proposal, "planner", objective_key)
            history.append({"attempt": attempt, "calls": proposal.get("calls"), "problems": list(check.problems)})
        return self._escalate(run_id, f"no plan passed the checks in {self.policy.max_plan_attempts} attempts",
                              {"attempts": history})

    def _review(self, card, view, proposal):
        """None to accept; otherwise the reason (reject / escalate / no reviewer / outage)."""
        if self.reviewer is None:
            return "no plan reviewer configured; an agent-written plan needs review"
        try:
            result = self.reviewer.call(review_request(card, view, proposal))
        except ModelEscalation as exc:
            self._calls(card.run_id, (1 + self.reviewer.max_repairs) if exc.errors else (1 if exc.model else 0))
            return self._review_row(card.run_id, "escalate", [f"reviewer: {exc.reason}"], [], [], exc.model)
        except RetryableError as exc:
            return self._review_row(card.run_id, "escalate", [f"reviewer unavailable: {exc}"], [], [], None)
        self._calls(card.run_id, result.attempts)
        verdict = result.output
        problems = verdict_problems(verdict, card)
        decision = "escalate" if problems else verdict["verdict"]
        return self._review_row(card.run_id, decision, verdict["reasons"], verdict["criteria"], problems, result.model)

    def _review_row(self, run_id, decision, reasons, findings, problems, model):
        with self.db:
            self.db.execute("INSERT INTO planning_reviews (run_id, verdict, reasons, findings, problems, model) "
                            "VALUES (?,?,?,?,?,?)", (run_id, decision, canonical(list(reasons)), canonical(list(findings)),
                                                     canonical(list(problems)), model))
            self.audit("plan_reviewed", run_id, {"verdict": decision, "problems": list(problems)})
        return None if decision == "accept" else f"{decision}: " + "; ".join(list(problems) + list(reasons))

    def _run(self, card, check, proposal, source, objective_key):
        run_id = card.run_id
        outcome = self.stack.run_research(check.request, card=card)
        with self.db:
            self.db.execute("UPDATE planning_runs SET status='ran', source=?, reason=?, plan=?, run_route=? WHERE run_id=?",
                            (source, f"ran from {source}", canonical(proposal), outcome.route, run_id))
            self.audit("plan_ran", run_id, {"source": source, "route": outcome.route, "plan_digest": digest(proposal)})
        if source == "planner" and objective_key and outcome.route in ("completed", "recovered"):
            self._learn(run_id, objective_key, plan_shape(proposal, card))
        done = self.outcome(run_id)
        return PlanOutcome(done.route, run_id, done.reason, source, outcome.route, done.model_calls, outcome.final_run_id)

    def _learn(self, run_id, key, shape):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO planning_shapes VALUES (?,?,?)", (run_id, key, shape))
            count = self.db.execute("SELECT COUNT(*) FROM planning_shapes WHERE objective_key=? AND shape=?",
                                    (key, shape)).fetchone()[0]
            if count >= self.policy.template_threshold:
                cur = self.db.execute("INSERT OR IGNORE INTO planning_template_proposals VALUES (?,?,?)", (key, shape, count))
                if cur.rowcount:
                    self.audit("plan_template_proposed", run_id, {"objective_key": key, "plans": count})

    def _escalate(self, run_id, reason, packet):
        action = ProposedAction(PLAN_ESCALATION, f"plan:{run_id}", {"run_id": run_id, "packet_digest": digest(packet)})
        request_id = None
        try:
            request_id = self.stack.gate.open(run_id, "planning", action, reason=reason[:500],
                                              evidence=dict(packet, reason=reason)).request_id
        except ValueError as exc:
            reason = f"{reason} (no approval request: {exc})"
        with self.db:
            self.db.execute("UPDATE planning_runs SET status='escalated', reason=?, request_id=? WHERE run_id=?",
                            (reason, request_id, run_id))
            self.audit("plan_escalated", run_id, {"reason": reason, "request_id": request_id})
        return self.outcome(run_id)
