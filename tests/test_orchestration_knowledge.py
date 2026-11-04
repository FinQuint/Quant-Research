"""Phase 6: number provenance, metric stores, tiers and promotion, code index, event re-indexing,
the opt-in workflow hook, and the exit test on the report golden tasks."""
import json
import sqlite3

import pytest

from finquint.autonomous import AutonomousWorkflow, Call, Request, RunStore
from finquint.autonomous.adapters import default_registry
from finquint.orchestration.context import Evidence, InMemoryEvidenceStore, SQLiteEvidenceStore
from finquint.orchestration.evals import numbers as reports
from finquint.orchestration.evals.golden import BOND, CONVENTIONS
from finquint.orchestration.knowledge import (Claim, CodeIndex, CompositeMetricStore, DerivedMetrics, DictMetricStore,
                                              EvidenceMetrics, ExperimentMetrics, Ingestor, RunOutputMetrics,
                                              demote_superseded, promote, write_report)
from finquint.orchestration.knowledge.reporting import REPORT_TEXT_SCHEMA, metric_segment
from finquint.orchestration.models import Capabilities, ModelCaller, ModelReply, ModelRequest, Segment, SegmentRole
from finquint.orchestration.models.socket import SOCKET_VERSION, Usage
from finquint.orchestration.verify.numbers import RenderError, check_numerals, render
from finquint.orchestration.verify.paths import is_protected
from finquint.research import ResearchStore
from finquint.research.models import ExperimentRecord

METRICS = DictMetricStore({"run:r1/analytics": {"bond_price": 104.4912925, "count": 3, "flag": True,
                                                "nested": {"dv01": 0.0460879}}})


# --- rendering and the numeral post-check -----------------------------------------------------------

def test_render_formats_and_records_every_number():
    rendered = render("Price {{metric:run:r1/analytics:bond_price|.2f}}, dollar duration "
                      "{{metric:run:r1/analytics:nested.dv01}} over {{metric:run:r1/analytics:count|,d}} runs.", METRICS)
    assert rendered.text == "Price 104.49, dollar duration 0.0460879 over 3 runs."
    assert [n.text for n in rendered.numbers] == ["104.49", "0.0460879", "3"]
    assert check_numerals(rendered) == []


def test_render_fails_closed():
    with pytest.raises(RenderError) as exc:
        render("{{metric:run:r1/analytics:missing}} and {{metric:run:r9/x:bond_price}}", METRICS)
    assert len(exc.value.errors) == 2
    with pytest.raises(RenderError):
        render("{{metric:run:r1/analytics:bond_price|*2}}", METRICS)       # format only, no arithmetic
    with pytest.raises(RenderError):
        render("{{metric:run:r1/analytics:flag}}", METRICS)                # booleans are not numbers
    with pytest.raises(RenderError):
        render("{{metric:run:r1/analytics:bond_price + 1}}", METRICS)      # malformed placeholder left over


def test_numeral_exemptions_are_narrow():
    rendered = render("C1 on 2026-11-04 quotes \"coupon 5% paid twice\": {{metric:run:r1/analytics:count}}", METRICS)
    assert check_numerals(rendered, identifiers={"C1"}, inputs=["The coupon 5% paid twice a year."]) == []
    assert [u.text for u in check_numerals(rendered, identifiers={"C1"})] == ["5"]          # quote not from an input
    sloppy = render("A 5y bond, about 15 percent cheaper, cited as C1.", METRICS)
    assert [u.text for u in check_numerals(sloppy, identifiers={"C1"})] == ["5", "15"]


# --- metric stores ---------------------------------------------------------------------------------

def _research(tmp_path):
    return ResearchStore(tmp_path / "research.sqlite")


def _experiment(experiment_id, sharpe):
    return ExperimentRecord(experiment_id, "h1", "sha", {"prices": "v3"}, {"window": 20}, 1, {},
                            {"sharpe": sharpe}, "verified")


def test_run_and_experiment_metrics_read_existing_stores(tmp_path):
    research = _research(tmp_path)
    runs = RunStore(research)
    workflow = AutonomousWorkflow(runs, default_registry(), max_workers=1)
    workflow.submit(Request("m1", "Price a bond", "sha", (Call("research", "source_review", {"source_ids": ["conventions"]}),
                                                          Call("analytics", "bond_analytics", BOND)), (CONVENTIONS,)))
    workflow.run("m1")
    store = RunOutputMetrics(runs)
    assert store.get("run:m1/analytics")["bond_price"] == pytest.approx(104.4912925)
    assert store.get("run:m1/nope") is None and store.get("run:other/analytics") is None
    research.register_experiment(_experiment("e1", 1.2))
    assert ExperimentMetrics(research).get("experiment:e1")["metrics"]["sharpe"] == 1.2
    research.close()


