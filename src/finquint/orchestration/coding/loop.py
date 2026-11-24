"""The coding loop (ADR-023): coding agent -> host-built change -> scope, static and contract checks ->
novelty -> sandboxed conformance -> blind code review -> PR on agent/<run_id> -> human merge.

CodingService.develop(card) is idempotent per card.run_id. Every proposal, check result, review and
outcome is stored in coding_* tables and the audit log. The agent's code is executed only by the
sandbox until a merge_to_main for its exact diff is recorded as done; load_merged() refuses before.
"""
from dataclasses import dataclass
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time

from finquint import strategies
from finquint.autonomous import RetryableError
from finquint.autonomous.models import canonical, digest
from finquint.strategies.base import StrategyTemplate

from ..gate.decide import ActionStatus, CheckResult, ProposedAction
from ..models.core import ModelEscalation
from ..models.socket import ModelRequest, Segment, SegmentRole
from ..recovery.agents import VALIDATOR_SCHEMA, verdict_problems
from ..tools.ledger import EffectStatus
from .proposal import REGISTRY_PATH, STRATEGIES_DIR, normalize
from .static import check_proposal

CODING_ESCALATION = "coding_escalation"

CODER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "module": {"type": "string", "description": "new module name: lowercase letters, digits, underscores"},
        "source": {"type": "string", "description": "the whole module text"},
        "rationale": {"type": "string"},
        "escalate": {"type": "string", "description": "set only if the card cannot be met by a new template"},
    },
}

CODER_SYSTEM = """You are the coding agent in a governed quant research pipeline. Write one new strategy template module that implements the strategy the task card asks for. Deterministic code checks it, runs it in a sandbox against a protected conformance suite, an independent reviewer reads it, and a human decides whether to merge it.

The module contract (checked by code; a module that breaks a rule is rejected and counts as an attempt):
- Reply with "module" (a new lowercase module name not in the existing list) and "source" (the whole module). You never write any other file: the host registers the template.
- Imports only at module level and only these: {imports}.
- Define one strategy class (no base classes, no decorators) with __init__(self, symbol, <every template parameter as a keyword argument with its default>) and on_bar(self, context). context has timestamp, bars, history (a DataFrame of bars up to now: timestamp, symbol, open, close), cash, positions (symbol -> quantity) and equity. on_bar returns a tuple of MarketOrder(order_id, symbol, quantity, context.timestamp) (quantity is the signed change in position); orders fill at the next bar's open.
- End with TEMPLATE = StrategyTemplate("<template name>", "<one-paragraph description>", {{"<param>": Param("integer"|"number", minimum, maximum, default) or Param("boolean", default=...)}}, <StrategyClass>) using literal values only.
- Decide only from context; keep no state that outlives one strategy instance (no mutable class attributes). The same data must always give the same orders.
- Not allowed: file, network, process or OS access, eval/exec/compile, getattr/setattr, type/id/object/super, print, global/nonlocal, dunder attributes other than __init__, randomness, module-level code other than imports, definitions and literal constants. At most {max_lines} lines.
- If the card cannot be met by a new template on these terms, reply with "escalate" (a reason) and no module.

Reply with only JSON matching the output schema."""

CODE_REVIEW_SYSTEM = """You are the independent reviewer of agent-written strategy code in a governed quant research pipeline. The module has passed deterministic checks (scope, banned constructs, template contract) and a sandboxed conformance suite (determinism, no lookahead, well-formed orders). Decide whether merging it would give the research team the strategy the task card asks for.

Look for code that does something other than the card asks (for example a renamed copy of an existing template, or a different signal than described), a description that does not match the code, parameters or bounds that make no sense for the strategy, and anything the checks cannot see, such as dates or prices hard-coded to fit one dataset. You see the code and the check results, not the author's reasoning.

Give one finding per acceptance criterion on the card (its ID, whether the code satisfies it, and why). verdict: "accept" only if every criterion is satisfied; "reject" if the code does not implement what the card asks; "escalate" if you cannot tell.

Reply with only JSON matching the output schema."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS coding_runs (
    run_id TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT NOT NULL, module TEXT, template TEXT,
    source TEXT, diff_sha TEXT, branch TEXT, request_id TEXT, model_calls INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS coding_attempts (
    run_id TEXT NOT NULL, attempt INTEGER NOT NULL, module TEXT, source_sha TEXT, stage TEXT NOT NULL,
    problems TEXT NOT NULL, outcome TEXT NOT NULL, sandbox TEXT, model TEXT, PRIMARY KEY (run_id, attempt));
CREATE TABLE IF NOT EXISTS coding_reviews (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, verdict TEXT NOT NULL, reasons TEXT NOT NULL,
    findings TEXT NOT NULL, problems TEXT NOT NULL, model TEXT);
CREATE TABLE IF NOT EXISTS coding_loads (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, diff_sha TEXT NOT NULL, template TEXT NOT NULL);
"""


