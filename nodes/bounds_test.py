"""Bounds against costly input, including rules that cannot be bounded from outside.

The dangerous class here is a rule that is perfectly valid and matches NOTHING.
It yields no occurrences, so any budget counted over yielded results never
advances even once, while the expander scans on. The rewritten UNTIL catches
most of these; the isolated worker catches the rest, because with certain part
combinations the expander does not honour that UNTIL at all.

Every test below asserts BOTH a structured error and a wall-clock ceiling -- a
bound that returns the right code after two minutes is not a bound.
"""

import time

import pytest

from gen.messages_pb2 import (
    BetweenRequest,
    ContainsRequest,
    CountRequest,
    ExpandRequest,
    RuleInput,
)
from nodes.between import between
from nodes.contains import contains
from nodes.count import count
from nodes.expand import expand
from nodes.next_occurrence import next_occurrence
from nodes.testkit import NY, FakeContext, recurrence
from nodes.validate import validate

SECONDS = ",".join(str(i) for i in range(60))
MINUTES = ",".join(str(i) for i in range(60))
HOURS = ",".join(str(i) for i in range(24))
WEEKNOS = ",".join(str(i) for i in range(1, 54))

# A ceiling generous enough not to flake on a loaded machine, but far below the
# unbounded behaviour these guard against (measured in minutes, or never).
CEILING = 12.0


def timed(fn):
    started = time.time()
    result = fn()
    return result, time.time() - started


@pytest.mark.parametrize(
    "rule",
    [
        # February has no 30th, so none of these ever produce an occurrence.
        "FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30",
        f"FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30;BYSECOND={SECONDS};BYMINUTE={MINUTES}",
        "FREQ=MINUTELY;BYMONTH=2;BYMONTHDAY=30",
        "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30",
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30",
        f"FREQ=SECONDLY;BYMONTH=2;BYMONTHDAY=30;BYSECOND={SECONDS};"
        f"BYMINUTE={MINUTES};BYHOUR={HOURS};BYSETPOS=-1",
    ],
)
def test_a_rule_that_never_matches_is_bounded(rule):
    """The guarantee is termination, not a particular verdict.

    A rule matching nothing has a truthful answer -- zero occurrences -- and
    returning it is better than erroring. What must never happen is running on
    unbounded, so either outcome is acceptable provided it arrives promptly:
    a count of 0, or LIMIT_EXCEEDED when the search costs more than the budget.
    """
    result, elapsed = timed(
        lambda: count(
            FakeContext(),
            CountRequest(recurrence=recurrence(rule, "00010101T000000"), limit=1),
        )
    )
    assert elapsed < CEILING, f"took {elapsed:.1f}s"
    if result.error.code:
        assert result.error.code == "LIMIT_EXCEEDED", result
    else:
        assert result.count == 0, result


def test_the_never_matching_rule_is_bounded_on_every_expansion_node():
    rule = f"FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30;BYSECOND={SECONDS};BYMINUTE={MINUTES}"
    rec = recurrence(rule, "00010101T000000")
    calls = [
        lambda: expand(FakeContext(), ExpandRequest(recurrence=rec, limit=1)),
        lambda: between(
            FakeContext(),
            BetweenRequest(recurrence=rec, start="00010101T000000", end="99990101T000000"),
        ),
        lambda: next_occurrence(FakeContext(), __import__(
            "gen.messages_pb2", fromlist=["NextRequest"]
        ).NextRequest(recurrence=rec, after="00010101T000000")),
        lambda: contains(
            FakeContext(),
            ContainsRequest(recurrence=rec, candidate="20260101T000000"),
        ),
        lambda: count(FakeContext(), CountRequest(recurrence=rec, limit=1)),
    ]
    for call in calls:
        result, elapsed = timed(call)
        assert result.error.code == "LIMIT_EXCEEDED", result
        assert elapsed < CEILING, f"took {elapsed:.1f}s"


@pytest.mark.parametrize(
    "rule,fragment",
    [
        # RFC 5545 3.3.10 part/frequency constraints. BYWEEKNO on a sub-yearly
        # frequency is also what makes the expander ignore its own UNTIL.
        ("FREQ=HOURLY;BYWEEKNO=1", "BYWEEKNO"),
        ("FREQ=MONTHLY;BYWEEKNO=1", "BYWEEKNO"),
        ("FREQ=DAILY;BYWEEKNO=1", "BYWEEKNO"),
        ("FREQ=DAILY;BYYEARDAY=1", "BYYEARDAY"),
        ("FREQ=WEEKLY;BYYEARDAY=1", "BYYEARDAY"),
        ("FREQ=MONTHLY;BYYEARDAY=1", "BYYEARDAY"),
        ("FREQ=WEEKLY;BYMONTHDAY=1", "BYMONTHDAY"),
    ],
)
def test_rfc_part_frequency_constraints_are_enforced(rule, fragment):
    result = validate(FakeContext(), RuleInput(rrule=rule))
    assert result.valid is False
    assert fragment in result.error.message


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=YEARLY;BYWEEKNO=20",      # BYWEEKNO is legal on YEARLY
        "FREQ=YEARLY;BYYEARDAY=100",
        "FREQ=SECONDLY;BYYEARDAY=100",  # legal on sub-daily frequencies
        "FREQ=MONTHLY;BYMONTHDAY=15",
        "FREQ=DAILY;BYMONTHDAY=15",
    ],
)
def test_the_constraints_do_not_over_reach(rule):
    assert validate(FakeContext(), RuleInput(rrule=rule)).valid is True