def test_evidence_metrics_refuse_untrusted_tiers():
    store = InMemoryEvidenceStore([Evidence("ev-a", "1", "duration", "x", claims={"duration": "4.41"}),
                                   Evidence("ev-b", "1", "duration", "x", tier="provisional", claims={"duration": "9"})])
    metrics = EvidenceMetrics(store)
    assert metrics.get("evidence:ev-a@1") == {"duration": 4.41}
    assert metrics.get("evidence:ev-b@1") is None


def test_derived_metrics_are_computed_once_by_a_fixed_method():
    source = DictMetricStore({"experiment:a": {"sharpe": 1.0}, "experiment:b": {"sharpe": 1.3}})
    derived = DerivedMetrics(source)
    artifact = derived.derive("gain", "difference", ("experiment:b", "sharpe"), ("experiment:a", "sharpe"))
    assert derived.get(artifact)["gain"] == pytest.approx(0.3)
    assert derived.get(artifact)["inputs"] == ["experiment:b:sharpe", "experiment:a:sharpe"]
    with pytest.raises(ValueError):
        derived.derive("gain", "difference", ("experiment:b", "sharpe"), ("experiment:a", "sharpe"))
    with pytest.raises(ValueError):
        derived.derive("x", "exp", ("experiment:b", "sharpe"), ("experiment:a", "sharpe"))
    combined = CompositeMetricStore(derived, source)
    assert render("{{metric:derived:gain:gain|.1f}} vs {{metric:experiment:a:sharpe}}", combined).text == "0.3 vs 1.0"


# --- tiers and promotion --------------------------------------------------------------------------

def _claim(template="Duration is {{metric:evidence:ev-dur@1:duration|.2f}} years.", cites=(("ev-dur", "1"),)):
    return Claim("claim-dur", "1", "duration", template, cites, run_id="r1", claims={"duration": "4.41"})


def _trusted_store():
    return InMemoryEvidenceStore([Evidence("ev-dur", "1", "duration", "risk run", run_id="r1",
                                           claims={"duration": "4.4107"})])


def test_promotion_requires_traced_numbers_and_trusted_citations():
    store = _trusted_store()
    result = promote(_claim(), store=store, metrics=EvidenceMetrics(store))
    assert result.promoted and store.current("claim-dur").tier == "verified"
    assert store.current("claim-dur").supports == (("ev-dur", "1"),) and "4.41" in store.current("claim-dur").content
    store = _trusted_store()
    assert not promote(_claim("Duration is 4.41 years."), store=store, metrics=EvidenceMetrics(store)).promoted
    assert not promote(_claim(cites=()), store=store, metrics=EvidenceMetrics(store)).promoted
    store.put(Evidence("ev-guess", "1", "duration", "agent summary", run_id="r1", tier="provisional"))
    reasons = promote(_claim(cites=(("ev-guess", "1"),)), store=store, metrics=EvidenceMetrics(store)).reasons
    assert any("provisional" in r for r in reasons)


def test_verified_knowledge_is_demoted_when_its_source_changes():
    store = _trusted_store()
    promote(_claim(), store=store, metrics=EvidenceMetrics(store))
    assert demote_superseded(store) == []
    store.put(Evidence("ev-dur", "2", "duration", "risk re-run", run_id="r1", claims={"duration": "4.42"}))
    demoted = demote_superseded(store)
    assert demoted == [("claim-dur", "1.verified.demoted")]
    assert store.current("claim-dur").tier == "provisional"
    assert store.get("claim-dur", "1.verified").tier == "verified"          # old version kept


# --- code index and re-indexing -------------------------------------------------------------------

def _package(root, body):
    pkg = root / "pkg"
    pkg.mkdir(exist_ok=True)
    (pkg / "mod.py").write_text(body, encoding="utf-8")
    (pkg / "other.py").write_text("def stable():\n    return 1\n", encoding="utf-8")


