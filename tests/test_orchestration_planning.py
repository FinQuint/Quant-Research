"""Phase 9: planning layer (ADR-022)."""
from dataclasses import replace
import json

import pytest

from finquint.backtesting import EventDrivenBacktester
from finquint.orchestration.context.evidence import Evidence
from finquint.orchestration.evals import planning as suite
from finquint.orchestration.gate.approvals import ApprovalPolicy
from finquint.orchestration.models import ModelCaller
from finquint.orchestration.planning import (DatasetError, FileDatasetCatalog, PlanningService, catalog_view,
                                             fill_template, parse_ref, planning_registry, trusted_evidence)
from finquint.orchestration.planning.simulated import SimulatedPlanner, SimulatedPlanReviewer
from finquint.orchestration.verify.invariants import check_lookahead
from finquint.orchestration.verify.paths import DEFAULT_PROTECTED_PATHS
from finquint.orchestration.verify.plans import PlanningPolicy, check_plan, plan_cost
from finquint.strategies import TEMPLATES, template

CATALOG = FileDatasetCatalog.from_file()
REGISTRY = planning_registry(CATALOG)
POLICY = PlanningPolicy.from_file(suite.PLANNING_POLICY_PATH)


def _check(plan, **card_kw):
    return check_plan(plan, card=suite.card("p", **card_kw), catalog=CATALOG, registry=REGISTRY, policy=POLICY)


# --- policy, catalog, strategies --------------------------------------------------------------------

def test_policy_files_match_the_settled_decisions():
    assert (POLICY.max_plan_attempts, POLICY.max_plan_calls, POLICY.template_threshold) == (2, 12, 3)
    assert POLICY.planner_methods == {"source_review", "parameter_search", "backtest", "performance_summary"}
    assert POLICY.plan_templates == ()
    approvals = ApprovalPolicy.from_file(suite.APPROVAL_POLICY_PATH)
    assert approvals.requirement("plan_escalation").approvals == 1
    assert "src/finquint/orchestration/planning" in DEFAULT_PROTECTED_PATHS
    with pytest.raises(ValueError):
        replace(POLICY, method_costs={"source_review": 1})


def test_catalog_pins_hashes_and_windows(tmp_path):
    entry = CATALOG.get("demo_prices@1")
    train, test = CATALOG.load("demo_prices@1", "train"), CATALOG.load("demo_prices@1", "test")
    assert train["timestamp"].max() <= entry.split.train_end < entry.split.test_start <= test["timestamp"].min()
    for bad in ("demo_prices", "demo_prices@2", "x@1", None):
        with pytest.raises(DatasetError):
            CATALOG.get(bad)
    with pytest.raises(DatasetError):
        CATALOG.load("demo_prices@1", "all")
    data = json.loads(open(suite.DATASETS_PATH, encoding="utf-8").read())
    data["datasets"][0]["path"] = "../outside.csv"
    with pytest.raises(DatasetError):
        FileDatasetCatalog.from_dict(data)
    assert parse_ref("a-b@1.2") == ("a-b", "1.2")


def test_the_demo_dataset_regenerates_byte_for_byte():
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_demo", suite.REPO_ROOT / "datasets" / "make_demo_prices.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = module.make().to_csv(index=False, lineterminator="\n").encode()
    from hashlib import sha256
    assert sha256(text).hexdigest() == CATALOG.get("demo_prices@1").sha256


def test_momentum_template_is_bounded_and_has_no_lookahead():
    tpl = template("ts_momentum")
    assert tpl.resolve({}) == {"lookback": 60, "units": 5000, "long_only": False}
    for bad in ({"lookback": 4}, {"lookback": 20.5}, {"units": 0}, {"long_only": 1}, {"other": 1}):
        with pytest.raises(ValueError):
            tpl.resolve(bad)
    data = CATALOG.load("demo_prices@1", "train")
    assert check_lookahead(data, lambda frame: tpl.build("DEMO", {"lookback": 20}), cutoff_count=4).passed
    result = EventDrivenBacktester().run(data, tpl.build("DEMO", {"lookback": 20, "long_only": True}))
    assert (result.positions["quantity"] >= 0).all() and len(result.fills) > 0
    assert set(TEMPLATES) == {"ts_momentum"}


# --- plan checks --------------------------------------------------------------------------------------

def test_a_good_plan_passes_and_becomes_a_request():
    check = _check(suite.good_plan())
    assert check.passed and check.cost == 1 + 3 + 2 + 1
    assert [c.method for c in check.request.calls] == ["source_review", "parameter_search", "backtest", "performance_summary"]
    assert [s.source_id for s in check.request.sources] == [suite.PROFILE]


