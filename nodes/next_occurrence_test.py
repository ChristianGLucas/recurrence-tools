from gen.messages_pb2 import NextRequest
from nodes.next_occurrence import next_occurrence as next_node
from nodes.testkit import FakeContext, recurrence


def run(rrule, dtstart, after="", **kw):
    return next_node(
        FakeContext(),
        NextRequest(recurrence=recurrence(rrule, dtstart, **kw), after=after),
    )


def test_empty_after_returns_the_first_occurrence():
    r = run("FREQ=DAILY;COUNT=5", "20260101T000000")
    assert r.found is True and r.occurrence == "20260101T000000"


def test_is_strictly_after_not_inclusive():
    r = run("FREQ=DAILY;COUNT=5", "20260101T000000", after="20260102T000000")
    assert r.occurrence == "20260103T000000"


def test_returns_not_found_past_the_end_of_a_finite_rule():
    r = run("FREQ=DAILY;COUNT=2", "20260101T000000", after="20260301T000000")
    assert r.found is False and r.occurrence == "" and r.error.code == ""


def test_skips_an_excluded_occurrence():
    r = run(
        "FREQ=DAILY;COUNT=5",
        "20260101T000000",
        after="20260101T000000",
        exdate=["20260102T000000"],
    )
    assert r.occurrence == "20260103T000000"


def test_works_on_an_unbounded_rule():
    r = run("FREQ=DAILY", "20260101T000000", after="20260615T000000")
    assert r.found is True and r.occurrence == "20260616T000000"


def test_unreachable_candidate_is_bounded():
    r = run("FREQ=SECONDLY", "20260101T000000", after="20860101T000000")
    assert r.error.code == "LIMIT_EXCEEDED"


def test_invalid_after_returns_structured_error():
    r = run("FREQ=DAILY;COUNT=2", "20260101T000000", after="whenever")
    assert r.error.code == "INVALID_DATETIME"