def test_code_index_keeps_exact_text_per_sha(tmp_path):
    _package(tmp_path, 'class Pricer:\n    """Prices bonds."""\n    def price(self, y):\n        return 100 - y\n')
    index = CodeIndex.build(tmp_path, "a" * 40, package="pkg")
    method = index.get("pkg.mod.Pricer.price")
    assert method.text == "def price(self, y):\n        return 100 - y" and (method.start, method.end) == (3, 4)
    assert index.search("prices bonds")[0].qualname == "pkg.mod.Pricer"
    _package(tmp_path, 'class Pricer:\n    def price(self, y):\n        return 101 - y\n')
    newer = CodeIndex.build(tmp_path, "b" * 40, package="pkg")
    assert newer.changed_files(index) == ["pkg/mod.py"]
    assert {e.version for e in newer.evidence()} == {"b" * 12}


def test_code_events_add_changed_code_and_mark_summaries_stale(tmp_path):
    _package(tmp_path, "def price(y):\n    return 100 - y\n")
    db = sqlite3.connect(":memory:")
    store = SQLiteEvidenceStore(db)
    ingest = Ingestor(store, db, code_root=tmp_path, package="pkg")
    first = ingest.on_code_sha("a" * 40)
    assert len(first.added) == 2 and ingest.on_code_sha("a" * 40).skipped
    store.put(Evidence("sum-price", "1", "pricing", "summary of price()", tier="verified", source="file:pkg/mod.py"))
    _package(tmp_path, "def price(y):\n    return 101 - y\n")
    second = Ingestor(store, db, code_root=tmp_path, package="pkg").on_code_sha("b" * 40)   # state survives restart
    assert second.changed_files == ("pkg/mod.py",)
    assert second.added == (("code-pkg-mod-price", "b" * 12),)
    assert ("sum-price", "1.stale") in second.demoted and store.current("sum-price").tier == "provisional"
    assert store.get("code-pkg-mod-price", "a" * 12) is not None                                # old version kept


def test_experiment_and_dataset_events():
    db = sqlite3.connect(":memory:")
    store = SQLiteEvidenceStore(db)
    ingest = Ingestor(store, db)
    report = ingest.on_experiment_verified(_experiment("e7", 1.4))
    item = store.current("experiment-e7")
    assert report.added == (("experiment-e7", "1"),) and item.dataset == "prices@v3" and item.claims["sharpe"] == "1.4"
    store.put(Evidence("finding", "1", "sharpe", "on v3", tier="verified", dataset="prices@v3"))
    assert ingest.on_dataset_version("prices", "v3").demoted == ()
    moved = ingest.on_dataset_version("prices", "v4")
    assert ("finding", "1.stale") in moved.demoted and ingest.on_dataset_version("prices", "v4").skipped


def _bond_run(workflow, run_id="k1"):
    workflow.submit(Request(run_id, "Price a bond", "sha", (Call("research", "source_review", {"source_ids": ["conventions"]}),
                                                            Call("analytics", "bond_analytics", BOND)), (CONVENTIONS,)))
    return workflow.run(run_id)


def test_workflow_hook_is_opt_in_and_never_changes_the_outcome(tmp_path):
    research = _research(tmp_path)
    plain = _bond_run(AutonomousWorkflow(RunStore(research), default_registry(), max_workers=1), "k0")
    assert plain["status"] == "completed"
    assert not [e for e in research.audit_log() if e["event_type"].startswith("knowledge")]
    store = SQLiteEvidenceStore(research.connection)
    hooked = AutonomousWorkflow(RunStore(research), default_registry(), max_workers=1,
                                knowledge=Ingestor(store, research.connection))
    assert _bond_run(hooked)["status"] == "completed"
    assert store.current("run-k1-analytics").source == "run:k1/analytics"

    class Broken:
        def on_run_completed(self, state):
            raise RuntimeError("index offline")
    broken = AutonomousWorkflow(RunStore(research), default_registry(), max_workers=1, knowledge=Broken())
    assert _bond_run(broken, "k2")["status"] == "completed"
    assert any(e["event_type"] == "knowledge_ingest_failed" for e in research.audit_log())
    research.close()


# --- report writing --------------------------------------------------------------------------------

