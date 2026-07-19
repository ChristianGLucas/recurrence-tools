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
from nodes.testkit import FakeContext, recurrence
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
    result, elapsed = timed(
        lambda: count(
            FakeContext(),
            CountRequest(recurrence=recurrence(rule, "00010101T000000"), limit=1),
        )
    )
    assert result.error.code == "LIMIT_EXCEEDED", result
    assert elapsed < CEILING, f"took {elapsed:.1f}s"


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
