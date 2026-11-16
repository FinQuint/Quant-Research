"""ContextSession: one agent's context for one task, with health checks and rebuild.

ask(instruction) builds a request, checks it, calls the model through the socket core, checks
the output, and on a failure signal rebuilds and asks again:

  * Rebuild = archive the full working history, build a fresh request from the task card,
    handoffs and required evidence, with history only since the last verified checkpoint.
  * A rebuild after a bad output that is followed by another bad output is a failed rebuild.
    policy.max_failed_rebuilds in a row -> ESCALATE.
  * Evidence conflicts, ContextOverflow and ModelEscalation -> ESCALATE at once.
  * Rebuilds for high utilization or mixed versions are not failed rebuilds.
  * policy.prioritize_uncited: a rebuild after a bad output asks the builder to place evidence
    for the criteria left uncited first. Topics accumulate over the ask(), so making room for
    one criterion's evidence cannot push out evidence an earlier rebuild added for another. Such
    a rebuild may fill the whole input budget: capping it at the utilization threshold (the aim of
    a high-utilization rebuild) would leave less room than the request that just failed.
  * policy.escalate_identical_rebuild: a rebuilt request identical to one already sent in this
    ask(), while a criterion the last output left uncited has no evidence on its topic in that
    request -> ESCALATE without calling the model: no retry of that request can supply the
    evidence. With the evidence present the retry still runs (a sampled model may get it right).
"""
from dataclasses import dataclass, field
import json
import threading

from ..models.core import ModelEscalation
from ..tracing import NULL_TRACER
from ..verify.citations import check_citations
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


def uncited_criteria(output, card, built, store):
    """Criterion IDs the output did not address, or addressed without a citation that holds."""
    by_id = {c.get("id"): c for c in output.get("criteria", []) if isinstance(c, dict)}
    out = []
    for cid in card.criterion_ids:
        criterion = by_id.get(cid)
        cites = criterion.get("citations", []) if criterion else []
        if not cites or check_citations(cites, store=store, run_id=card.run_id, offered=built.offered):
            out.append(cid)
    return out


def _topics_without_evidence(topics, built):
    """Of these topics, those with no evidence in the built request (evidence or history output)."""
    present = {e.topic for e in built.evidence}
    present |= {h.evidence.topic for h in built.history if h.kind == "tool_output" and h.evidence is not None}
    return tuple(t for t in topics if t not in present)


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

    def _uncited_topics(self, output, built):
        ids = set(uncited_criteria(output, self.card, built, self.store))
        return tuple(c.topic for c in self.card.criteria if c.criterion_id in ids)

    def ask(self, instruction, *, budget_left=None):
        calls = rebuilds = failed = tokens = 0
        reports, minimal, after_bad_output = [], False, False
        sent, priority, last_problems, uncited = set(), (), "", ()
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
                                               limit=None if priority or not minimal
                                               else self.policy.utilization_threshold,
                                               priority_topics=priority)
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

                digest = built.request.digest()
                unsupplied = _topics_without_evidence(uncited, built)
                if self.policy.escalate_identical_rebuild and digest in sent and unsupplied:
                    reports.append(HealthReport((Finding(Signal.REBUILDS_EXHAUSTED,
                                                         "rebuild repeats a sent request that lacks evidence for "
                                                         + ", ".join(unsupplied)),)))
                    return escalate("rebuild repeats a sent request with no evidence for "
                                    + ", ".join(unsupplied) + "; last: " + last_problems)
                sent.add(digest)
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
                last_problems = "; ".join(f.detail for f in outcome.findings[:3])
                uncited = self._uncited_topics(result.output, built)
                if self.policy.prioritize_uncited:   # accumulate: a fix for one criterion must not drop another's
                    priority = priority + tuple(t for t in uncited if t not in priority)
                self._archive(outcome.action.value)
                minimal, after_bad_output = True, True
                rebuilds += 1
            return escalate("context session hit its iteration limit")
