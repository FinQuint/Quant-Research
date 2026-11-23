"""Stack: every orchestration layer wired together on one run database (roadmap Phase 7).

Phases 1-6 each added an opt-in piece. A trusted host process builds them like this:

    ResearchStore (SQLite)        the run database and its append-only audit log
    CapabilityAuthority           tokens for side effects (capability key)
    ApprovalService + HumanGate   signed approvals (approval key, versioned policy file)
    SQLiteLedger                  idempotency ledger + outbox
    GuardedTools + OutboxWorker   the only path to a side effect
    AutonomousWorkflow            research runs, with the human gate, tracer and knowledge hook
    Scheduler                     side-effect branches (open a PR, merge), one per run_id
    SQLiteEvidenceStore           evidence; Ingestor re-indexes on explicit events
    MetricStore                   run outputs, verified experiments, current trusted evidence
    write_report                  reports through the model socket, every number a placeholder
    ReportStore                   final reports and escalated attempts, for audit
    RecoveryLadder (opt-in)       triage, repair agent, guards, blind validator, escalation (Phase 8)

The Stack holds no state of its own beyond the database: a crashed host is replaced by a new
Stack over the same directory, then recover() settles the outbox before anything else runs.

Keys are passed in by the host and never reach an agent. Agents only propose: requests,
branches of ProposedActions, and model replies; everything else here is deterministic code.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import time

from finquint.autonomous import AutonomousWorkflow, RunStore
from finquint.research import ResearchStore

from .adapters import action_for_call, ledger_for, status_for_task
from .context.evidence import SQLiteEvidenceStore
from .gate.approvals import ApprovalPolicy, ApprovalService
from .gate.capabilities import CapabilityAuthority
from .gate.decide import GateDecision, GatePolicy, decide, default_policy
from .gate.human_gate import HumanGate
from .knowledge.evidence_store import promote
from .knowledge.ingestion import Ingestor
from .knowledge.metrics import CompositeMetricStore, EvidenceMetrics, ExperimentMetrics, RunOutputMetrics
from .knowledge.reporting import ReportResult, metric_segment, request_identifiers, write_report
from .scheduler import Scheduler
from .tools.guarded import GuardedTools, OutboxWorker
from .recovery.ladder import RecoveryLadder
from .recovery.policy import RecoveryPolicy
from .tools.registry import ToolRegistry
from .tracing import SQLiteSpanStore, Tracer

RUN_ROUTES = ("completed", "awaiting_approval", "escalated", "blocked", "needs_revision", "recovered")


@dataclass(frozen=True)
class RunOutcome:
    """A research run after the host has routed it. route is one of RUN_ROUTES."""
    run_id: str
    route: str
    state: dict
    reason: str = ""
    final_run_id: str = None   # the linked run that recovered (or awaits approval), when the ladder ran


class ReportStore:
    """Every report attempt the host made: final reports (status "ok") and escalations.

    The rendered numbers and the identifiers that were exempt are stored with the text, so an
    auditor can re-run the numeral check on the stored report alone.
    """

    def __init__(self, connection):
        self.db = connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS host_reports (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL,
            status TEXT NOT NULL, text TEXT NOT NULL, numbers TEXT NOT NULL, identifiers TEXT NOT NULL,
            problems TEXT NOT NULL, model TEXT, model_calls INTEGER NOT NULL)""")
        self.db.commit()

    def save(self, report_id, run_id, result, identifiers, audit=None):
        numbers = [{"start": n.start, "end": n.end, "artifact_id": n.artifact_id, "field": n.field,
                    "value": n.value, "text": n.text} for n in result.numbers]
        with self.db:
            self.db.execute("INSERT INTO host_reports (report_id, run_id, status, text, numbers, identifiers, "
                            "problems, model, model_calls) VALUES (?,?,?,?,?,?,?,?,?)",
                            (report_id, run_id, result.status, result.text, json.dumps(numbers, sort_keys=True),
                             json.dumps(sorted(identifiers)), json.dumps(list(result.problems)), result.model,
                             result.model_calls))
            if audit is not None:
                audit("report_written" if result.status == "ok" else "report_escalated", report_id,
                      {"run_id": run_id, "numbers": len(numbers), "problems": list(result.problems)})

    def all(self):
        rows = self.db.execute("SELECT report_id, run_id, status, text, numbers, identifiers, problems, model, "
                               "model_calls FROM host_reports ORDER BY seq").fetchall()
        return [{"report_id": r[0], "run_id": r[1], "status": r[2], "text": r[3], "numbers": json.loads(r[4]),
                 "identifiers": json.loads(r[5]), "problems": json.loads(r[6]), "model": r[7],
                 "model_calls": r[8]} for r in rows]


