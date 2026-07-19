from gen.messages_pb2 import ExpandRequest
from nodes.expand import expand
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


def test_truncated_is_false_when_rule_ends_exactly_at_limit():
    r = run("FREQ=DAILY;COUNT=3", "20260101T000000", limit=3)
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


def test_limit_above_maximum_is_rejected():
    r = run("FREQ=DAILY;COUNT=1", "20260101T000000", limit=10001)
    assert r.error.code == "INVALID_ARGUMENT"


def test_unknown_tzid_is_rejected():
    r = run("FREQ=DAILY;COUNT=1", "20260101T000000", tzid="Mars/Olympus_Mons")
    assert r.error.code == "INVALID_ARGUMENT"


def test_tzid_with_utc_anchor_is_rejected_as_contradictory():
    r = run("FREQ=DAILY;COUNT=1", "20260101T000000Z", tzid=NY)
    assert r.error.code == "INVALID_ARGUMENT"


def test_is_deterministic_across_invocations():
    a = run("FREQ=MONTHLY;COUNT=12;BYDAY=1FR", "20260102T090000", tzid=NY)
    b = run("FREQ=MONTHLY;COUNT=12;BYDAY=1FR", "20260102T090000", tzid=NY)
    assert list(a.occurrences) == list(b.occurrences)


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
