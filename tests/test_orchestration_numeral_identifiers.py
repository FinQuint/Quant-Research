"""Identifiers in the numeral check match ignoring case (found by the live Claude soak: every repair
was for the "01" in the label "DV01" while the field name "dv01" was exempt). The exemption still
covers only tokens equal to an identifier from the request; no bare or invented number passes."""
from finquint.orchestration.verify.numbers import Rendered, check_numerals


def untraced(text, identifiers):
    return [u.text for u in check_numerals(Rendered(text, ()), identifiers=identifiers)]


def test_an_identifier_in_another_case_is_exempt():
    assert untraced("DV01: the dollar value of a basis point.", {"dv01"}) == []
    assert untraced("Dv01 and dv01.", {"dv01"}) == []
    assert untraced("Run SOAK-R29 completed.", {"soak-r29"}) == []


def test_only_tokens_equal_to_an_identifier_are_exempt():
    assert untraced("DV02 rose.", {"dv01"}) == ["02"]
    assert untraced("DV01 was 0.046.", {"dv01"}) == ["0.046"]
    assert untraced("A face amount of 100.", {"dv01"}) == ["100"]
    assert untraced("DV01x is not dv01.", {"dv01"}) == ["01"]


def test_an_all_digit_identifier_is_never_given():
    """request_identifiers drops all-digit tokens, so case-folding cannot exempt a bare number."""
    from finquint.orchestration.knowledge.reporting import request_identifiers
    from finquint.orchestration.models import ModelRequest, Segment, SegmentRole
    request = ModelRequest("r", "t", (Segment(SegmentRole.EVIDENCE, "[metric run:r1/analytics field=dv01] 0.046\n"
                                                                    "[metric run:r1/analytics field=100] 1"),),
                           {"type": "object"})
    found = request_identifiers(request)
    assert "dv01" in found and "100" not in found