class Scripted:
    socket_version = SOCKET_VERSION

    def __init__(self, texts):
        self.texts, self.name = list(texts), "scripted"
        self.capabilities = Capabilities(context_window=4096)

    def complete(self, request):
        text = self.texts.pop(0)
        return ModelReply(json.dumps({"run_id": "r1", "sections": [{"id": "C1", "text": text}]}), "scripted", Usage(1, 1))


def _report_request():
    segment = metric_segment(METRICS, [("run:r1/analytics", "bond_price")])
    card = Segment(SegmentRole.TASK_CARD, "[criterion C1 topic=bond_price] Report the price")
    return ModelRequest("r1", "report", (Segment(SegmentRole.SYSTEM, "s"), card, segment,
                                         Segment(SegmentRole.VOLATILE, "go")), REPORT_TEXT_SCHEMA, 128)


def test_write_report_repairs_once_then_escalates():
    good = "Price {{metric:run:r1/analytics:bond_price|.2f}}."
    result = write_report(ModelCaller(Scripted(["Price 104.49.", good])), _report_request(), metrics=METRICS)
    assert result.status == "ok" and result.model_calls == 2 and result.text == "C1: Price 104.49."
    stuck = write_report(ModelCaller(Scripted(["Price 104.49.", "Price 104.5."])), _report_request(), metrics=METRICS)
    assert stuck.status == "escalated" and stuck.text == "" and "untraced number '104.5'" in stuck.problems


# --- exit test ---------------------------------------------------------------------------------------

def test_exit_every_reported_number_traces():
    baseline = reports.load_baseline()
    results = reports.run_suite()
    assert results == baseline["tasks"]
    assert reports.traced_share(results, "provenance") == 1.0
    provenance = reports.totals(results, "provenance")
    assert provenance["wrong_status"] == 0 and provenance["invented_reported"] == 0 and provenance["numbers_reported"] > 0
    for task in reports.TASKS:
        assert results[task.name]["provenance"]["status"] == task.expect
    unchecked = reports.totals(results, "unchecked")
    assert reports.traced_share(results, "unchecked") < 1.0 and unchecked["invented_reported"] > 0
    assert unchecked["wrong_status"] == sum(1 for t in reports.TASKS if t.expect != "ok")


def test_report_baseline_and_number_checks_are_protected():
    assert is_protected("src/finquint/orchestration/evals/numbers/baseline.json")
    assert is_protected("src/finquint/orchestration/verify/numbers.py")


# --- review regressions ---------------------------------------------------------------------------

def test_hook_leaves_no_open_transaction_between_runs(tmp_path):
    research = _research(tmp_path)
    store = SQLiteEvidenceStore(research.connection)
    workflow = AutonomousWorkflow(RunStore(research), default_registry(), max_workers=1,
                                  knowledge=Ingestor(store, research.connection))
    assert _bond_run(workflow, "t1")["status"] == "completed"
    assert not research.connection.in_transaction
    assert _bond_run(workflow, "t2")["status"] == "completed"
    research.close()


def test_bare_versions_and_numeric_ids_are_not_exempt():
    from finquint.orchestration.knowledge import request_identifiers
    req = ModelRequest("r1", "t1", (Segment(SegmentRole.EVIDENCE, "[evidence ev-7 v2 run=42 topic=x] text"),),
                       REPORT_TEXT_SCHEMA, 64)
    ids = request_identifiers(req)
    assert "v2" in ids and "ev-7" in ids and "2" not in ids and "42" not in ids
    rendered = render("Returns rose 2 percent in run 42.", METRICS)
    assert [u.text for u in check_numerals(rendered, identifiers=ids)] == ["2", "42"]


def test_short_or_numeric_quotes_cannot_borrow_digits():
    rendered = render('It was "15" pct, and "rate 0.15" moved.', METRICS)
    assert [u.text for u in check_numerals(rendered, inputs=["rate 0.154 in 2015"])] == ["15", "0.15"]


@pytest.mark.parametrize("make", [lambda: InMemoryEvidenceStore(), lambda: SQLiteEvidenceStore(sqlite3.connect(":memory:"))])
def test_reputting_an_old_version_makes_it_current(make):
    store = make()
    store.put(Evidence("code-f", "aaa", "code", "v-a"))
    store.put(Evidence("code-f", "bbb", "code", "v-b"))
    store.put(Evidence("code-f", "aaa", "code", "v-a"))          # revert to SHA A
    assert store.current("code-f").version == "aaa"


