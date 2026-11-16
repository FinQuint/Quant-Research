"""Phase 7: the host Stack and the full-stack soak (exit test), plus scorer sensitivity."""
from dataclasses import replace
import json
import shutil
import sqlite3

import pytest

from finquint.autonomous import Call, Request, RetryableError, Source
from finquint.autonomous import MethodRegistry
from finquint.autonomous.adapters import default_registry
from finquint.orchestration.context.evidence import Evidence, SQLiteEvidenceStore
from finquint.orchestration.evals.soak import (CONFIG_PATH, MIN_HOURS, SoakPolicy, criteria, grade,
                                               load_baseline, run_soak, score)
from finquint.orchestration.gate.decide import ProposedAction
from finquint.orchestration.host import Stack
from finquint.orchestration.knowledge.evidence_store import Claim, promote
from finquint.orchestration.knowledge.ingestion import Ingestor
from finquint.orchestration.knowledge.metrics import EvidenceMetrics
from finquint.research import ResearchStore

APPROVAL_KEY = b"test-approval-key-00000000000000"
CAPABILITY_KEY = b"test-capability-key-000000000000"
POLICY = "configs/approval_policy.json"
SOURCE = Source("methodology", "repo://docs/conventions", "v1", "Yields use nominal annual compounding.")
BOND = {"bond": {"face_value": 100, "coupon_rate": .05, "maturity_years": 5, "frequency": 2}, "annual_yield": .04}


# --- the exit test ------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def soak(tmp_path_factory):
    directory = tmp_path_factory.mktemp("soak")
    document = run_soak(directory=directory)
    return directory, document


def test_soak_meets_every_success_criterion(soak):
    _, document = soak
    failed = [name for name, ok in document["grade"]["criteria"].items() if not ok]
    assert failed == []
    assert document["metrics"]["unattended_hours"] >= SoakPolicy.from_file().min_unattended_hours


def test_soak_matches_the_baseline(soak):
    _, document = soak
    baseline = load_baseline()
    for section in ("metrics", "facts", "grade", "policy"):
        assert document[section] == baseline[section], section


def test_soak_policy_file_matches_the_loader():
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    policy = SoakPolicy.from_file()
    assert (policy.hours, policy.min_unattended_hours) == (data["hours"], data["min_unattended_hours"])
    with pytest.raises(ValueError):
        SoakPolicy(MIN_HOURS - 1, 3, 12, 24, 1.0)
    with pytest.raises(ValueError):
        SoakPolicy(MIN_HOURS, 3, 12, 72, 0.0)


# --- the scorer is not vacuous: tampered durable state is caught ---------------------------------

@pytest.fixture
def tampered(soak, tmp_path):
    directory, _ = soak
    copy = tmp_path / "copy"
    shutil.copytree(directory, copy)
    return copy


def _remote(directory):
    return sqlite3.connect(directory / "remote.sqlite")


def _metrics(directory):
    return score(directory, SoakPolicy.from_file())["metrics"]


def test_scorer_catches_a_merge_without_approval(tampered):
    with _remote(tampered) as db:
        action = ProposedAction("merge_to_main", "repo:main", {"branch": "agent/rogue"}, code_sha="x").to_dict()
        db.execute("INSERT INTO deliveries (tool, key, fingerprint, action) VALUES (?,?,?,?)",
                   ("merge_to_main", "rogue-key", "0" * 64, json.dumps(action)))
    assert _metrics(tampered)["unauthorized_actions"] == 1


def test_scorer_catches_a_duplicate_delivery(tampered):
    with _remote(tampered) as db:
        db.execute("INSERT INTO deliveries (tool, key, fingerprint, action) SELECT tool, key, fingerprint, action "
                   "FROM deliveries WHERE tool = 'merge_to_main' ORDER BY seq LIMIT 1")
    metrics = _metrics(tampered)
    assert metrics["duplicate_side_effects"] == 1
    assert metrics["unauthorized_actions"] == 0     # the copy carries an approved fingerprint


