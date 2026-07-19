from gen.messages_pb2 import RuleInput
from nodes.parse import parse
from nodes.testkit import FakeContext


def run(rrule):
    return parse(FakeContext(), RuleInput(rrule=rrule))


def test_decomposes_every_supported_part():
    r = run(
        "FREQ=MONTHLY;INTERVAL=2;COUNT=10;BYDAY=MO,-1SU;BYMONTHDAY=1,-1;"
        "BYMONTH=3,6;BYHOUR=9;BYMINUTE=30;BYSECOND=0;BYSETPOS=1;WKST=SU"
    )
    assert r.freq == "MONTHLY"
    assert r.interval == 2
    assert r.count == 10
    assert list(r.byday) == ["MO", "-1SU"]
    assert list(r.bymonthday) == [1, -1]
    assert list(r.bymonth) == [3, 6]
    assert list(r.byhour) == [9]
    assert list(r.byminute) == [30]
    assert list(r.bysecond) == [0]
    assert list(r.bysetpos) == [1]
    assert r.wkst == "SU"
    assert r.error.code == ""


def test_omitted_parts_stay_zero_rather_than_defaulted():
    # The RFC default for INTERVAL is 1, but reporting 1 here would erase the
    # difference between "INTERVAL=1" and "INTERVAL omitted".
    r = run("FREQ=DAILY")
    assert r.freq == "DAILY"
    assert r.interval == 0
    assert r.count == 0
    assert r.until == ""
    assert list(r.byday) == []
    assert r.wkst == ""


def test_until_is_preserved_verbatim():
    assert run("FREQ=DAILY;UNTIL=20260110T000000Z").until == "20260110T000000Z"


def test_uppercases_case_insensitive_input():
    r = run("freq=weekly;byday=mo;wkst=su")
    assert r.freq == "WEEKLY" and list(r.byday) == ["MO"] and r.wkst == "SU"


def test_invalid_rule_returns_structured_error_and_no_parts():
    r = run("FREQ=DAILY;INTERVAL=0")
    assert r.error.code == "INVALID_RULE"
    assert r.freq == "" and r.interval == 0
