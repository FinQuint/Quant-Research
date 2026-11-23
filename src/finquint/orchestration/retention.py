"""Retention: delete operational data past its age, never the safety trail or knowledge.

Policy (configs/retention_policy.json, all ages (tune)): finished runs 365 days, traces 365 days,
archived context histories 90 days (spec, "Open questions", answered 2026-11-23).

Deleted when old enough:
  * finished research runs (autonomous_runs payloads) with their recovery transcripts and archived
    context histories; a recovery root and its linked child runs go together, aged by the newest;
  * trace spans;
  * archived context histories (also of runs that are kept).

Never deleted:
  * approval requests, records and uses (records are immutable, ADR-013), the audit log, and the
    idempotency ledger and outbox (they back "no duplicate side effects");
  * knowledge: evidence, experiments, artifacts, claims, reports, recovery lessons and known-fix
    proposals;
  * a run that is not finished, has a pending approval request, has an open recovery escalation,
    or whose output a stored report or knowledge item cites as a metric (run:RUN/TASK), because
    that output backs a reported number;
  * anything whose age cannot be read (fail safe: keep).

    python -m finquint.orchestration.retention --db RUN_DIR/research.sqlite           # dry run
    python -m finquint.orchestration.retention --db RUN_DIR/research.sqlite --apply   # delete

A dry run only reads. --apply deletes in one transaction and writes one audit event,
"retention_purged", with the counts, cutoffs, policy version and the deleted run IDs. SQLite does not
shrink the file on delete; run VACUUM separately if the space matters.
"""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "retention_policy.json"
DAY = 86400.0
FINISHED = frozenset({"completed", "failed", "rejected", "escalated", "needs_revision"})
OPEN_RECOVERY = frozenset({"open", "escalated"})


