"""Planning golden objectives and faults (roadmap Phase 9 exit test, ADR-022).

Each task runs a host Stack with the planning layer, a deterministic SimulatedPlanner (scripted
plans) and SimulatedPlanReviewer, on the pinned demo dataset (configs/datasets.json), and is scored
only from durable state: planning_* tables, autonomous_runs, approval requests and the audit log.

Exit criteria (spec, Phase 9):
  * 0 plans that failed a check executed (every executed plan's last attempt passed, or is a template);
  * in-catalog objectives complete with every criterion covered;
  * an out-of-catalog objective escalates with 0 calls executed;
  * a plan that tunes on the test window, uses an unpinned or tampered dataset, misses a criterion,
    exceeds its budget, uses an unknown method or names a path is rejected and either repaired within
    2 attempts or escalated;
  * a template-matched objective runs with 0 LLM calls; a plan the reviewer rejects never runs;
  * repeated accepted plans for one objective become a template proposal for a human.

    python -m finquint.orchestration.evals.planning                   # compare with the baseline
    python -m finquint.orchestration.evals.planning --write-baseline  # humans only
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from ...context.task_card import AcceptanceCriterion, TaskCard
from ...host import Stack
from ...models.core import ModelCaller
from ...planning.catalog import CONFIG_PATH as DATASETS_PATH, FileDatasetCatalog
from ...planning.methods import planning_registry
from ...planning.planner import PlanningService
from ...planning.simulated import SimulatedPlanner, SimulatedPlanReviewer
from ...verify.plans import PlanningPolicy

REPO_ROOT = Path(__file__).resolve().parents[5]
APPROVAL_POLICY_PATH = REPO_ROOT / "configs" / "approval_policy.json"
PLANNING_POLICY_PATH = REPO_ROOT / "configs" / "planning_policy.json"
BASELINE_PATH = Path(__file__).with_name("baseline.json")
DATASET = "demo_prices@1"
PROFILE = "dataset-demo_prices-1"


def card(run_id, *, budget=None, datasets=None):
    return TaskCard(run_id, "plan", "Does time-series momentum on DEMO hold up out of sample?",
                    (AcceptanceCriterion("c_data", "Use the pinned demo_prices dataset.", "data"),
                     AcceptanceCriterion("c_oos", "Tune in-sample and evaluate out of sample.", "oos"),
                     AcceptanceCriterion("c_metrics", "Report the Sharpe ratio and maximum drawdown.", "metrics")),
                    "sha-planning-eval", {"demo_prices": "1"} if datasets is None else datasets,
                    budget_limits={} if budget is None else {"plan_cost": budget})


def call(task_id, method, parameters, deps=(), criteria=("c_oos",)):
    return {"task_id": task_id, "method": method, "parameters": parameters, "dependencies": list(deps),
            "criteria": list(criteria), "rationale": f"{method} step"}


def good_plan(grid=(20, 60, 120), dataset=DATASET):
    return {"calls": [
        call("profile", "source_review", {"source_ids": [PROFILE]}, criteria=("c_data",)),
        call("search", "parameter_search", {"dataset": dataset, "strategy": "ts_momentum", "window": "train",
                                            "grid": {"lookback": list(grid)}}, ("profile",)),
        call("oos", "backtest", {"dataset": dataset, "strategy": "ts_momentum", "window": "test",
                                 "params_from": "search"}, ("search",)),
        call("perf", "performance_summary", {"source_task": "oos"}, ("oos",), ("c_metrics",))]}


def _with(plan, task_id, **changes):
    out = json.loads(json.dumps(plan))
    for c in out["calls"]:
        if c["task_id"] == task_id:
            for key, value in changes.items():
                if key == "parameters":
                    c["parameters"].update(value)
                else:
                    c[key] = value
    return out


TEST_TUNING = _with(good_plan(), "search", parameters={"window": "test"})
TUNE_AFTER_TEST = {"calls": [
    call("profile", "source_review", {"source_ids": [PROFILE]}, criteria=("c_data",)),
    call("peek", "backtest", {"dataset": DATASET, "strategy": "ts_momentum", "window": "test",
                              "params": {"lookback": 60}}, ("profile",)),
    call("search", "parameter_search", {"dataset": DATASET, "strategy": "ts_momentum", "window": "train",
                                        "grid": {"lookback": [20, 60]}}, ("peek",)),
    call("oos", "backtest", {"dataset": DATASET, "strategy": "ts_momentum", "window": "test",
                             "params_from": "search"}, ("search",)),
    call("perf", "performance_summary", {"source_task": "oos"}, ("oos",), ("c_metrics",))]}
UNPINNED = _with(good_plan(), "search", parameters={"dataset": "demo_prices"})
MISSING_CRITERION = {"calls": good_plan()["calls"][:3]}
UNKNOWN_METHOD = {"calls": good_plan()["calls"] + [call("sh", "shell", {"cmd": "ls"}, ("perf",), ("c_metrics",))]}
PATH_PARAM = _with(good_plan(), "profile", parameters={"source_ids": ["configs/approval_policy.json"]})
BIG_GRID = good_plan(grid=tuple(range(10, 210, 10)))
SMALL_GRID = good_plan(grid=(20, 60))
TEMPLATE = {"key": "momentum_oos", "calls": [
    {**call("profile", "source_review", {"source_ids": [PROFILE]}), "criteria": "all"},
    {**call("search", "parameter_search", {"dataset": "{dataset}", "strategy": "ts_momentum", "window": "train",
                                           "grid": {"lookback": [20, 60, 120]}}, ("profile",)), "criteria": "all"},
    {**call("oos", "backtest", {"dataset": "{dataset}", "strategy": "ts_momentum", "window": "test",
                                "params_from": "search"}, ("search",)), "criteria": "all"},
    {**call("perf", "performance_summary", {"source_task": "oos"}, ("oos",)), "criteria": "all"}]}


def _policy(**changes):
    return replace(PlanningPolicy.from_file(PLANNING_POLICY_PATH), **changes)


def _setup(directory, *, script, reject=False, policy=None, catalog=None):
    catalog = catalog or FileDatasetCatalog.from_file(DATASETS_PATH)
    stack = Stack(directory, approval_key=b"planning-eval-approval-key-00000", capability_key=b"planning-eval-cap-key-0000000000",
                  approval_policy=APPROVAL_POLICY_PATH, registry=planning_registry(catalog))
    service = PlanningService(stack, catalog=catalog, policy=policy or _policy(),
                              planner=ModelCaller(SimulatedPlanner(script)),
                              reviewer=ModelCaller(SimulatedPlanReviewer(reject=reject)))
    return stack, service


def _single(script, *, reject=False, policy=None, catalog=None, card_kw=None, key=None):
    def task(directory):
        stack, service = _setup(directory, script=script, reject=reject, policy=policy,
                                catalog=catalog() if callable(catalog) else catalog)
        service.plan_and_run(card("p1", **(card_kw or {})), objective_key=key)
        return stack, ["p1"]
    return task


def _tampered():
    data = json.loads(Path(DATASETS_PATH).read_text(encoding="utf-8"))
    data["datasets"][0]["sha256"] = "0" * 64
    return FileDatasetCatalog.from_dict(data)


def _templates_learned(directory):
    stack, service = _setup(directory, script=[good_plan()])
    for i in range(3):
        service.plan_and_run(card(f"p{i}"), objective_key="momentum_oos")
    return stack, ["p0", "p1", "p2"]


TASKS = {
    "planner_plan_runs": (_single([good_plan()]), "ran"),
    "template_runs_without_model": (_single([{"escalate": "unused"}], policy=_policy(plan_templates=(TEMPLATE,)),
                                            key="momentum_oos"), "template"),
    "out_of_catalog_escalates": (_single([{"escalate": "the card pins fx_rates@1, which is not in the catalog"}],
                                         card_kw={"datasets": {"fx_rates": "1"}}), "escalated"),
    "test_window_tuning_repaired": (_single([TEST_TUNING, good_plan()]), "repaired"),
    "tuning_after_test_escalates": (_single([TUNE_AFTER_TEST]), "escalated"),
    "unpinned_dataset_repaired": (_single([UNPINNED, good_plan()]), "repaired"),
    "missing_criterion_escalates": (_single([MISSING_CRITERION]), "escalated"),
    "over_budget_repaired": (_single([BIG_GRID, SMALL_GRID], card_kw={"budget": 10}), "repaired"),
    "unknown_method_repaired": (_single([UNKNOWN_METHOD, good_plan()]), "repaired"),
    "path_parameter_escalates": (_single([PATH_PARAM]), "escalated"),
    "tampered_dataset_escalates": (_single([good_plan()], catalog=_tampered), "escalated"),
    "reviewer_rejects": (_single([good_plan()], reject=True), "escalated"),
    "planner_schema_fault": (_single([{"raw": "I would backtest momentum."}]), "escalated"),
    "repeated_plans_become_template": (_templates_learned, "learned"),
}


# --- scoring from durable state ---------------------------------------------------------------------

def measure(db, run_id):
    row = db.execute("SELECT status, source, run_route, model_calls, request_id FROM planning_runs WHERE run_id=?",
                     (run_id,)).fetchone()
    attempts = db.execute("SELECT outcome, problems FROM planning_attempts WHERE run_id=? ORDER BY attempt",
                          (run_id,)).fetchall()
    reviews = [r[0] for r in db.execute("SELECT verdict FROM planning_reviews WHERE run_id=? ORDER BY seq", (run_id,))]
    executed = db.execute("SELECT payload FROM autonomous_runs WHERE run_id=?", (run_id,)).fetchone()
    request = db.execute("SELECT status FROM approval_requests WHERE request_id=?", (row[4],)).fetchone() if row and row[4] else None
    problems = [json.loads(a[1]) for a in attempts]
    return {"status": row[0] if row else None, "source": row[1] if row else None, "run_route": row[2] if row else None,
            "model_calls": row[3] if row else 0, "attempt_outcomes": [a[0] for a in attempts],
            "problem_kinds": sorted({p.split(":")[-1].strip().split(" ")[0] for ps in problems for p in ps})[:6],
            "reviews": reviews, "executed": executed is not None,
            "executed_status": json.loads(executed[0])["status"] if executed else None,
            "executed_after_failed_check": executed is not None and bool(attempts) and bool(problems[-1]),
            "approval_request": request[0] if request else None,
            "template_proposals": db.execute("SELECT COUNT(*) FROM planning_template_proposals").fetchone()[0]}


def checks(kind, m):
    out = {"no_failed_plan_executed": not m["executed_after_failed_check"]}
    if kind in ("ran", "repaired"):
        out["completed"] = m["status"] == "ran" and m["run_route"] == "completed" and m["executed_status"] == "completed"
        out["reviewed_before_run"] = m["reviews"] == ["accept"]
        out["attempts_within_budget"] = len(m["attempt_outcomes"]) <= 2
        if kind == "repaired":
            out["first_attempt_rejected"] = m["attempt_outcomes"][:1] == ["rejected"]
    elif kind == "template":
        out["completed"] = m["status"] == "ran" and m["source"] == "template" and m["run_route"] == "completed"
        out["no_llm_calls"] = m["model_calls"] == 0
    elif kind == "escalated":
        out["escalated_to_human"] = m["status"] == "escalated" and m["approval_request"] == "pending"
        out["nothing_executed"] = not m["executed"]
    elif kind == "learned":
        out["completed"] = m["run_route"] == "completed"
        out["template_proposed"] = m["template_proposals"] == 1
    return out


def run_task(name):
    function, kind = TASKS[name]
    with tempfile.TemporaryDirectory() as directory:
        stack = None
        try:
            stack, runs = function(directory)
            m = measure(stack.db, runs[-1])
        finally:
            if stack is not None:
                stack.close()
    c = checks(kind, m)
    return {"kind": kind, "checks": c, "passed": sum(c.values()), "total": len(c), "metrics": m}


def run_suite():
    return {name: run_task(name) for name in TASKS}


def summary(results):
    return {"criteria_passed": sum(r["passed"] for r in results.values()),
            "criteria_total": sum(r["total"] for r in results.values()),
            "plans_executed_after_failed_check": sum(r["metrics"]["executed_after_failed_check"] for r in results.values()),
            "llm_calls_on_templates": sum(r["metrics"]["model_calls"] for r in results.values() if r["kind"] == "template"),
            "escalations_with_executed_calls": sum(r["metrics"]["executed"] for r in results.values()
                                                   if r["kind"] == "escalated"),
            "model_calls_total": sum(r["metrics"]["model_calls"] for r in results.values())}


def document(results=None):
    results = results or run_suite()
    return {"tasks": results, "summary": summary(results)}


def load_baseline(path=BASELINE_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_baseline(path=BASELINE_PATH):
    doc = document()
    Path(path).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return doc
