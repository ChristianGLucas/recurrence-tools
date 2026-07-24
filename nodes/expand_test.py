import pytest

from gen.messages_pb2 import ExpandRequest, RuleInput
from nodes.expand import expand
from nodes.validate import validate
from nodes.testkit import NY, FakeContext, recurrence


def run(rrule, dtstart, limit=0, **kw):
    return expand(
        FakeContext(),
        ExpandRequest(recurrence=recurrence(rrule, dtstart, **kw), limit=limit),
    )


def test_expands_a_bounded_rule_exactly():
    r = run("FREQ=WEEKLY;COUNT=3", "20260105T090000")
    assert list(r.occurrences) == [
        "20260105T090000",
        "20260112T090000",
        "20260119T090000",
    ]
    assert r.count == 3
    assert r.truncated is False
    assert r.error.code == ""


def test_unbounded_rule_is_truncated_not_infinite():
    r = run("FREQ=DAILY", "20260101T000000", limit=5)
    assert r.count == 5
    assert r.truncated is True
    assert list(r.occurrences)[-1] == "20260105T000000"


def test_default_limit_is_100():
    r = run("FREQ=DAILY", "20260101T000000")
    assert r.count == 100 and r.truncated is True


def test_truncated_is_conservative_when_the_limit_is_reached_exactly():
    """`truncated` means "collection stopped early, more may exist".

    Deciding it precisely would mean pulling one occurrence beyond the limit,
    and that pull is unbounded: for a rule whose next gap is decades wide it can
    consume the whole budget and lose the answer the caller asked for. Over-
    reporting truncation is the safe direction -- the caller can ask for more
    and get nothing, which costs them nothing.
    """
    # With a bare COUNT rule the total is known for free, so this IS exact.
    r = run("FREQ=DAILY;COUNT=3", "20260101T000000", limit=3)
    assert r.count == 3
    assert list(r.occurrences)[-1] == "20260103T000000"
    assert r.truncated is False

    # With no COUNT to settle it, reaching the limit means "may be more".
    r = run("FREQ=DAILY", "20260101T000000", limit=3)
    assert r.count == 3 and r.truncated is True

    # Below the limit it is exact, because collection ran to genuine exhaustion.
    r = run("FREQ=DAILY;COUNT=3", "20260101T000000", limit=10)
    assert r.count == 3 and r.truncated is False


def test_rdate_is_merged_in_order():
    r = run("FREQ=DAILY;COUNT=2", "20260101T000000", rdate=["20260105T000000"])
    assert list(r.occurrences) == [
        "20260101T000000",
        "20260102T000000",
        "20260105T000000",
    ]


def test_exdate_removes_an_occurrence():
    r = run("FREQ=DAILY;COUNT=3", "20260101T000000", exdate=["20260102T000000"])
    assert list(r.occurrences) == ["20260101T000000", "20260103T000000"]


def test_exdate_that_matches_nothing_is_ignored():
    r = run("FREQ=DAILY;COUNT=2", "20260101T000000", exdate=["20301231T000000"])
    assert r.count == 2


def test_tzid_anchor_keeps_wall_clock_across_dst():
    # 8 March 2026 is the EST->EDT transition; 09:00 is on the far side of the
    # 02:00-03:00 gap, so both occurrences keep their wall-clock reading.
    # (That the zone is applied at all is proved in oracle_test.py by
    # test_tzid_actually_changes_the_answer, which a UTC window can falsify.)
    r = run("FREQ=DAILY;COUNT=2", "20260307T090000", tzid=NY)
    assert list(r.occurrences) == ["20260307T090000", "20260308T090000"]


def test_invalid_rule_returns_structured_error_not_a_crash():
    r = run("FREQ=DAILY;INTERVAL=0", "20260101T000000")
    assert r.error.code == "INVALID_RULE"
    assert "INTERVAL" in r.error.message
    assert r.count == 0 and list(r.occurrences) == []


def test_invalid_dtstart_returns_structured_error():
    r = run("FREQ=DAILY;COUNT=1", "not-a-date")
    assert r.error.code == "INVALID_DATETIME"


