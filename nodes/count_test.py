from gen.messages_pb2 import CountRequest
from nodes.count import count
from nodes.testkit import FakeContext, recurrence


def run(rrule, dtstart, limit=0, **kw):
    return count(
        FakeContext(),
        CountRequest(recurrence=recurrence(rrule, dtstart, **kw), limit=limit),
    )


def test_counts_a_finite_rule_exactly():
    r = run("FREQ=WEEKLY;COUNT=7", "20260105T090000")
    assert r.count == 7 and r.truncated is False


def test_counts_an_until_bounded_rule():
    # 20260101 through 20260110 inclusive of the UNTIL instant.
    r = run("FREQ=DAILY;UNTIL=20260110T000000", "20260101T000000")
    assert r.count == 10 and r.truncated is False


def test_unbounded_rule_reports_a_truncated_floor():
    r = run("FREQ=DAILY", "20260101T000000", limit=50)
    assert r.count == 50 and r.truncated is True


def test_exdate_reduces_the_count():
    r = run("FREQ=DAILY;COUNT=5", "20260101T000000", exdate=["20260103T000000"])
    assert r.count == 4 and r.truncated is False


def test_rdate_increases_the_count():
    r = run("FREQ=DAILY;COUNT=5", "20260101T000000", rdate=["20260901T000000"])
    assert r.count == 6


def test_invalid_rule_returns_structured_error():
    r = run("FREQ=DAILY;COUNT=0", "20260101T000000")
    assert r.error.code == "INVALID_RULE" and r.count == 0
