"""Report golden tasks: the Phase 6 exit test.

Spec exit: "100% of reported numbers trace to artifacts on the golden suite."

Each task produces stored results through real code paths, then asks the simulated reporter for a
prose report on them:

  report_bond_risk          a real AutonomousWorkflow bond run; numbers from run:RUN/analytics
  report_backtest           a registered ExperimentRecord; numbers from experiment:ID
  report_strategy_change    two experiments plus derived changes (derived:NAME)
  fault_invented_number     the model adds a figure that is in no artifact
  fault_missing_artifact    a placeholder names an artifact that does not exist
  fault_raw_numbers         the model types numbers instead of placeholders

Two pipelines report on each task:
  unchecked   before Phase 6: the model types numbers and its text is the report, unchecked.
  provenance  knowledge.reporting.write_report: placeholders rendered from the MetricStore,
              numeral post-check, one repair, then ESCALATE.

Scores per run:
  numbers_reported   digit runs in the final report (IDs, dates and verbatim quotes excluded)
  numbers_traced     of those, rendered from an artifact
  invented_reported  of those, matching no artifact value at the shown precision
  wrong_status       1 if the run ended differently from expected (clean tasks report, fault
                     tasks must not reach a final report)

This package is part of the protected evaluator (ADR-005).
"""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile

from finquint.autonomous import AutonomousWorkflow, Call, Request, RunStore
from finquint.autonomous.adapters import default_registry
from finquint.research import ResearchStore
from finquint.research.models import ExperimentRecord

from ...context.tags import card_line, criterion_line
from ...knowledge.metrics import CompositeMetricStore, DerivedMetrics, ExperimentMetrics, RunOutputMetrics
from ...knowledge.reporting import REPORT_TEXT_SCHEMA, metric_segment, request_identifiers, write_report
from ...models.core import ModelCaller
from ...models.simulated import SimulatedReporter
from ...models.socket import ModelRequest, Segment, SegmentRole
from ...verify.numbers import Rendered, check_numerals, resolve
from ..golden import BOND, CONVENTIONS

BASELINE_FORMAT = 1
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"
PLUG = {"name": "simulated-reporter-v1", "context_window": 4096}
PIPELINES = ("unchecked", "provenance")
SYSTEM = ("You write research reports. Write every number as {{metric:ARTIFACT_ID:FIELD}} using the "
          "[metric ...] lines; never type a number yourself.")
INSTRUCTION = "Write the report: one section per acceptance criterion."
TOTAL_KEYS = ("numbers_reported", "numbers_traced", "invented_reported", "wrong_status", "model_calls")


@dataclass(frozen=True)
class ReportTask:
    name: str
    about: str
    setup: object                # directory -> (research store, metric store, [(artifact_id, field)], card)
    mode: str = "placeholder"    # how the model writes numbers under the provenance pipeline
    invent: bool = False
    expect: str = "ok"           # provenance pipeline outcome


def _card(run, goal, refs):
    return {"run": run, "goal": goal,
            "criteria": [(f"C{i + 1}", f.rsplit(".", 1)[-1], f"Report the {f.rsplit('.', 1)[-1].replace('_', ' ')}")
                         for i, (_, f) in enumerate(refs)]}


def _bond(directory):
    research = ResearchStore(Path(directory) / "research.sqlite")
    runs = RunStore(research)
    workflow = AutonomousWorkflow(runs, default_registry(), max_workers=1)
    workflow.submit(Request("report-bond", "Price and risk a 5y bond", "golden", (
        Call("research", "source_review", {"source_ids": ["conventions"]}),
        Call("analytics", "bond_analytics", BOND)), (CONVENTIONS,)))
    state = workflow.run("report-bond")
    if state["status"] != "completed":
        raise RuntimeError(f"bond run ended {state['status']}")
    refs = [("run:report-bond/analytics", f) for f in ("bond_price", "modified_duration", "dv01", "convexity")]
    return research, RunOutputMetrics(runs), refs, _card("report-bond", "Bond risk report for the 5y bond", refs)


def _experiment(experiment_id, window, sharpe, drawdown, turnover):
    return ExperimentRecord(experiment_id, "momentum-window", "golden", {"prices": "v3"}, {"window": window}, 7, {},
                            {"sharpe": sharpe, "max_drawdown": drawdown, "turnover": turnover}, "verified")


def _backtest(directory):
    research = ResearchStore(Path(directory) / "research.sqlite")
    research.register_experiment(_experiment("exp-momentum-a", 20, 1.137, 0.184, 4.2))
    refs = [("experiment:exp-momentum-a", f"metrics.{f}") for f in ("sharpe", "max_drawdown", "turnover")]
    return research, ExperimentMetrics(research), refs, _card("report-backtest", "Momentum backtest report", refs)


