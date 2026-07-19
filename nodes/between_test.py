from gen.messages_pb2 import BetweenRequest
from nodes.between import between
from nodes.testkit import FakeContext, recurrence


def run(rrule, dtstart, start, end, limit=0, **kw):
    return between(
        FakeContext(),
        BetweenRequest(
            recurrence=recurrence(rrule, dtstart, **kw),
            start=start,
            end=end,
            limit=limit,
        ),
    )


def test_window_is_half_open_start_inclusive_end_exclusive():
    r = run("FREQ=DAILY", "20260101T000000", "20260103T000000", "20260105T000000")
    assert list(r.occurrences) == [
        "20260103T000000",  # exactly at start -- included
        "20260104T000000",
    ]  # 20260105 sits exactly at end -- excluded


def test_skips_occurrences_before_the_window():
    r = run("FREQ=DAILY", "20260101T000000", "20260110T000000", "20260112T000000")
    assert list(r.occurrences) == ["20260110T000000", "20260111T000000"]


def test_empty_window_returns_no_occurrences_without_error():
    r = run("FREQ=YEARLY", "20260101T000000", "20260601T000000", "20260901T000000")
    assert r.count == 0 and r.error.code == ""


def test_truncates_at_limit_within_a_wide_window():
    r = run("FREQ=DAILY", "20260101T000000", "20260101T000000", "20270101T000000", limit=3)
    assert r.count == 3 and r.truncated is True


def test_end_before_start_is_rejected():
    r = run("FREQ=DAILY", "20260101T000000", "20260105T000000", "20260103T000000")
    assert r.error.code == "INVALID_ARGUMENT"


def test_end_equal_to_start_is_rejected():
    r = run("FREQ=DAILY", "20260101T000000", "20260103T000000", "20260103T000000")
    assert r.error.code == "INVALID_ARGUMENT"


def test_far_future_window_is_bounded_rather_than_hanging():
    # Without a step budget this walks ~1.9 billion occurrences before the
    # window is even reached. It must come back as a structured error.
    r = run("FREQ=SECONDLY", "20260101T000000", "20860101T000000", "20860101T000100")
    assert r.error.code == "LIMIT_EXCEEDED"


def test_mixing_utc_window_with_floating_anchor_is_rejected():
    r = run("FREQ=DAILY", "20260101T000000", "20260103T000000Z", "20260105T000000Z")
    assert r.error.code == "INVALID_ARGUMENT"


def test_invalid_rule_returns_structured_error():
    r = run("FREQ=DAILY;BYSETPOS=2", "20260101T000000", "20260101T000000", "20260201T000000")
    assert r.error.code == "INVALID_RULE"