class Stack:
    """approval_policy: ApprovalPolicy, or a path to a policy file (configs/approval_policy.json).
    tools: Tool definitions for side-effect branches. registry: MethodRegistry for research runs.
    reporter: a models.core.ModelCaller used by report(); None if this host writes no reports.
    tracer_factory(stack) -> Tracer, or None for no tracing; spans are saved on close().
    recovery_policy: RecoveryPolicy or a path (configs/recovery_policy.json) to switch on the Phase 8
    recovery ladder; repairer / validator are ModelCallers for its agents; pricing(reply) -> USD.
    Without recovery_policy, behavior is unchanged.
    """

    def __init__(self, directory, *, approval_key, capability_key, approval_policy, registry, tools=(),
                 gate_policy=None, clock=time.time, reporter=None, code_root=None, capability_ttl=600,
                 max_workers=1, tracer_factory=None, recovery_policy=None, repairer=None, validator=None,
                 pricing=None):
        if approval_key == capability_key:
            raise ValueError("the approval key and the capability key must differ")
        if not isinstance(approval_policy, ApprovalPolicy):
            approval_policy = ApprovalPolicy.from_file(approval_policy)
        self.directory = Path(directory)
        self.research = ResearchStore(self.directory / "research.sqlite")
        self.db = self.research.connection
        self.clock = clock
        self.audit = self.research._event
        self.tracer = tracer_factory(self) if tracer_factory else None
        self.authority = CapabilityAuthority(capability_key, ttl_seconds=capability_ttl, clock=clock)
        self.approvals = ApprovalService(self.db, secret=approval_key, policy=approval_policy,
                                         authority=self.authority, clock=clock, audit=self.audit)
        self.gate = HumanGate(self.approvals)
        self.gate_policy = gate_policy or default_policy()
        self.ledger = ledger_for(self.research)
        self.tool_registry = ToolRegistry()
        for tool in tools:
            self.tool_registry.register(tool)
        self.tools = GuardedTools(self.tool_registry, self.ledger, self.authority, tracer=self.tracer)
        self.worker = OutboxWorker(self.tool_registry, self.ledger, tracer=self.tracer)
        self.runs = RunStore(self.research)
        self.evidence = SQLiteEvidenceStore(self.db)
        self.ingestor = Ingestor(self.evidence, self.db, code_root=code_root)
        self.registry = registry
        self.workflow = AutonomousWorkflow(self.runs, registry, max_workers=max_workers, approvals=self.gate,
                                           tracer=self.tracer, knowledge=self.ingestor)
        self.metrics = CompositeMetricStore(RunOutputMetrics(self.runs), ExperimentMetrics(self.research),
                                            EvidenceMetrics(self.evidence))
        self.reporter = reporter
        self.reports = ReportStore(self.db)
        self._schedulers = {}
        self.ladder = None
        if recovery_policy is not None:
            if not isinstance(recovery_policy, RecoveryPolicy):
                recovery_policy = RecoveryPolicy.from_file(recovery_policy)
            self.ladder = RecoveryLadder(self.db, workflow=self.workflow, runs=self.runs, registry=registry,
                                         policy=recovery_policy, gate=self.gate, audit=self.audit, repairer=repairer,
                                         validator=validator, pricing=pricing, clock=clock)
            self.workflow.review = self.ladder.review

    # --- side-effect branches -----------------------------------------------------------------

    def scheduler(self, run_id, status_for, *, preconditions=None, evidence_for=None):
        """One Scheduler per run_id; its branches are persisted, so a new Stack picks them up."""
        if run_id not in self._schedulers:
            self._schedulers[run_id] = Scheduler(
                self.db, run_id=run_id, policy=self.gate_policy, tools=self.tools, authority=self.authority,
                gate=self.gate, status_for=status_for, preconditions=preconditions, evidence_for=evidence_for,
                audit=self.audit, tracer=self.tracer)
        return self._schedulers[run_id]

    def recover(self):
        """Run once after a crash, before any worker dispatches: settle interrupted dispatches."""
        return self.worker.recover()

    # --- research runs ------------------------------------------------------------------------

    def run_research(self, request, card=None):
        """Submit (once) and run a research request, then route the result through the gate.

        With the recovery ladder on, a finished run is triaged first; a fixable failure climbs the
        ladder and the outcome names the linked run that recovered. card: optional TaskCard the
        repair and validator agents see (default: the objective as the one criterion).

        A failed task is classified by decide() on its recorded status: an exhausted RetryableError
        escalates to a human, any other failure is blocked. The route is written to the audit log
        as host_run_routed, so every escalation has a recorded cause.
        """
        try:
            self.runs.load(request.run_id)
        except KeyError:
            self.workflow.submit(request)
        if self.ladder is not None and card is not None:
            self.ladder.register_card(request.run_id, card)
        state = self.workflow.run(request.run_id)
        if self.ladder is not None:
            outcome = self.ladder.handle(state)
            if outcome.route is not None:
                final = self.runs.load(outcome.final_run_id) if outcome.final_run_id else state
                routed = self._routed(request.run_id, outcome.route, final, outcome.reason)
                return RunOutcome(routed.run_id, routed.route, final, routed.reason, outcome.final_run_id)
        status = state["status"]
        if status in ("completed", "awaiting_approval", "needs_revision", "escalated"):
            return self._routed(request.run_id, status, state, state.get("error") or "")
        policy = GatePolicy(allowed_actions=frozenset(state["plan"]["methods"]))
        routes = []
        for call in state["plan"]["calls"]:
            task = state["tasks"][call["task_id"]]
            if task["status"] != "failed":
                continue
            decision = decide(action_for_call(state, call),
                              status_for_task(task, max_attempts=state["plan"]["max_attempts"]), policy)
            routes.append((decision, f"{call['task_id']}: {task.get('error') or 'failed'}"))
        if not routes:
            return self._routed(request.run_id, "blocked", state, state.get("error") or f"run ended {status}")
        route = "escalated" if any(d is GateDecision.ESCALATE for d, _ in routes) else "blocked"
        return self._routed(request.run_id, route, state, "; ".join(reason for _, reason in routes))

    def _routed(self, run_id, route, state, reason):
        with self.db:
            self.audit("host_run_routed", run_id, {"route": route, "status": state["status"], "reason": reason})
        return RunOutcome(run_id, route, state, reason)

    # --- knowledge ----------------------------------------------------------------------------

    def register_experiment(self, record):
        """A verified experiment is stored, then becomes authoritative evidence."""
        self.research.register_experiment(record)
        return self.ingestor.on_experiment_verified(record)

    def promote(self, claim):
        """Provisional -> verified through the deterministic verifier (knowledge.evidence_store)."""
        result = promote(claim, store=self.evidence, metrics=self.metrics)
        with self.db:
            self.audit("claim_promoted" if result.promoted else "claim_refused", claim.claim_id,
                       {"version": claim.version, "reasons": list(result.reasons)})
        return result

    # --- reports ------------------------------------------------------------------------------

    def report(self, report_id, run_id, build_request, refs, *, caller=None, inputs=()):
        """Write and store one report. build_request(metric_segment) -> ModelRequest.

        A ref the metric store cannot serve (missing, unverified, superseded, demoted) escalates
        before any model call: the model is never shown a number that is not authoritative.
        """
        caller = caller or self.reporter
        if caller is None:
            raise ValueError("this Stack has no reporter")
        try:
            segment = metric_segment(self.metrics, refs)
        except KeyError as exc:
            result = ReportResult("escalated", problems=(f"metric not available: {exc.args[0]}",))
            self.reports.save(report_id, run_id, result, (), audit=self.audit)
            return result
        request = build_request(segment)
        identifiers = request_identifiers(request)
        result = write_report(caller, request, metrics=self.metrics, inputs=inputs)
        self.reports.save(report_id, run_id, result, identifiers, audit=self.audit)
        return result

    # --- lifecycle ----------------------------------------------------------------------------

    def close(self):
        if isinstance(self.tracer, Tracer):
            SQLiteSpanStore(self.db).save(self.tracer.spans)
        self.research.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


__all__ = ["RUN_ROUTES", "ReportStore", "RunOutcome", "Stack"]
