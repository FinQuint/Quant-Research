"""Citation versions in the request's tag form ("v2") resolve to the stored version ("2"), and the
tag form never lets a citation pass that the stored form would fail. Found by the first live
Claude context sweep: every citation copied the tag's "v2" and failed as "no such evidence"."""
from finquint.orchestration.context import REPORT_SCHEMA, Evidence, InMemoryEvidenceStore
from finquint.orchestration.context.tags import evidence_line, parse_line, parse_ref
from finquint.orchestration.verify.citations import check_citation, check_citations, resolve_version


def store_with(*items):
    store = InMemoryEvidenceStore()
    for item in items:
        store.put(item)
    return store


def ev(version="2", **kw):
    kw.setdefault("run_id", "r1")
    return Evidence("ev-duration", version, "duration", "4.2", **kw)


def test_tag_form_resolves_to_the_stored_version():
    store = store_with(ev("1"), ev("2"))
    assert resolve_version(store, "ev-duration", "v2") == "2"
    assert check_citation("ev-duration", "v2", store=store, run_id="r1") is None
    assert check_citation("ev-duration", "2", store=store, run_id="r1") is None


def test_the_version_the_tag_prints_is_the_one_accepted():
    store = store_with(ev("2"))
    line = evidence_line("ev-duration", "2", "r1", "duration", "4.2")
    printed = parse_line(line).positional[1]
    assert printed == "v2" and parse_ref(parse_line(line)) == ("ev-duration", "2")
    assert check_citation("ev-duration", printed, store=store, run_id="r1") is None


def test_tag_form_cannot_pass_what_the_stored_form_fails():
    superseded = store_with(ev("1"), ev("2"))
    assert "superseded by v2" in check_citation("ev-duration", "v1", store=superseded, run_id="r1")
    foreign = store_with(ev("2", run_id="other"))
    assert "belongs to run other" in check_citation("ev-duration", "v2", store=foreign, run_id="r1")
    provisional = store_with(ev("2", tier="provisional"))
    assert "provisional" in check_citation("ev-duration", "v2", store=provisional, run_id="r1")
    deprecated = store_with(ev("2"))
    deprecated.deprecate("ev-duration")
    assert "deprecated" in check_citation("ev-duration", "v2", store=deprecated, run_id="r1")


def test_offered_set_is_checked_on_the_resolved_version():
    store = store_with(ev("2"))
    assert check_citation("ev-duration", "v2", store=store, run_id="r1", offered={("ev-duration", "2")}) is None
    assert "not in the evidence" in check_citation("ev-duration", "v2", store=store, run_id="r1",
                                                   offered={("ev-other", "2")})


def test_unknown_versions_still_fail_and_only_one_v_is_stripped():
    store = store_with(ev("2"))
    for cited in ("v3", "vv2", "v", "V2", "3"):
        assert "no such evidence" in check_citation("ev-duration", cited, store=store, run_id="r1"), cited


def test_an_exact_stored_v_version_wins_over_the_stripped_form():
    store = store_with(ev("2"), Evidence("ev-duration", "v2", "duration", "4.3", run_id="r1"))
    assert resolve_version(store, "ev-duration", "v2") == "v2"


def test_check_citations_accepts_a_model_report_written_in_tag_form():
    store = store_with(ev("2"), Evidence("ev-convexity", "1", "convexity", "22.9", run_id="r1"))
    cites = [{"evidence_id": "ev-duration", "version": "v2"}, {"evidence_id": "ev-convexity", "version": "v1"}]
    assert check_citations(cites, store=store, run_id="r1") == []


def test_report_schema_tells_the_model_the_version_format():
    version = REPORT_SCHEMA["properties"]["criteria"]["items"]["properties"]["citations"]["items"]["properties"]["version"]
    assert "without the leading v" in version["description"]