@dataclass(frozen=True)
class RetentionPolicy:
    run_days: float = 365
    trace_days: float = 365
    context_days: float = 90
    version: str = "1"

    def __post_init__(self):
        for name in ("run_days", "trace_days", "context_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
                raise ValueError(f"{name} must be a positive number of days")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("policy version is required")

    @classmethod
    def from_file(cls, path=CONFIG_PATH):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        unknown = set(data) - {"version", "run_days", "trace_days", "context_days"}
        if unknown:
            raise ValueError(f"unknown retention settings: {sorted(unknown)}")
        return cls(data.get("run_days", 365), data.get("trace_days", 365), data.get("context_days", 90),
                   str(data.get("version", "1")))


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _epoch(iso):
    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _run_starts(db):
    """run_id -> start time (epoch) from the audit log's first autonomous_planned event."""
    starts = {}
    for subject, created in db.execute("SELECT subject_id, created_at FROM audit_events "
                                       "WHERE event_type='autonomous_planned' ORDER BY sequence"):
        if subject not in starts:
            starts[subject] = _epoch(created)
    return starts


_RUN_PLACEHOLDER = re.compile(r"metric:run:([A-Za-z0-9_-]+)/")


def _cited_runs(db, tables):
    """Runs a stored report or knowledge item cites as a metric (run:RUN/TASK): their outputs back a
    reported number. Evidence ingested from a run keeps its own copy of the output, so merely coming
    from a run does not pin it."""
    cited = set()
    if "host_reports" in tables:
        for text, numbers in db.execute("SELECT text, numbers FROM host_reports"):
            cited.update(_RUN_PLACEHOLDER.findall(text))
            for number in json.loads(numbers):
                artifact = number.get("artifact_id") or ""
                if artifact.startswith("run:") and "/" in artifact:
                    cited.add(artifact[4:].split("/", 1)[0])
    if "context_evidence" in tables:
        for (data,) in db.execute("SELECT data FROM context_evidence"):
            cited.update(_RUN_PLACEHOLDER.findall(data))
    return cited


def plan(db, policy, now):
    """What --apply would delete, and why every other run is kept. Reads only."""
    tables = _tables(db)
    run_cutoff, trace_cutoff, context_cutoff = (now - policy.run_days * DAY, now - policy.trace_days * DAY,
                                                now - policy.context_days * DAY)
    runs = {r: json.loads(p)["status"] for r, p in db.execute("SELECT run_id, payload FROM autonomous_runs")} \
        if "autonomous_runs" in tables else {}
    starts = _run_starts(db) if "audit_events" in tables else {}
    cited = _cited_runs(db, tables)
    pending = {r for (r,) in db.execute("SELECT DISTINCT run_id FROM approval_requests WHERE status='pending'")} \
        if "approval_requests" in tables else set()
    root_of, open_roots = {}, set()
    if "recovery_children" in tables:
        root_of = dict(db.execute("SELECT child_run_id, root_run_id FROM recovery_children"))
    if "recovery_runs" in tables:
        open_roots = {r for (r, s) in db.execute("SELECT root_run_id, status FROM recovery_runs") if s in OPEN_RECOVERY}

    groups = {}
    for run_id in runs:
        groups.setdefault(root_of.get(run_id, run_id), []).append(run_id)
    expire, kept, active = [], {}, set()
    for root, members in sorted(groups.items()):
        reasons = set()
        for run_id in members:
            if runs[run_id] not in FINISHED:
                reasons.add("not finished")
                active.add(run_id)
            if run_id in pending:
                reasons.add("pending approval")
                active.add(run_id)
            if run_id in cited:
                reasons.add("cited as a metric")
            if starts.get(run_id) is None:
                reasons.add("start time unknown")
            elif starts[run_id] >= run_cutoff:
                reasons.add("younger than the run limit")
        if root in open_roots:
            reasons.add("open recovery escalation")
            active.update(members)
        if reasons:
            for reason in reasons:
                kept[reason] = kept.get(reason, 0) + len(members)
        else:
            expire.extend(sorted(members))
    roots = sorted({root_of.get(r, r) for r in expire})
    counts = {"autonomous_runs": len(expire)}
    if "recovery_runs" in tables:
        q = ",".join("?" * len(roots))
        counts["recovery_runs"] = db.execute(f"SELECT COUNT(*) FROM recovery_runs WHERE root_run_id IN ({q})", roots).fetchone()[0] if roots else 0
        counts["recovery_attempts"] = db.execute(f"SELECT COUNT(*) FROM recovery_attempts WHERE root_run_id IN ({q})", roots).fetchone()[0] if roots else 0
        counts["recovery_reviews"] = db.execute(f"SELECT COUNT(*) FROM recovery_reviews WHERE root_run_id IN ({q})", roots).fetchone()[0] if roots else 0
        counts["recovery_children"] = db.execute(f"SELECT COUNT(*) FROM recovery_children WHERE root_run_id IN ({q})", roots).fetchone()[0] if roots else 0
        q2 = ",".join("?" * len(expire))
        counts["recovery_cards"] = db.execute(f"SELECT COUNT(*) FROM recovery_cards WHERE run_id IN ({q2})", expire).fetchone()[0] if expire else 0
    context = []
    if "context_archive" in tables:
        expired = set(expire)
        for seq, run_id, created in db.execute("SELECT seq, run_id, created_at FROM context_archive"):
            if run_id in active:
                continue
            age = created if created is not None else starts.get(run_id)
            if run_id in expired or (age is not None and age < context_cutoff):
                context.append(seq)
        counts["context_archive"] = len(context)
    spans = []
    if "trace_spans" in tables:
        for trace_id, span_id, start, attributes in db.execute("SELECT trace_id, span_id, start, attributes FROM trace_spans"):
            if start is not None and start < trace_cutoff and json.loads(attributes).get("run_id") not in active:
                spans.append((trace_id, span_id))
        counts["trace_spans"] = len(spans)
    return {"now": now, "policy": {"version": policy.version, "run_days": policy.run_days,
                                   "trace_days": policy.trace_days, "context_days": policy.context_days},
            "cutoffs": {"runs": run_cutoff, "traces": trace_cutoff, "context": context_cutoff},
            "expire_runs": expire, "expire_roots": roots, "kept_runs": dict(sorted(kept.items())),
            "rows": counts, "_context_seqs": context, "_spans": spans}


def apply(db, policy, now):
    """Plan and delete inside one write transaction (nothing can start citing or using a run between
    the plan and the delete), then audit it. Returns the public report."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        report = plan(db, policy, now)
        expire, roots = report["expire_runs"], report["expire_roots"]
        tables = _tables(db)
        for run_id in expire:
            db.execute("DELETE FROM autonomous_runs WHERE run_id=?", (run_id,))
            if "recovery_cards" in tables:
                db.execute("DELETE FROM recovery_cards WHERE run_id=?", (run_id,))
        if "recovery_runs" in tables:
            for root in roots:
                for table in ("recovery_attempts", "recovery_reviews", "recovery_children", "recovery_runs"):
                    db.execute(f"DELETE FROM {table} WHERE root_run_id=?", (root,))
        for seq in report["_context_seqs"]:
            db.execute("DELETE FROM context_archive WHERE seq=?", (seq,))
        for trace_id, span_id in report["_spans"]:
            db.execute("DELETE FROM trace_spans WHERE trace_id=? AND span_id=?", (trace_id, span_id))
        public = public_report(report)
        public["applied"] = True
        public["expire_runs_sha256"] = sha256(json.dumps(expire).encode()).hexdigest()
        db.execute("INSERT INTO audit_events(event_type, subject_id, payload, created_at) VALUES (?,?,?,?)",
                   ("retention_purged", "retention", json.dumps(public, sort_keys=True),
                    datetime.now(timezone.utc).isoformat()))
    return public


def public_report(report):
    return {k: v for k, v in report.items() if not k.startswith("_")}


def run(path, *, policy=None, now=None, do_apply=False):
    policy = policy or RetentionPolicy.from_file()
    now = time.time() if now is None else now
    db = sqlite3.connect(path)
    try:
        if do_apply:
            return apply(db, policy, now)
        return dict(public_report(plan(db, policy, now)), applied=False)
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Delete operational data past its retention age (dry run by default).")
    parser.add_argument("--db", required=True, help="the run database (research.sqlite)")
    parser.add_argument("--policy", default=str(CONFIG_PATH))
    parser.add_argument("--apply", action="store_true", help="delete; without it nothing is changed")
    args = parser.parse_args(argv)
    if not Path(args.db).is_file():
        parser.error(f"no database at {args.db}")
    report = run(args.db, policy=RetentionPolicy.from_file(args.policy), do_apply=args.apply)
    json.dump(report, sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