def test_large_limit_is_accepted_not_capped():
    # No package-level upper bound on `limit`: walk's own scan-step budget
    # caps real work regardless of what's requested.
    r = run("FREQ=DAILY;COUNT=1", "20260101T000000", limit=10001)
    assert r.error.code == ""
    assert r.count == 1


def test_unknown_tzid_is_rejected():
    r = run("FREQ=DAILY;COUNT=1", "20260101T000000", tzid="Mars/Olympus_Mons")
    assert r.error.code == "INVALID_ARGUMENT"


def test_tzid_with_utc_anchor_is_rejected_as_contradictory():
    r = run("FREQ=DAILY;COUNT=1", "20260101T000000Z", tzid=NY)
    assert r.error.code == "INVALID_ARGUMENT"


def test_output_matches_a_fixed_golden_regardless_of_when_it_runs():
    # Comparing two calls in one process would pass for any pure function and
    # prove nothing. The real nondeterminism risk is a dependence on "today"
    # (see PROBE_DTSTART in _recur.py), which only a hard-coded golden catches.
    r = run("FREQ=MONTHLY;COUNT=12;BYDAY=1FR", "20260102T090000", tzid=NY)
    assert list(r.occurrences) == [
        "20260102T090000", "20260206T090000", "20260306T090000",
        "20260403T090000", "20260501T090000", "20260605T090000",
        "20260703T090000", "20260807T090000", "20260904T090000",
        "20261002T090000", "20261106T090000", "20261204T090000",
    ]
    assert r.truncated is False


def test_utc_until_with_utc_anchor_expands():
    r = run("FREQ=DAILY;UNTIL=20260103T000000Z", "20260101T000000Z")
    assert list(r.occurrences) == [
        "20260101T000000Z",
        "20260102T000000Z",
        "20260103T000000Z",
    ]


def test_utc_until_against_a_floating_anchor_is_reported_not_crashed():
    # RFC 5545 3.3.10 forbids the pairing; it must surface as a structured
    # error rather than a TypeError from deep inside the expander.
    r = run("FREQ=DAILY;UNTIL=20260103T000000Z", "20260101T000000")
    assert r.error.code == "INVALID_RULE"


def test_utc_until_is_compared_against_the_anchors_real_instant():
    # The anchor is 09:00 in New York, which in January is 14:00 UTC. So a UTC
    # UNTIL is decided against 14:00Z, not against the 09:00 wall-clock reading.
    before = run("FREQ=DAILY;UNTIL=20260103T120000Z", "20260101T090000", tzid=NY)
    assert list(before.occurrences) == [
        "20260101T090000",
        "20260102T090000",
    ]  # 12:00Z precedes Jan 3's 14:00Z occurrence, so Jan 3 is excluded

    after = run("FREQ=DAILY;UNTIL=20260103T150000Z", "20260101T090000", tzid=NY)
    assert list(after.occurrences) == [
        "20260101T090000",
        "20260102T090000",
        "20260103T090000",
    ]  # 15:00Z follows it, so Jan 3 is included


@pytest.mark.parametrize("ordinal", ["54", "8", "6", "0", "-54", "99"])
def test_out_of_range_byday_is_reported_not_crashed(ordinal):
    # Regression: these once escaped as an uncaught IndexError from inside
    # dateutil, surfacing to the caller as a 422 with a raw traceback, while
    # Validate declared the very same rule valid.
    r = run(f"FREQ=MONTHLY;BYDAY={ordinal}MO", "19970902T090000")
    assert r.error.code == "INVALID_RULE"
    assert "BYDAY" in r.error.message


def test_validate_and_expand_agree_on_byday_ordinals():
    # The two nodes must not disagree: anything Validate blesses must expand,
    # and anything Validate rejects must not crash Expand.
    for freq in ("MONTHLY", "YEARLY"):
        for ordinal in range(-60, 61):
            rule = f"FREQ={freq};BYDAY={ordinal}MO"
            valid = validate(FakeContext(), RuleInput(rrule=rule)).valid
            expanded = run(rule, "19970902T090000", limit=1)
            assert valid == (expanded.error.code == ""), f"disagreement on {rule}"


def test_date_form_candidate_mismatch_is_rejected():
    r = run("FREQ=DAILY;COUNT=3", "19970902", limit=1)
    assert r.error.code == ""  # a DATE anchor alone is fine