@pytest.mark.parametrize("plan,expected", [
    (suite._with(suite.good_plan(), "oos", dependencies=["profile"]), "params_from 'search' must be a direct dependency"),
    (suite._with(suite.good_plan(), "perf", parameters={"source_task": "search"}, dependencies=["search"]),
     "source_task 'search' must be a backtest call"),
    (suite._with(suite.good_plan(), "perf", criteria=["c_nope"]), "serves unknown criteria"),
    (suite._with(suite.good_plan(), "perf", criteria=[]), "serves no acceptance criterion"),
    (suite._with(suite.good_plan(), "profile", dependencies=["search"]), "plan rejected by the planner rules"),
    ({"calls": suite.good_plan()["calls"] * 4}, "at most 12"),
    ({"calls": []}, "the plan has no calls"),
    (suite._with(suite.good_plan(), "search", parameters={"dataset": "demo_prices@2"}), "not in the catalog"),
])
def test_plan_check_rejections(plan, expected):
    check = _check(plan)
    assert not check.passed and any(expected in p for p in check.problems), check.problems


def test_dataset_must_match_the_cards_pin():
    check = _check(suite.good_plan(), datasets={"demo_prices": "2"})
    assert any("differs from the card's pin" in p for p in check.problems)
    assert any("not pinned on the task card" in p for p in _check(suite.good_plan(), datasets={"other": "1"}).problems)


def test_plan_cost_counts_grid_combinations():
    assert plan_cost(suite.BIG_GRID["calls"], POLICY) == 1 + 20 + 2 + 1
    assert _check(suite.BIG_GRID, budget=24).passed and not _check(suite.BIG_GRID, budget=23).passed


# --- service behaviors beyond the eval -------------------------------------------------------------------

def _service(tmp_path, planner_script=None, reviewer=None, policy=POLICY):
    stack, service = suite._setup(str(tmp_path), script=planner_script or [suite.good_plan()], policy=policy)
    if reviewer is not None:
        service.reviewer = reviewer
    return stack, service


def test_planner_sees_only_verified_evidence_and_the_reviewer_is_blind(tmp_path):
    planner, reviewer = SimulatedPlanner([suite.good_plan()]), SimulatedPlanReviewer()
    stack, service = _service(tmp_path)
    service.planner, service.reviewer = ModelCaller(planner), ModelCaller(reviewer)
    evidence = [Evidence("ev_ok", "1", "oos", "momentum held out of sample in 2021", tier="verified"),
                Evidence("ev_draft", "1", "oos", "a draft note", tier="provisional"),
                Evidence("ev_old", "1", "oos", "superseded", tier="authoritative", deprecated=True)]
    assert [e.evidence_id for e in trusted_evidence(evidence)] == ["ev_ok"]
    service.plan_and_run(suite.card("p1"), evidence=evidence)
    assert "ev_ok" in planner.seen[0] and "ev_draft" not in planner.seen[0] and "ev_old" not in planner.seen[0]
    assert "momentum held out of sample" in planner.seen[0]
    assert "rationale" not in reviewer.seen[0] and "parameter_search step" not in reviewer.seen[0]
    stack.close()


def test_plan_and_run_is_idempotent(tmp_path):
    stack, service = _service(tmp_path)
    first = service.plan_and_run(suite.card("p1"))
    again = service.plan_and_run(suite.card("p1"))
    assert first.route == again.route == "ran" and again.model_calls == first.model_calls == 2
    assert service.planner.plugs[0].calls == 1
    stack.close()


def test_no_planner_or_reviewer_escalates(tmp_path):
    stack, service = _service(tmp_path)
    service.reviewer = None
    assert service.plan_and_run(suite.card("p1")).route == "escalated"
    service.planner = None
    assert service.plan_and_run(suite.card("p2")).route == "escalated"
    assert not stack.db.execute("SELECT 1 FROM autonomous_runs").fetchall()
    stack.close()


def test_template_errors_escalate(tmp_path):
    with pytest.raises(ValueError):
        fill_template(suite.TEMPLATE, suite.card("p", datasets={"a": "1", "b": "1"}))
    bad = dict(suite.TEMPLATE, calls=[dict(c, method="shell") for c in suite.TEMPLATE["calls"]])
    stack, service = _service(tmp_path, policy=replace(POLICY, plan_templates=(bad,)))
    outcome = service.plan_and_run(suite.card("p1"), objective_key="momentum_oos")
    assert outcome.route == "escalated" and service.planner.plugs[0].calls == 0
    stack.close()


def test_catalog_view_shows_only_card_pinned_datasets():
    view = catalog_view(suite.card("p"), CATALOG, POLICY)
    assert [d["ref"] for d in view["datasets"]] == ["demo_prices@1"]
    assert catalog_view(suite.card("p", datasets={"other": "1"}), CATALOG, POLICY)["datasets"] == []
    assert set(view["methods"]) == POLICY.planner_methods and view["limits"]["max_plan_calls"] == 12


# --- exit test ----------------------------------------------------------------------------------------------

def test_phase9_exit_matches_the_baseline():
    """Fails on regressions and on improvements: a human re-records with --write-baseline."""
    doc = suite.document()
    baseline = suite.load_baseline()
    assert doc == baseline
    s = doc["summary"]
    assert s["criteria_passed"] == s["criteria_total"]
    assert s["plans_executed_after_failed_check"] == 0 and s["llm_calls_on_templates"] == 0
    assert s["escalations_with_executed_calls"] == 0