@dataclass(frozen=True)
class CodingOutcome:
    """route: "pr_opened" (merge waits for a human) or "escalated"."""
    route: str
    run_id: str
    reason: str
    branch: str = None
    diff_sha: str = None
    template: str = None
    model_calls: int = 0


class NotMerged(PermissionError):
    """Agent code may run in the host only after a human merged its exact diff."""


def _source_sha(source):
    return sha256(normalize(source).encode("utf-8")).hexdigest()


def _proposal_key(proposal):
    """Novelty key: the module name and its normalized source together."""
    module, source = proposal.get("module"), proposal.get("source")
    if not isinstance(module, str) or not isinstance(source, str):
        return None
    return sha256(canonical([module, normalize(source)]).encode("utf-8")).hexdigest()


def _passed(*_):
    return ActionStatus(CheckResult.PASSED, 0)   # every check ran before the branch was added


class CodingService:
    """stack: orchestration.host.Stack with open_pr and merge_to_main tools. coder / reviewer:
    ModelCallers (None: agent-written code escalates). sandbox: coding.sandbox.Sandbox. catalog: the
    DatasetCatalog holding policy.conformance_dataset. repo_root: the checkout whose strategies
    package and registration file the change is built against. templates: registered template names
    (default: the package registry)."""

    def __init__(self, stack, *, policy, catalog, sandbox, coder=None, reviewer=None, repo_root=None,
                 templates=None, clock=time.time):
        self.stack, self.policy, self.catalog, self.sandbox = stack, policy, catalog, sandbox
        self.coder, self.reviewer, self.clock = coder, reviewer, clock
        self.repo_root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[4]
        self.templates = strategies.TEMPLATES if templates is None else templates
        self.db, self.audit = stack.db, stack.audit
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # --- what the change is built against ---------------------------------------------------------

    def existing_modules(self):
        return sorted(p.stem for p in (self.repo_root / STRATEGIES_DIR).glob("*.py"))

    def registry_text(self):
        return (self.repo_root / REGISTRY_PATH).read_text(encoding="utf-8")

    def context(self):
        """What the agent sees besides the card: existing modules and templates, the contract limits."""
        return {"existing_modules": self.existing_modules(),
                "existing_templates": [t.describe() for _, t in sorted(self.templates.items())],
                "conformance_dataset": self.policy.conformance_dataset, "max_module_lines": self.policy.max_module_lines}

    # --- state --------------------------------------------------------------------------------------

    def outcome(self, run_id):
        row = self.db.execute("SELECT status, reason, branch, diff_sha, template, model_calls FROM coding_runs "
                              "WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row[0] == "open":
            return None
        return CodingOutcome(row[0], run_id, row[1], row[2], row[3], row[4], row[5])

    def attempts(self, run_id):
        rows = self.db.execute("SELECT attempt, module, source_sha, stage, problems, outcome, sandbox, model "
                               "FROM coding_attempts WHERE run_id=? ORDER BY attempt", (run_id,)).fetchall()
        return [{"attempt": a, "module": m, "source_sha": s, "stage": st, "problems": json.loads(p), "outcome": o,
                 "sandbox": json.loads(sb) if sb else None, "model": model} for a, m, s, st, p, o, sb, model in rows]

    def reviews(self, run_id):
        rows = self.db.execute("SELECT verdict, reasons, problems, model FROM coding_reviews WHERE run_id=? ORDER BY seq",
                               (run_id,)).fetchall()
        return [{"verdict": v, "reasons": json.loads(r), "problems": json.loads(p), "model": m} for v, r, p, m in rows]

    def _calls(self, run_id, n):
        with self.db:
            self.db.execute("UPDATE coding_runs SET model_calls=model_calls+? WHERE run_id=?", (n, run_id))

    def _attempt(self, run_id, attempt, proposal, stage, problems, outcome, model, sandbox=None):
        module = proposal.get("module") if isinstance(proposal, dict) else None
        source = proposal.get("source") if isinstance(proposal, dict) else None
        with self.db:
            self.db.execute("INSERT INTO coding_attempts VALUES (?,?,?,?,?,?,?,?,?)",
                            (run_id, attempt, module if isinstance(module, str) else None,
                             _source_sha(source) if isinstance(source, str) else None, stage,
                             canonical(list(problems)), outcome, None if sandbox is None else canonical(sandbox), model))
            self.audit("coding_attempt", run_id, {"attempt": attempt, "stage": stage, "outcome": outcome,
                                                  "problems": list(problems)})

    # --- the flow -------------------------------------------------------------------------------------

    def develop(self, card):
        run_id = card.run_id
        done = self.outcome(run_id)
        if done is not None:
            return done
        if self.db.execute("SELECT 1 FROM coding_runs WHERE run_id=?", (run_id,)).fetchone() is None:
            with self.db:
                self.db.execute("INSERT INTO coding_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (run_id, "open", "", None, None, None, None, None, None, 0, self.clock()))
                self.audit("coding_started", run_id, {"code_sha": card.code_sha})
        if self.coder is None:
            return self._escalate(run_id, "no coding agent configured", {})
        history, seen = [], {}
        for attempt in range(1, self.policy.max_revision_attempts + 1):
            request = self._coder_request(card, history, attempt)
            try:
                result = self.coder.call(request)
            except ModelEscalation as exc:
                self._calls(run_id, (1 + self.coder.max_repairs) if exc.errors else (1 if exc.model else 0))
                self._attempt(run_id, attempt, None, "model", exc.errors, "model_escalated", exc.model)
                return self._escalate(run_id, f"coding agent: {exc.reason}", {"attempts": history})
            except RetryableError as exc:
                return self._escalate(run_id, f"coding agent unavailable: {exc}", {"attempts": history})
            self._calls(run_id, result.attempts)
            proposal = result.output
            if proposal.get("escalate"):
                self._attempt(run_id, attempt, proposal, "agent", (), "agent_escalated", result.model)
                return self._escalate(run_id, f"coding agent escalated: {proposal['escalate']}", {"attempts": history})
            stage, problems, check, sandbox = self._check(proposal, seen)
            outcome = "passed" if not problems else "rejected"
            self._attempt(run_id, attempt, proposal, stage, problems, outcome, result.model,
                          None if sandbox is None else sandbox.to_dict())
            if _proposal_key(proposal) is not None:
                seen.setdefault(_proposal_key(proposal), attempt)
            if not problems:
                verdict = self._review(card, proposal, check, sandbox)
                if verdict is not None:
                    return self._escalate(run_id, f"code review: {verdict}",
                                          {"module": proposal["module"], "diff_sha": check.change.diff_sha,
                                           "attempts": history})
                return self._open_pr(card, proposal, check)
            history.append({"attempt": attempt, "module": proposal.get("module"), "stage": stage,
                            "problems": list(problems)})
        return self._escalate(run_id, f"no module passed the checks in {self.policy.max_revision_attempts} attempts",
                              {"attempts": history})

    def _check(self, proposal, seen):
        """(stage, problems, StaticCheck, SandboxResult or None). Stages: scope/static/contract, novelty,
        conformance. Nothing runs before every static check has passed."""
        check = check_proposal(proposal, policy=self.policy, existing_modules=self.existing_modules(),
                               existing_templates=self.templates, registry_text=self.registry_text())
        if not check.passed:
            stage = check.problems[0].split(":", 1)[0]
            return stage, check.problems, check, None
        key = _proposal_key(proposal)
        if key in seen:
            return "novelty", (f"novelty: the proposal repeats attempt {seen[key]} unchanged",), check, None
        entry = self.catalog.get(self.policy.conformance_dataset)
        bars = self.catalog.load(entry.ref, "train")
        result = self.sandbox.run(proposal["module"], normalize(proposal["source"]), bars, entry.symbols[0])
        if not result.passed:
            return ("sandbox" if result.status in ("timeout", "crashed") else "conformance"), result.problems, check, result
        return "conformance", (), check, result

    def _coder_request(self, card, history, attempt):
        imports = ", ".join(f"{m}" if names == "*" else f"from {m} import {', '.join(sorted(names))}"
                            for m, names in sorted(self.policy.allowed_imports.items()))
        system = CODER_SYSTEM.format(imports=imports, max_lines=self.policy.max_module_lines)
        segments = [Segment(SegmentRole.SYSTEM, system, stable=True),
                    Segment(SegmentRole.TASK_CARD, card.render(), stable=True, ids=card.criterion_ids),
                    Segment(SegmentRole.HANDOFF, "Catalog (JSON):\n" + json.dumps(self.context(), sort_keys=True, indent=1),
                            stable=True)]
        if history:
            segments.append(Segment(SegmentRole.HANDOFF, "Previous attempts (JSON):\n" + json.dumps(history, sort_keys=True,
                                                                                                    indent=1)))
        segments.append(Segment(SegmentRole.VOLATILE, f"Attempt {attempt} of {self.policy.max_revision_attempts}. "
                                                      "Write the module."))
        return ModelRequest(card.run_id, f"{card.task_id}-code-{attempt}", tuple(segments), CODER_SCHEMA,
                            max_output_tokens=6000)

    def review_request(self, card, proposal, check, sandbox):
        packet = {"mode": "code", "module": proposal["module"], "template": check.spec.describe(),
                  "existing_templates": [t.describe() for _, t in sorted(self.templates.items())],
                  "checks": {"static": "passed",
                             "conformance": {k: v for k, v in sorted((sandbox.tests if sandbox else {}).items())}}}
        return ModelRequest(card.run_id, f"{card.task_id}-code-review", (
            Segment(SegmentRole.SYSTEM, CODE_REVIEW_SYSTEM, stable=True),
            Segment(SegmentRole.TASK_CARD, card.render(), stable=True, ids=card.criterion_ids),
            Segment(SegmentRole.HANDOFF, "Review packet (JSON):\n" + json.dumps(packet, sort_keys=True, indent=1)),
            Segment(SegmentRole.HANDOFF, "Module source:\n" + normalize(proposal["source"])),
            Segment(SegmentRole.VOLATILE, "Mode: code. Give your verdict.")), VALIDATOR_SCHEMA, max_output_tokens=1500)

    def _review(self, card, proposal, check, sandbox):
        """None to accept; otherwise the reason (reject / escalate / no reviewer / outage)."""
        run_id = card.run_id
        if self.reviewer is None:
            return self._review_row(run_id, "escalate", ["no code reviewer configured; agent-written code needs review"],
                                    [], [], None)
        try:
            result = self.reviewer.call(self.review_request(card, proposal, check, sandbox))
        except ModelEscalation as exc:
            self._calls(run_id, (1 + self.reviewer.max_repairs) if exc.errors else (1 if exc.model else 0))
            return self._review_row(run_id, "escalate", [f"reviewer: {exc.reason}"], [], [], exc.model)
        except RetryableError as exc:
            return self._review_row(run_id, "escalate", [f"reviewer unavailable: {exc}"], [], [], None)
        self._calls(run_id, result.attempts)
        verdict = result.output
        problems = verdict_problems(verdict, card)
        decision = "escalate" if problems else verdict["verdict"]
        return self._review_row(run_id, decision, verdict["reasons"], verdict["criteria"], problems, result.model)

    def _review_row(self, run_id, decision, reasons, findings, problems, model):
        with self.db:
            self.db.execute("INSERT INTO coding_reviews (run_id, verdict, reasons, findings, problems, model) "
                            "VALUES (?,?,?,?,?,?)", (run_id, decision, canonical(list(reasons)), canonical(list(findings)),
                                                     canonical(list(problems)), model))
            self.audit("code_reviewed", run_id, {"verdict": decision, "problems": list(problems)})
        return None if decision == "accept" else f"{decision}: " + "; ".join(list(problems) + list(reasons))

    # --- PR path ----------------------------------------------------------------------------------

    def branch_name(self, run_id):
        return f"{self.policy.branch_prefix}{run_id}"

    def _open_pr(self, card, proposal, check):
        run_id, change = card.run_id, check.change
        branch = self.branch_name(run_id)
        params = {"branch": branch, "patch": change.patch, "template": check.spec.name,
                  "title": f"feat: strategy template {check.spec.name} (coding agent, {run_id})"}
        actions = [ProposedAction("open_pr", "repo:main", params, code_sha=card.code_sha, diff_sha=change.diff_sha),
                   ProposedAction("merge_to_main", "repo:main", params, code_sha=card.code_sha, diff_sha=change.diff_sha)]
        scheduler = self.scheduler(run_id)
        try:
            scheduler.branch(branch)
        except KeyError:
            scheduler.add_branch(branch, actions)
        scheduler.run_until_idle()
        with self.db:
            self.db.execute("UPDATE coding_runs SET status='pr_opened', reason=?, module=?, template=?, source=?, "
                            "diff_sha=?, branch=? WHERE run_id=?",
                            ("merge waits for a human", proposal["module"], check.spec.name, normalize(proposal["source"]),
                             change.diff_sha, branch, run_id))
            self.audit("coding_pr_proposed", run_id, {"branch": branch, "diff_sha": change.diff_sha,
                                                      "template": check.spec.name})
        return self.outcome(run_id)

    def scheduler(self, run_id):
        return self.stack.scheduler(run_id, _passed, evidence_for=lambda action: {
            "checks": "scope, static, contract, sandboxed conformance and blind review passed",
            "diff_sha": action.diff_sha})

    def merged(self, run_id):
        """True once merge_to_main for this run's exact diff is done in the ledger."""
        row = self.db.execute("SELECT branch, diff_sha FROM coding_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row[1] is None:
            return False
        branch, diff_sha = row
        return any(e.tool == "merge_to_main" and e.action.get("params", {}).get("branch") == branch
                   and e.action.get("diff_sha") == diff_sha
                   for e in self.stack.ledger.entries(status=EffectStatus.DONE, run_id=run_id))

    def load_merged(self, run_id):
        """The merged template, loaded into this process. Raises NotMerged before a human merged it."""
        if not self.merged(run_id):
            raise NotMerged(f"{run_id}: no completed merge_to_main for this diff; agent code stays in the sandbox")
        module, source, diff_sha = self.db.execute("SELECT module, source, diff_sha FROM coding_runs WHERE run_id=?",
                                                   (run_id,)).fetchone()
        name = f"finquint_merged_{module}_{diff_sha[:12]}"
        if name in sys.modules:
            tpl = sys.modules[name].TEMPLATE
        else:
            with tempfile.TemporaryDirectory(prefix="finquint-merged-") as directory:
                path = Path(directory) / f"{module}.py"
                path.write_text(source, encoding="utf-8")
                spec = importlib.util.spec_from_file_location(name, path)
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
            sys.modules[name] = loaded
            tpl = loaded.TEMPLATE
        if not isinstance(tpl, StrategyTemplate):
            raise TypeError("merged module has no StrategyTemplate")
        with self.db:
            self.db.execute("INSERT INTO coding_loads (run_id, diff_sha, template) VALUES (?,?,?)",
                            (run_id, diff_sha, tpl.name))
            self.audit("coding_template_loaded", run_id, {"diff_sha": diff_sha, "template": tpl.name})
        return tpl

    def _escalate(self, run_id, reason, packet):
        action = ProposedAction(CODING_ESCALATION, f"code:{run_id}", {"run_id": run_id, "packet_digest": digest(packet)})
        request_id = None
        try:
            request_id = self.stack.gate.open(run_id, "coding", action, reason=reason[:500],
                                              evidence=dict(packet, reason=reason)).request_id
        except ValueError as exc:
            reason = f"{reason} (no approval request: {exc})"
        with self.db:
            self.db.execute("UPDATE coding_runs SET status='escalated', reason=?, request_id=? WHERE run_id=?",
                            (reason, request_id, run_id))
            self.audit("coding_escalated", run_id, {"reason": reason, "request_id": request_id})
        return self.outcome(run_id)


__all__ = ["CODER_SCHEMA", "CODER_SYSTEM", "CODE_REVIEW_SYSTEM", "CODING_ESCALATION", "CodingOutcome", "CodingService",
           "NotMerged"]
