"""ContextSession: one agent's context for one task, with health checks and rebuild.

ask(instruction) builds a request, checks it, calls the model through the socket core, checks
the output, and on a failure signal rebuilds and asks again:

  * Rebuild = archive the full working history, build a fresh request from the task card,
    handoffs and required evidence, with history only since the last verified checkpoint.
  * A rebuild after a bad output that is followed by another bad output is a failed rebuild.
    policy.max_failed_rebuilds in a row -> ESCALATE.
  * Evidence conflicts, ContextOverflow and ModelEscalation -> ESCALATE at once.
  * Rebuilds for high utilization or mixed versions are not failed rebuilds.
"""
from dataclasses import dataclass, field
import json
import threading

from ..models.core import ModelEscalation
from ..tracing import NULL_TRACER
from .builder import ContextOverflow
from .health import Action, Finding, HealthReport, LoopDetector, Signal, check_output, check_request


class InMemoryContextArchive:
    def __init__(self):
        self.records = []
        self._lock = threading.Lock()

    def archive(self, run_id, task_id, reason, entries):
        with self._lock:
            self.records.append({"run_id": run_id, "task_id": task_id, "reason": reason,
                                 "entries": [_entry_dict(e) for e in entries]})


class SQLiteContextArchive:
    """Archived histories in the run database. Retention is an open question in the spec; nothing is deleted."""

    def __init__(self, connection):
        self.db = connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS context_archive (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
            reason TEXT NOT NULL, entries TEXT NOT NULL)""")
        self.db.commit()

    def archive(self, run_id, task_id, reason, entries):
        with self.db:
            self.db.execute("INSERT INTO context_archive (run_id, task_id, reason, entries) VALUES (?, ?, ?, ?)",
                            (run_id, task_id, reason, json.dumps([_entry_dict(e) for e in entries], sort_keys=True)))

    @property
    def records(self):
        return [{"run_id": r, "task_id": t, "reason": why, "entries": json.loads(e)} for r, t, why, e in
                self.db.execute("SELECT run_id, task_id, reason, entries FROM context_archive ORDER BY seq")]


def _entry_dict(entry):
    data = {"step": entry.step, "kind": entry.kind, "content": entry.content, "run_id": entry.run_id,
            "mentions": [list(m) for m in entry.mentions]}
    if entry.evidence is not None:
        data["evidence"] = [entry.evidence.evidence_id, entry.evidence.version]
    return data


@dataclass(frozen=True)
class AskResult:
    status: str                 # "ok" or "escalated"
    output: object = None
    model: str | None = None
    reason: str = ""
    model_calls: int = 0
    rebuilds: int = 0
    failed_rebuilds: int = 0
    input_tokens: int = 0       # summed over every request sent
    reports: tuple = field(default=(), repr=False)


class ContextSession:
    def __init__(self, card, *, builder, retriever, caller, store, system, archive=None, permissions=frozenset(),
                 tracer=None):
        self.card = card
        self.builder = builder
        self.retriever = retriever
        self.caller = caller
        self.store = store
        self.system = system
        self.archive = archive or InMemoryContextArchive()
        self.permissions = frozenset(permissions)
        self.tracer = tracer or NULL_TRACER
        self.history = []
        self.handoffs = []
        self.checkpoint_at = 0
        self.loops = LoopDetector()

    @property
    def policy(self):
        return self.builder.policy

    # --- working history -----------------------------------------------------------------

    def record(self, entry):
        """Add a history entry. A repeated tool call with no state change switches retrieval
        strategy and returns the finding (the caller should stop its loop)."""
        self.history.append(entry)
        if entry.kind == "tool_call" and entry.fingerprint:
            finding = self.loops.observe(entry.fingerprint, entry.state_hash)
            if finding:
                self.retriever.change_strategy()
                return finding
        return None

    def add_handoff(self, handoff):
        if handoff.run_id != self.card.run_id:
            raise ValueError(f"handoff from run {handoff.run_id} offered to run {self.card.run_id}")
        self.handoffs.append(handoff)

    def checkpoint(self):
        """Mark everything so far as verified; a rebuild keeps history from here on."""
        self.checkpoint_at = len(self.history)

    def _archive(self, reason):
        self.archive.archive(self.card.run_id, self.card.task_id, reason, list(self.history))

    def _reset_working(self):
        """Drop history entries from other runs or dataset versions (after archiving them)."""
        self._archive("reset_working")
        keep = []
        for entry in self.history:
            item = entry.evidence
            foreign = entry.run_id not in (None, self.card.run_id) or (
                item is not None and (item.run_id not in (None, self.card.run_id) or (
                    item.dataset_name in self.card.dataset_versions
                    and self.card.dataset_versions[item.dataset_name] != item.dataset_version)))
            if not foreign:
                keep.append(entry)
        removed_before_checkpoint = sum(1 for e in self.history[:self.checkpoint_at] if e not in keep)
        self.history = keep
        self.checkpoint_at -= removed_before_checkpoint

    # --- asking the model ----------------------------------------------------------------

    def ask(self, instruction, *, budget_left=None):
        calls = rebuilds = failed = tokens = 0
        reports, minimal, after_bad_output = [], False, False
        limit = 3 + 2 * self.policy.max_failed_rebuilds   # hard stop; the rules below end sooner

        def escalate(reason, model=None):
            return AskResult("escalated", None, model, reason, calls, rebuilds, failed, tokens, tuple(reports))

        with self.tracer.span("context.ask", run_id=self.card.run_id, task_id=self.card.task_id) as span:
            for _ in range(limit):
                try:
                    built = self.builder.build(self.card, retriever=self.retriever, system=self.system,
                                               instruction=instruction, handoffs=self.handoffs, history=self.history,
                                               permissions=self.permissions, budget_left=budget_left,
                                               minimal=minimal, since_checkpoint=self.checkpoint_at,
                                               limit=self.policy.utilization_threshold if minimal else None)
                except ContextOverflow as exc:
                    return escalate(str(exc))
                except ValueError as exc:   # a value the request format cannot carry: fail closed
                    return escalate(f"cannot build the request: {exc}")
                report = check_request(built, self.card, utilization_threshold=self.policy.utilization_threshold)
                reports.append(report)
                action = report.action
                if action is Action.ESCALATE:
                    return escalate("; ".join(f.detail for f in report.findings if f.signal is Signal.EVIDENCE_CONFLICT))
                if action is Action.RESET_WORKING:
                    self._reset_working()
                    rebuilds += 1
                    continue
                if action is Action.DROP_OLD_OUTPUTS_AND_REBUILD and not minimal:
                    self._archive("high_utilization")
                    minimal = True
                    rebuilds += 1
                    continue

                calls += 1
                tokens += built.input_tokens
                try:
                    result = self.caller.call(built.request)
                except ModelEscalation as exc:
                    return escalate(str(exc), exc.model)
                outcome = check_output(result.output, built, self.card, store=self.store)
                reports.append(outcome)
                if outcome.ok:
                    span.set(model=result.model, rebuilds=rebuilds, model_calls=calls)
                    return AskResult("ok", result.output, result.model, "", calls, rebuilds, failed, tokens,
                                     tuple(reports))
                if after_bad_output:
                    failed += 1
                if failed >= self.policy.max_failed_rebuilds:
                    reports.append(HealthReport((Finding(Signal.REBUILDS_EXHAUSTED,
                                                         f"{failed} failed rebuilds in a row"),)))
                    return escalate(f"{failed} failed rebuilds in a row; last: "
                                    + "; ".join(f.detail for f in outcome.findings[:3]), result.model)
                self._archive(outcome.action.value)
                minimal, after_bad_output = True, True
                rebuilds += 1
            return escalate("context session hit its iteration limit")
