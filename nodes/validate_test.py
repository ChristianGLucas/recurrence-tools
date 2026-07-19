import pytest

from gen.messages_pb2 import RuleInput
from nodes.testkit import FakeContext
from nodes.validate import validate


def run(rrule):
    return validate(FakeContext(), RuleInput(rrule=rrule))


def test_valid_rule_is_accepted_and_normalized():
    r = run("FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10")
    assert r.valid is True
    assert r.normalized == "FREQ=WEEKLY;COUNT=10;BYDAY=MO,WE"
    assert r.error.code == ""


def test_normalization_reorders_into_canonical_form():
    assert run("COUNT=5;FREQ=DAILY").normalized == "FREQ=DAILY;COUNT=5"


def test_normalization_uppercases():
    assert run("freq=daily;byday=mo").normalized == "FREQ=DAILY;BYDAY=MO"


def test_normalized_output_is_itself_valid():
    once = run("count=5;byday=mo,we;freq=WEEKLY;wkst=su").normalized
    twice = run(once)
    assert twice.valid is True and twice.normalized == once


@pytest.mark.parametrize(
    "rule,fragment",
    [
        ("FREQ=DAILY;INTERVAL=0", "INTERVAL"),
        ("FREQ=DAILY;COUNT=3;UNTIL=20260101T000000Z", "COUNT and UNTIL"),
        ("FREQ=DAILY;BYSETPOS=1", "BYSETPOS"),
        ("FREQ=YEARLY;BYMONTH=13", "BYMONTH"),
        ("COUNT=2", "FREQ"),
        ("RRULE:FREQ=DAILY", "bare RECUR"),
    ],
)
def test_invalid_rules_are_rejected_with_a_naming_message(rule, fragment):
    r = run(rule)
    assert r.valid is False
    assert r.normalized == ""
    assert fragment in r.error.message


def test_empty_rule_is_invalid():
    r = run("")
    assert r.valid is False and r.error.code == "INVALID_RULE"
