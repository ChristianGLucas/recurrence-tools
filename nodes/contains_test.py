from gen.messages_pb2 import ContainsRequest
from nodes.contains import contains
from nodes.testkit import FakeContext, recurrence


def run(rrule, dtstart, candidate, **kw):
    return contains(
        FakeContext(),
        ContainsRequest(recurrence=recurrence(rrule, dtstart, **kw), candidate=candidate),
    )


def test_true_for_a_real_occurrence():
    assert run("FREQ=WEEKLY;COUNT=5", "20260105T090000", "20260119T090000").contains is True


def test_false_for_an_instant_between_occurrences():
    assert run("FREQ=WEEKLY;COUNT=5", "20260105T090000", "20260120T090000").contains is False


def test_false_past_the_end_of_a_finite_rule():
    assert run("FREQ=WEEKLY;COUNT=2", "20260105T090000", "20270105T090000").contains is False


def test_false_before_the_anchor():
    assert run("FREQ=WEEKLY;COUNT=5", "20260105T090000", "20251201T090000").contains is False


def test_excluded_instant_is_not_a_member():
    r = run("FREQ=DAILY;COUNT=5", "20260101T000000", "20260102T000000",
            exdate=["20260102T000000"])
    assert r.contains is False


def test_added_rdate_is_a_member():
    r = run("FREQ=DAILY;COUNT=2", "20260101T000000", "20260210T000000",
            rdate=["20260210T000000"])
    assert r.contains is True


def test_unreachable_candidate_is_bounded():
    r = run("FREQ=SECONDLY", "20260101T000000", "20860101T000000")
    assert r.error.code == "LIMIT_EXCEEDED"


def test_invalid_candidate_returns_structured_error():
    r = run("FREQ=DAILY;COUNT=2", "20260101T000000", "sometime")
    assert r.error.code == "INVALID_DATETIME"
