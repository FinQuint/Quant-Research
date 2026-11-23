"""Retention (2026-11-23 review): delete operational data past its age, never the safety trail or knowledge."""
from datetime import datetime, timezone
import json
import sqlite3
import time

import pytest

from finquint.orchestration import retention
from finquint.orchestration.context.session import SQLiteContextArchive
from finquint.orchestration.evals import recovery as suite
from finquint.orchestration.retention import DAY, RetentionPolicy
from finquint.orchestration.tracing import SQLiteSpanStore

POLICY = RetentionPolicy.from_file()
KEEP_FOREVER = ("approval_requests", "approval_records", "approval_uses", "side_effects", "outbox",
                "context_evidence", "host_reports", "recovery_lessons", "recovery_fix_proposals")


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


@pytest.fixture
def db_path(tmp_path):
    real = time.time()
    stack = suite._stack(str(tmp_path), script=[{"set": {"window": 20}}])
    stack.run_research(suite._vol("old_done", 10))                                   # finished, uncited
    stack.run_research(suite._vol("rec", 60))                                        # recovered: rec + rec-r1
    stack.run_research(suite._vol("esc", 60), card=suite._window_card("esc"))        # open escalation
    stack.run_research(suite._vol("cited_run", 10))                                  # a report cites it
    stack.run_research(suite._vol("fresh", 10))                                      # recent
    stack.workflow.submit(suite._vol("ready", 10))                                   # never run
    SQLiteSpanStore(stack.db)                                                         # creates trace_spans
    with stack.db:
        stack.db.execute("INSERT INTO host_reports (report_id, run_id, status, text, numbers, identifiers, problems, "
                         "model, model_calls) VALUES (?,?,?,?,?,?,?,?,?)",
                         ("rep1", "other", "ok", "Vol is {{metric:run:cited_run/risk:volatility|.4f}}.", "[]", "[]",
                          "[]", "m", 1))
        stack.db.execute("UPDATE audit_events SET created_at=? WHERE event_type='autonomous_planned' AND subject_id='fresh'",
                         (_iso(real + 399 * DAY),))
        for trace_id, start, run_id in (("t_old", real, "old_done"), ("t_esc", real, "esc"),
                                        ("t_new", real + 399 * DAY, "old_done")):
            stack.db.execute("INSERT INTO trace_spans VALUES (?,?,?,?,?,?,?,?,?)",
                             (trace_id, "s1", None, "x", start, start, "ok", json.dumps({"run_id": run_id}), None))
    SQLiteContextArchive(stack.db, clock=lambda: real).archive("old_done", "t", "rebuild", [])
    SQLiteContextArchive(stack.db, clock=lambda: real).archive("esc", "t", "rebuild", [])
    SQLiteContextArchive(stack.db, clock=lambda: real + 399 * DAY).archive("fresh", "t", "rebuild", [])
    SQLiteContextArchive(stack.db, clock=lambda: real).archive("cited_run", "t", "rebuild", [])
    stack.close()
    return tmp_path / "research.sqlite", real + 400 * DAY


def _counts(path):
    db = sqlite3.connect(path)
    try:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        db.close()


def _column(path, query):
    db = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in db.execute(query))
    finally:
        db.close()


def test_policy_file_matches_the_review():
    assert (POLICY.run_days, POLICY.trace_days, POLICY.context_days) == (365, 365, 90)
    with pytest.raises(ValueError):
        RetentionPolicy(run_days=0)


def test_unknown_policy_settings_are_refused(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"run_days": 1, "approvals_days": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        RetentionPolicy.from_file(path)


def test_dry_run_changes_nothing(db_path):
    path, now = db_path
    before = _counts(path)
    report = retention.run(path, policy=POLICY, now=now)
    assert report["applied"] is False and report["expire_runs"] == ["old_done", "rec", "rec-r1"]
    assert _counts(path) == before


def test_apply_deletes_operational_data_and_keeps_the_rest(db_path):
    path, now = db_path
    before = _counts(path)
    report = retention.run(path, policy=POLICY, now=now, do_apply=True)
    after = _counts(path)
    runs = _column(path, "SELECT run_id FROM autonomous_runs")
    assert runs == ["cited_run", "esc", "esc-r1", "fresh", "ready"]
    assert report["kept_runs"] == {"cited as a metric": 1, "not finished": 1, "open recovery escalation": 2,
                                   "pending approval": 2, "younger than the run limit": 1}
    assert _column(path, "SELECT root_run_id FROM recovery_runs") == ["esc"]
    assert _column(path, "SELECT run_id FROM context_archive") == ["esc", "fresh"]
    assert _column(path, "SELECT trace_id FROM trace_spans") == ["t_esc", "t_new"]
    for table in KEEP_FOREVER:
        assert after.get(table) == before.get(table), table
    assert after["audit_events"] == before["audit_events"] + 1
    event = json.loads(_column(path, "SELECT payload FROM audit_events WHERE event_type='retention_purged'")[0])
    assert event["applied"] and event["rows"]["autonomous_runs"] == 3 and event["expire_runs"] == ["old_done", "rec", "rec-r1"]


def test_apply_twice_deletes_nothing_more(db_path):
    path, now = db_path
    retention.run(path, policy=POLICY, now=now, do_apply=True)
    second = retention.run(path, policy=POLICY, now=now, do_apply=True)
    assert second["expire_runs"] == [] and second["rows"]["context_archive"] == 0 and second["rows"]["trace_spans"] == 0


def test_nothing_expires_before_its_age(db_path):
    path, _ = db_path
    report = retention.run(path, policy=POLICY, now=time.time() + 30 * DAY)
    assert report["expire_runs"] == [] and report["rows"]["trace_spans"] == 0 and report["rows"]["context_archive"] == 0


def test_context_histories_expire_after_ninety_days_even_for_kept_runs(db_path):
    path, _ = db_path
    report = retention.run(path, policy=POLICY, now=time.time() + 120 * DAY)
    assert report["expire_runs"] == [] and report["rows"]["context_archive"] == 2   # old_done and cited_run, not esc


def test_unknown_start_time_is_kept(db_path):
    path, now = db_path
    db = sqlite3.connect(path)
    with db:
        db.execute("DELETE FROM audit_events WHERE event_type='autonomous_planned' AND subject_id='old_done'")
    db.close()
    report = retention.run(path, policy=POLICY, now=now)
    assert "old_done" not in report["expire_runs"] and report["kept_runs"]["start time unknown"] == 1


def test_context_archive_gains_created_at_on_an_old_table(tmp_path):
    db = sqlite3.connect(tmp_path / "a.sqlite")
    db.execute("CREATE TABLE context_archive (seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
               "task_id TEXT NOT NULL, reason TEXT NOT NULL, entries TEXT NOT NULL)")
    db.execute("INSERT INTO context_archive (run_id, task_id, reason, entries) VALUES ('r','t','x','[]')")
    db.commit()
    archive = SQLiteContextArchive(db, clock=lambda: 123.0)
    archive.archive("r2", "t", "rebuild", [])
    assert [r[0] for r in db.execute("SELECT created_at FROM context_archive ORDER BY seq")] == [None, 123.0]
    assert len(archive.records) == 2
    db.close()


def test_cli_dry_run_and_missing_db(db_path, capsys):
    path, _ = db_path
    assert retention.main(["--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    with pytest.raises(SystemExit):
        retention.main(["--db", str(path) + ".missing"])
