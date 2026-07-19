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


def test_a_window_the_scan_never_reached_is_an_error_not_an_empty_answer():
    # Reaching this window means stepping over ~1.9 billion occurrences, so the
    # budget stops first -- before the window is ever entered. Returning an empty
    # list would read as "this window has no occurrences", a finding that was
    # never established. The distinction matters: an empty answer is a claim.
    result = run("FREQ=SECONDLY", "20260101T000000", "20860101T000000", "20860101T000100")
    assert result.error.code == "LIMIT_EXCEEDED"
    assert "before reaching the requested window" in result.error.message


def test_an_empty_window_the_scan_did_reach_is_a_real_answer():
    # Here the scan passes straight through the window, so "no occurrences" is
    # an established finding and is returned as one.
    result = run("FREQ=YEARLY", "20260101T000000", "20260601T000000", "20260901T000000")
    assert result.error.code == ""
    assert result.count == 0


def test_mixing_utc_window_with_floating_anchor_is_rejected():
    r = run("FREQ=DAILY", "20260101T000000", "20260103T000000Z", "20260105T000000Z")
    assert r.error.code == "INVALID_ARGUMENT"


def test_invalid_rule_returns_structured_error():
    r = run("FREQ=DAILY;BYSETPOS=2", "20260101T000000", "20260101T000000", "20260201T000000")
    assert r.error.code == "INVALID_RULE"