def test_a_promoted_version_is_never_rewritten():
    store = _trusted_store()
    assert promote(_claim(), store=store, metrics=EvidenceMetrics(store)).promoted
    again = promote(_claim(), store=store, metrics=EvidenceMetrics(store))
    assert not again.promoted and "already promoted" in again.reasons[0]


def test_deleted_code_is_deprecated_and_claims_on_it_demote(tmp_path):
    _package(tmp_path, "def price(y):\n    return 100 - y\n\ndef gone():\n    return 0\n")
    db = sqlite3.connect(":memory:")
    store = SQLiteEvidenceStore(db)
    ingest = Ingestor(store, db, code_root=tmp_path, package="pkg")
    ingest.on_code_sha("a" * 40)
    store.put(Evidence("claim-gone", "1.verified", "x", "gone() returns zero", tier="verified",
                       supports=[("code-pkg-mod-gone", "a" * 12)]))
    _package(tmp_path, "def price(y):\n    return 100 - y\n")
    report = ingest.on_code_sha("b" * 40)
    assert ("code-pkg-mod-gone", "a" * 12) in report.deprecated and store.current("code-pkg-mod-gone").deprecated
    assert store.current("claim-gone").tier == "provisional"


def test_metric_sources_only_serve_verified_artifacts(tmp_path):
    store = _trusted_store()
    promote(_claim(), store=store, metrics=EvidenceMetrics(store))
    metrics = EvidenceMetrics(store)
    assert metrics.get("evidence:claim-dur@1.verified") is not None
    store.put(Evidence("ev-dur", "2", "duration", "re-run", run_id="r1", claims={"duration": "4.42"}))
    demote_superseded(store)
    assert metrics.get("evidence:claim-dur@1.verified") is None             # demoted: no longer citable
    research = _research(tmp_path)
    research.register_experiment(ExperimentRecord("draft", "h1", "sha", {"prices": "v3"}, {}, 1, {}, {"sharpe": 9.0}))
    assert ExperimentMetrics(research).get("experiment:draft") is None        # status "registered"
    research.close()


def test_run_outputs_need_a_completed_run(tmp_path):
    research = _research(tmp_path)
    runs = RunStore(research)
    from dataclasses import replace
    from finquint.autonomous import MethodRegistry
    base = default_registry()

    def broken(parameters, context):
        raise RuntimeError("pricing engine crashed")
    registry = (MethodRegistry().register(base.get("source_review"))
                .register(replace(base.get("bond_analytics"), execute=broken)))
    workflow = AutonomousWorkflow(runs, registry, max_workers=1)
    workflow.submit(Request("f1", "Price", "sha", (Call("research", "source_review", {"source_ids": ["conventions"]}),
                                                   Call("analytics", "bond_analytics", BOND)), (CONVENTIONS,)))
    state = workflow.run("f1")
    assert state["status"] != "completed" and state["tasks"]["research"]["status"] == "succeeded"
    assert RunOutputMetrics(runs).get("run:f1/research") is None
    research.close()


def test_code_index_handles_bom_bad_bytes_init_names_and_duplicates(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_bytes(b"\xef\xbb\xbfdef top():\n    return 1\n")
    (pkg / "latin.py").write_bytes(b"x = '\xe9'\n")
    (pkg / "props.py").write_text("class A:\n    @property\n    def v(self):\n        return 1\n"
                                  "    @v.setter\n    def v(self, x):\n        pass\n", encoding="utf-8")
    index = CodeIndex.build(tmp_path, "c" * 40, package="pkg")
    assert index.get("pkg.top") is not None
    assert "pkg/latin.py" in index.file_hashes
    assert index.get("pkg.props.A.v") and index.get("pkg.props.A.v#2")


def test_derive_rejects_reserved_names_and_zero_division():
    derived = DerivedMetrics(DictMetricStore({"experiment:a": {"x": 0.0}, "experiment:b": {"x": 1.0}}))
    with pytest.raises(ValueError):
        derived.derive("value", "difference", ("experiment:b", "x"), ("experiment:a", "x"))
    with pytest.raises(ValueError):
        derived.derive("r", "ratio", ("experiment:b", "x"), ("experiment:a", "x"))


def test_knowledge_gates_are_protected():
    assert is_protected("src/finquint/orchestration/knowledge/evidence_store.py")
    assert is_protected("src/finquint/orchestration/knowledge/reporting.py")