def test_legitimate_rules_stay_fast():
    cases = [
        ("FREQ=DAILY", "20260101T000000"),
        ("FREQ=MONTHLY;BYDAY=1FR;COUNT=120", "20260102T090000"),
        ("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;COUNT=3", "20240229T000000"),
        ("FREQ=YEARLY;BYWEEKNO=20;COUNT=5", "20260101T000000"),
        # SPARSE sub-daily rules: a high FREQ filtered by BY* parts down to one
        # occurrence a day. Every case above is dense, which is why none of them
        # caught a bound sized from raw frequency rather than real density.
        ("FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0", "20260101T000000"),
        ("FREQ=MINUTELY;BYHOUR=9;BYMINUTE=30", "20260101T000000"),
        ("FREQ=HOURLY;BYHOUR=9", "20260101T000000"),
    ]
    for rule, dtstart in cases:
        result, elapsed = timed(
            lambda: expand(
                FakeContext(),
                ExpandRequest(recurrence=recurrence(rule, dtstart), limit=100),
            )
        )
        assert result.error.code == "", result.error.message
        assert elapsed < 5.0, f"{rule} took {elapsed:.1f}s"


def test_a_sparse_sub_daily_rule_expands_to_the_right_dates():
    """A high FREQ narrowed by BY* parts is one occurrence per day, not a bound.

    Regression: sizing an expansion ceiling from FREQ alone treated this as
    "one occurrence per second" and stopped before the rule's own first
    occurrence, reporting LIMIT_EXCEEDED for a rule that is cheap and infinite.
    """
    result = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence(
                "FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0", "20260101T000000"
            ),
            limit=3,
        ),
    )
    assert result.error.code == "", result.error.message
    assert list(result.occurrences) == [
        "20260101T090000",
        "20260102T090000",
        "20260103T090000",
    ]


@pytest.mark.parametrize("limit", [1000, 5000, 10000])
def test_expansion_at_large_limits_returns_the_occurrences(limit):
    """The documented maximum must actually work, not just be documented.

    Regression: results crossed the worker boundary through a pipe, and any
    payload past the pipe buffer (~64KiB) deadlocked -- surfacing as a false
    LIMIT_EXCEEDED that blamed the rule for being "too costly". The threshold
    tracked BYTES, so it only appeared above a few thousand occurrences, which
    no test reached.
    """
    result, elapsed = timed(
        lambda: expand(
            FakeContext(),
            ExpandRequest(
                recurrence=recurrence(f"FREQ=DAILY;COUNT={limit}", "20260101T000000"),
                limit=limit,
            ),
        )
    )
    assert result.error.code == "", result.error.message
    assert result.count == limit
    assert result.occurrences[0] == "20260101T000000"
    assert elapsed < CEILING, f"took {elapsed:.1f}s"


def test_a_large_result_is_not_reported_as_too_costly():
    """The same expansion work with a tiny output already worked; the payload
    size was the only difference, which is what proved it was a deadlock."""
    big = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=10000", "20260101T000000"),
            limit=10000,
        ),
    )
    small = count(
        FakeContext(),
        CountRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=10000", "20260101T000000"),
            limit=10000,
        ),
    )
    assert big.error.code == "" and big.count == 10000
    assert small.error.code == "" and small.count == 10000


def test_occurrences_at_the_year_ceiling_are_returned_not_refused():
    """Near year 9999 there is genuinely nothing beyond the end of the calendar,
    so exhaustion is a real answer -- not a truncated scan to report as a bound."""
    result = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=5", "99991230T000000"), limit=5
        ),
    )
    assert result.error.code == "", result.error.message
    assert list(result.occurrences) == ["99991230T000000", "99991231T000000"]


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
        "FREQ=MINUTELY;BYHOUR=9;BYMINUTE=30;COUNT=100",
        "FREQ=HOURLY;BYHOUR=9;COUNT=2000",
        "FREQ=DAILY;COUNT=10000",
        "FREQ=MONTHLY;BYDAY=-1FR",
        "FREQ=YEARLY;BYWEEKNO=20",
    ],
)
def test_validate_accepting_a_rule_means_expand_can_serve_it(rule):
    """Validate is an admission gate, so anything it blesses must expand."""
    assert validate(FakeContext(), RuleInput(rrule=rule)).valid is True
    result = expand(
        FakeContext(),
        ExpandRequest(recurrence=recurrence(rule, "20260101T000000"), limit=100),
    )
    assert result.error.code == "", f"{rule} -> {result.error.message}"
    assert result.count > 0


def test_dst_edge_resolution_is_pinned():
    """The wall clock is preserved across both DST edges; pin the policy so it
    cannot change silently."""
    # Spring forward: 02:30 does not exist on 2026-03-08 in New York. The local
    # reading is preserved as written rather than being skipped or shifted.
    forward = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=3", "20260307T023000", tzid=NY),
            limit=3,
        ),
    )
    assert list(forward.occurrences) == [
        "20260307T023000",
        "20260308T023000",  # nonexistent locally; lands after the gap
        "20260309T023000",
    ]
    # Fall back: 01:30 occurs twice on 2026-11-01. The first is used.
    back = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=2", "20261031T013000", tzid=NY),
            limit=2,
        ),
    )
    assert list(back.occurrences) == ["20261031T013000", "20261101T013000"]
