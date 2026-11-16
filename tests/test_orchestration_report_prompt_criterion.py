"""The shared report prompt and the split soak criterion, from the first live Claude soak: Claude
typed convention numbers ("per 100 of face value"), 70 of 72 reports needed a repair, and one
escalated, which the old criterion ("exactly two report escalations") could not tell apart from a
missed fault."""
import json

import pytest

from finquint.orchestration.evals import numbers, soak
from finquint.orchestration.knowledge.reporting import REPORT_SYSTEM, _repair
from finquint.orchestration.verify.paths import is_protected


def test_one_report_prompt_for_every_report_evaluator():
    assert numbers.SYSTEM is REPORT_SYSTEM and soak.SYSTEM is REPORT_SYSTEM
    assert "{{metric:ARTIFACT_ID:FIELD}}" in REPORT_SYSTEM
    assert "spelled out in words" in REPORT_SYSTEM and not any(c.isdigit() for c in REPORT_SYSTEM)


def test_repair_tells_the_model_how_to_write_a_number_that_is_not_a_metric():
    text = _repair(("untraced number '100'",)).content
    assert "untraced number '100'" in text and "spell out in words" in text


def test_prompt_and_criterion_live_in_protected_code():
    for path in ("src/finquint/orchestration/knowledge/reporting.py", "src/finquint/orchestration/evals/soak/__init__.py",
                 "configs/soak_policy.json"):
        assert is_protected(path), path


@pytest.fixture(scope="module")
def document():
    return soak.load_baseline()


def regrade(document, **changes):
    bad = json.loads(json.dumps(document))
    for path, value in changes.items():
        section, key = path.split("__")
        bad[section][key] = value
    return {n for n, ok in soak.grade(bad, soak.SoakPolicy.from_file())["criteria"].items() if not ok}


def test_fault_reports_are_checked_by_id(document):
    assert document["facts"]["fault_reports"] == {"report-soak-r29": "escalated", "report-soak-e61": "escalated"}
    missed = {"report-soak-r29": "ok", "report-soak-e61": "escalated"}
    assert regrade(document, facts__fault_reports=missed) == {"invented_and_stale_reports_escalated"}


def test_a_routine_report_escalation_fails_its_own_criterion_not_the_fault_one(document):
    """The first live Claude soak's shape: both faults escalated plus one routine report."""
    failed = regrade(document, metrics__routine_report_escalations=1,
                     facts__reports={"ok": 71, "escalated": 3})
    assert failed == {"routine_reports_not_escalated"}


def test_routine_escalation_limit_is_tunable_and_validated():
    policy = soak.SoakPolicy.from_file()
    assert policy.max_routine_report_escalations == 0
    with pytest.raises(ValueError):
        soak.SoakPolicy(72, 3, 12, 72, 1.0, max_routine_report_escalations=-1)