def test_scorer_catches_a_protected_merge(tampered):
    with _remote(tampered) as db:
        patch = "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        action = ProposedAction("merge_to_main", "repo:main", {"branch": "agent/rogue", "patch": patch},
                                code_sha="x").to_dict()
        db.execute("INSERT INTO deliveries (tool, key, fingerprint, action) VALUES (?,?,?,?)",
                   ("merge_to_main", "rogue-key", "0" * 64, json.dumps(action)))
    assert _metrics(tampered)["protected_merges"] == 1


def test_scorer_catches_an_untraced_number_in_a_stored_report(tampered):
    with sqlite3.connect(tampered / "research.sqlite") as db:
        seq, text = db.execute("SELECT seq, text FROM host_reports WHERE status='ok' ORDER BY seq").fetchone()
        db.execute("UPDATE host_reports SET text = ? WHERE seq = ?", (text + " Up 42 percent.", seq))
    metrics = _metrics(tampered)
    assert metrics["untraced_numbers"] == 1 and metrics["traced_share"] < 1.0


def test_scorer_catches_a_number_that_does_not_match_its_artifact(tampered):
    with sqlite3.connect(tampered / "research.sqlite") as db:
        seq, numbers = db.execute("SELECT seq, numbers FROM host_reports WHERE status='ok' ORDER BY seq").fetchone()
        numbers = json.loads(numbers)
        numbers[0]["value"] = 999.0
        db.execute("UPDATE host_reports SET numbers = ? WHERE seq = ?", (json.dumps(numbers), seq))
    assert _metrics(tampered)["untraced_numbers"] == 1


def test_scorer_counts_human_input_on_routine_work(tampered):
    with sqlite3.connect(tampered / "research.sqlite") as db:
        db.execute("INSERT INTO approval_requests SELECT 'routine-req', 'soak-r40', branch_id, fingerprint, action, "
                   "reason, evidence, created_at, expires_at, status, policy_version FROM approval_requests LIMIT 1")
    assert _metrics(tampered)["unattended_hours"] == 40     # hours 0-39; hour 40 had human input


def test_criteria_fail_on_a_bad_document(soak):
    _, document = soak
    bad = json.loads(json.dumps(document))
    bad["metrics"]["duplicate_side_effects"] = 1
    bad["facts"]["branches"]["ship-24"] = "done"
    failed = {n for n, ok in grade(bad, SoakPolicy.from_file())["criteria"].items() if not ok}
    # a blocked branch reported done also breaks the one-delivery-per-done-branch count
    assert failed == {"zero_duplicate_side_effects", "protected_patch_blocked", "approved_merges_delivered_once"}
    assert len(criteria(SoakPolicy.from_file())) == grade(document, SoakPolicy.from_file())["total"]


# --- the host Stack -----------------------------------------------------------------------------

def _stack(tmp_path, registry=None, **kwargs):
    return Stack(tmp_path, approval_key=APPROVAL_KEY, capability_key=CAPABILITY_KEY, approval_policy=POLICY,
                 registry=registry or default_registry(), **kwargs)


def test_stack_refuses_one_key_for_both_roles(tmp_path):
    with pytest.raises(ValueError):
        Stack(tmp_path, approval_key=APPROVAL_KEY, capability_key=APPROVAL_KEY, approval_policy=POLICY,
              registry=default_registry())


def test_stack_routes_a_run_with_exhausted_retries_to_escalation(tmp_path):
    base = default_registry()

    def down(parameters, context):
        raise RetryableError("data API unavailable")
    registry = MethodRegistry()
    registry.register(replace(base.get("source_review"), execute=down))
    registry.register(base.get("bond_analytics"))
    with _stack(tmp_path, registry) as stack:
        outcome = stack.run_research(Request("r1", "Price a bond", "sha", (
            Call("research", "source_review", {"source_ids": ["methodology"]}),
            Call("analytics", "bond_analytics", BOND)), (SOURCE,)))
        assert outcome.route == "escalated" and "RetryableError" in outcome.reason
        routed = [e for e in stack.research.audit_log() if e["event_type"] == "host_run_routed"]
        assert routed[-1]["payload"]["route"] == "escalated"


