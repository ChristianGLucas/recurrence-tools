from gen.messages_pb2 import RuleInput, RuleParts
from nodes.build import build
from nodes.parse import parse
from nodes.testkit import FakeContext
from nodes.validate import validate


def run(**parts):
    return build(FakeContext(), RuleParts(**parts))


def test_assembles_parts_into_a_canonical_rule():
    r = run(freq="WEEKLY", count=10, byday=["MO", "WE"])
    assert r.rrule == "FREQ=WEEKLY;COUNT=10;BYDAY=MO,WE"
    assert r.error.code == ""


def test_emits_canonical_order_regardless_of_field_order():
    r = run(wkst="SU", byday=["MO"], freq="WEEKLY", interval=2)
    assert r.rrule == "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;WKST=SU"


def test_zero_and_empty_fields_are_treated_as_omitted():
    r = run(freq="DAILY", interval=0, count=0, until="", byday=[])
    assert r.rrule == "FREQ=DAILY"


def test_round_trips_with_parse():
    original = "FREQ=MONTHLY;INTERVAL=2;COUNT=10;BYDAY=MO,-1SU;BYMONTHDAY=1,-1;WKST=SU"
    parsed = parse(FakeContext(), RuleInput(rrule=original))
    rebuilt = build(FakeContext(), parsed)
    assert rebuilt.rrule == original


def test_round_trip_output_validates():
    rebuilt = run(freq="YEARLY", bymonth=[3], byday=["-1SU"])
    assert validate(FakeContext(), RuleInput(rrule=rebuilt.rrule)).valid is True


def test_no_parts_is_rejected():
    r = run()
    assert r.error.code == "INVALID_ARGUMENT" and r.rrule == ""


def test_parts_that_would_form_an_invalid_rule_are_rejected():
    # Build must never emit a rule that Validate would then reject.
    assert run(count=5).error.code == "INVALID_RULE"                 # no FREQ
    assert run(freq="DAILY", bysetpos=[1]).error.code == "INVALID_RULE"  # lone BYSETPOS
    assert run(freq="DAILY", count=3, until="20260101T000000Z").error.code == "INVALID_RULE"
    assert run(freq="FORTNIGHTLY").error.code == "INVALID_RULE"
    assert run(freq="YEARLY", bymonth=[13]).error.code == "INVALID_RULE"