def _change(directory):
    research = ResearchStore(Path(directory) / "research.sqlite")
    research.register_experiment(_experiment("exp-momentum-a", 20, 1.137, 0.184, 4.2))
    research.register_experiment(_experiment("exp-momentum-b", 30, 1.291, 0.171, 3.1))
    experiments = ExperimentMetrics(research)
    derived = DerivedMetrics(experiments)
    derived.derive("sharpe_change", "difference", ("experiment:exp-momentum-b", "metrics.sharpe"),
                   ("experiment:exp-momentum-a", "metrics.sharpe"))
    derived.derive("turnover_change", "percent_change", ("experiment:exp-momentum-b", "metrics.turnover"),
                   ("experiment:exp-momentum-a", "metrics.turnover"))
    refs = [("derived:sharpe_change", "sharpe_change"), ("derived:turnover_change", "turnover_change"),
            ("experiment:exp-momentum-b", "metrics.max_drawdown")]
    return (research, CompositeMetricStore(derived, experiments), refs,
            _card("report-change", "Effect of moving the signal window", refs))


TASKS = (
    ReportTask("report_bond_risk", "A real bond run; four risk numbers from its stored output.", _bond),
    ReportTask("report_backtest", "A registered experiment's metrics.", _backtest),
    ReportTask("report_strategy_change", "Two experiments and two derived changes.", _change),
    ReportTask("fault_invented_number", "The model adds a figure that is in no artifact.", _bond,
               invent=True, expect="escalated"),
    ReportTask("fault_missing_artifact", "A placeholder names an artifact that does not exist.", _backtest,
               mode="missing", expect="escalated"),
    ReportTask("fault_raw_numbers", "The model types numbers instead of placeholders.", _change,
               mode="raw", expect="escalated"),
)


def _request(card, segment):
    criteria = "\n".join(criterion_line(cid, topic, text) for cid, topic, text in card["criteria"])
    card_text = card_line(card["run"], "report", "golden", {}, card["goal"]) + "\n" + criteria
    return ModelRequest(card["run"], "report", (
        Segment(SegmentRole.SYSTEM, SYSTEM, stable=True), Segment(SegmentRole.TASK_CARD, card_text, stable=True),
        segment, Segment(SegmentRole.VOLATILE, INSTRUCTION)), REPORT_TEXT_SCHEMA, max_output_tokens=512)


def _invented(numbers, store, refs):
    """Reported digit runs matching no artifact value at the precision shown."""
    values = [resolve(store, a, f) for a, f in refs]
    count = 0
    for number in numbers:
        text = number.text.replace(",", "")
        decimals = len(text.split(".")[1]) if "." in text else 0
        try:
            shown = abs(float(text))
        except ValueError:
            count += 1
            continue
        # digit runs carry no sign, so compare magnitudes
        if not any(abs(abs(round(v, decimals)) - shown) < 10 ** -(decimals + 3) for v in values):
            count += 1
    return count


def run_task(task, pipeline):
    if pipeline not in PIPELINES:
        raise ValueError(f"pipeline must be one of {PIPELINES}")
    with tempfile.TemporaryDirectory(prefix="finquint-report-") as directory:
        research, store, refs, card = task.setup(directory)
        try:
            request = _request(card, metric_segment(store, refs))
            if pipeline == "unchecked":
                model = SimulatedReporter(PLUG["name"], context_window=PLUG["context_window"], mode="raw",
                                          invent=task.invent)
                result = ModelCaller(model).call(request)
                text = "\n".join(f"{s['id']}: {s['text']}" for s in result.output["sections"])
                numbers = check_numerals(Rendered(text, ()), identifiers=request_identifiers(request))
                return {"status": "ok", "numbers_reported": len(numbers), "numbers_traced": 0,
                        "invented_reported": _invented(numbers, store, refs),
                        "wrong_status": int(task.expect != "ok"),   # nothing stops a faulty report
                        "model_calls": result.attempts}
            model = SimulatedReporter(PLUG["name"], context_window=PLUG["context_window"], mode=task.mode,
                                      invent=task.invent)
            result = write_report(ModelCaller(model), request, metrics=store)
            reported = len(result.numbers) if result.status == "ok" else 0
            return {"status": result.status, "numbers_reported": reported, "numbers_traced": reported,
                    "invented_reported": 0 if result.status != "ok" else _invented(result.numbers, store, refs),
                    "wrong_status": int(result.status != task.expect), "model_calls": result.model_calls}
        finally:
            research.close()


def run_suite(tasks=TASKS):
    return {task.name: {p: run_task(task, p) for p in PIPELINES} for task in tasks}


def totals(results, pipeline):
    return {k: sum(r[pipeline][k] for r in results.values()) for k in TOTAL_KEYS}


def traced_share(results, pipeline):
    t = totals(results, pipeline)
    return 1.0 if t["numbers_reported"] == 0 else round(t["numbers_traced"] / t["numbers_reported"], 6)


def baseline_document(results):
    return {"format": BASELINE_FORMAT, "plug": dict(PLUG), "tasks": results,
            "totals": {p: totals(results, p) for p in PIPELINES},
            "traced_share": {p: traced_share(results, p) for p in PIPELINES}}


def load_baseline(path=BASELINE_PATH):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != BASELINE_FORMAT:
        raise ValueError(f"unsupported report baseline format: {data.get('format')}")
    return data


def write_baseline(path=BASELINE_PATH):
    """Humans only."""
    document = baseline_document(run_suite())
    Path(path).write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return document


__all__ = ["BASELINE_PATH", "PIPELINES", "TASKS", "ReportTask", "baseline_document", "load_baseline", "run_suite",
           "run_task", "totals", "traced_share", "write_baseline"]