def test_stack_completes_a_run_and_ingests_it(tmp_path):
    with _stack(tmp_path) as stack:
        outcome = stack.run_research(Request("r2", "Price a bond", "sha", (
            Call("research", "source_review", {"source_ids": ["methodology"]}),
            Call("analytics", "bond_analytics", BOND)), (SOURCE,)))
        assert outcome.route == "completed"
        assert stack.metrics.get("run:r2/analytics")["bond_price"] > 100
        assert stack.evidence.current("run-r2-analytics") is not None
        # a second call does not resubmit or rerun
        assert stack.run_research(Request("r2", "Price a bond", "sha", (
            Call("research", "source_review", {"source_ids": ["methodology"]}),
            Call("analytics", "bond_analytics", BOND)), (SOURCE,))).route == "completed"


class _NeverCalled:
    def call(self, request):
        raise AssertionError("the model must not be called")


def test_stack_report_on_an_unavailable_metric_escalates_before_any_model_call(tmp_path):
    with _stack(tmp_path, reporter=_NeverCalled()) as stack:
        result = stack.report("rep-1", "r9", lambda segment: None, [("run:missing/analytics", "bond_price")])
        assert result.status == "escalated" and "metric not available" in result.problems[0]
        assert stack.reports.all()[0]["status"] == "escalated"


# --- knowledge: a dataset change demotes claims that rest on it ----------------------------------

def _knowledge(tmp_path):
    research = ResearchStore(tmp_path / "k.sqlite")
    store = SQLiteEvidenceStore(research.connection)
    return research, store, Ingestor(store, research.connection)


def _claim(claim_id, cites):
    return Claim(claim_id, "1", "momentum", "Sharpe is {{metric:evidence:" + cites[0][0] + "@" + cites[0][1]
                 + ":sharpe|.2f}}.", cites, claims={"sharpe": "1.1"})


def test_dataset_change_demotes_a_promoted_claim_resting_on_old_data(tmp_path):
    research, store, ingestor = _knowledge(tmp_path)
    store.put(Evidence("exp-a", "1", "experiment", "{}", dataset="prices@v1", claims={"sharpe": "1.1"}))
    store.put(Evidence("exp-b", "1", "experiment", "{}", dataset="volumes@v1", claims={"sharpe": "0.9"}))
    metrics = EvidenceMetrics(store)
    assert promote(_claim("on-prices", (("exp-a", "1"),)), store=store, metrics=metrics).promoted
    assert promote(_claim("on-volumes", (("exp-b", "1"),)), store=store, metrics=metrics).promoted
    # a claim resting on a claim: demoted transitively
    assert promote(Claim("summary", "1", "momentum", "Sharpe is {{metric:evidence:on-prices@1.verified:sharpe}}.",
                         (("on-prices", "1.verified"),), claims={"sharpe": "1.1"}),
                   store=store, metrics=metrics).promoted
    report = ingestor.on_dataset_version("prices", "v2")
    demoted = {ref[0] for ref in report.demoted}
    assert demoted == {"on-prices", "summary"}
    assert store.current("on-prices").tier == "provisional"
    assert store.current("on-volumes").tier == "verified"
    assert metrics.get("evidence:on-prices@1.verified") is None
    assert store.get("on-prices", "1.verified") is not None          # old version kept
    research.close()


def test_scorer_fails_closed_on_an_unreadable_delivery(tampered):
    with _remote(tampered) as db:
        db.execute("INSERT INTO deliveries (tool, key, fingerprint, action) VALUES (?,?,?,?)",
                   ("merge_to_main", "rogue-key", "0" * 64, json.dumps({"action_type": "merge_to_main"})))
    metrics = _metrics(tampered)
    assert metrics["protected_merges"] == 1 and metrics["unauthorized_actions"] == 1
