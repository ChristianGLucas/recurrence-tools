"""Bounds against costly input.

Two shapes are dangerous and neither is visible in a result size:

1. A rule that is valid and matches NOTHING. It yields no occurrences, so any
   budget counted over yielded results never advances while the expander scans
   on. Only the isolated worker's wall-clock backstop can stop this one, because
   the scan happens inside a single library call.

2. A rule that is valid and SPARSE -- one occurrence a day at a per-second
   frequency. It yields steadily, but steps through 86400 candidates per
   occurrence. This is bounded by counting candidates, and a count is what keeps
   the answer identical on every machine and every run.

Tests assert a wall-clock ceiling as well as a verdict: a bound that returns the
right answer after two minutes is not a bound.
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
from gen.messages_pb2 import NextRequest, RuleParts
from nodes.between import between
from nodes.build import build
from nodes.contains import contains
from nodes.count import count
from nodes.expand import expand
from nodes.parse import parse
from nodes.next_occurrence import next_occurrence
from nodes import _recur
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
        # February has no 30th, April no 31st: these can never occur, and the
        # expander would only discover that by scanning to the year ceiling.
        "FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30",
        f"FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30;BYSECOND={SECONDS};BYMINUTE={MINUTES}",
        "FREQ=MINUTELY;BYMONTH=2;BYMONTHDAY=30",
        "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30",
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30",
        "FREQ=YEARLY;BYMONTH=4;BYMONTHDAY=31",
        f"FREQ=SECONDLY;BYMONTH=2;BYMONTHDAY=30;BYSECOND={SECONDS};"
        f"BYMINUTE={MINUTES};BYHOUR={HOURS};BYSETPOS=-1",
    ],
)
def test_an_impossible_month_day_pair_is_refused_immediately(rule):
    """Refuse what cannot happen, rather than spending seconds proving it.

    These are the rules that forced the whole isolation design: they yield
    nothing, so no budget counted over results can see them. A calendar fact --
    February never has a 30th -- settles them up front, deterministically, and
    tells the caller what is actually wrong with their rule.
    """
    result, elapsed = timed(
        lambda: count(
            FakeContext(),
            CountRequest(recurrence=recurrence(rule, "00010101T000000"), limit=1),
        )
    )
    assert result.error.code == "INVALID_RULE", result
    assert "can never occur" in result.error.message
    assert elapsed < 1.0, f"took {elapsed:.1f}s"


@pytest.mark.parametrize(
    "rule",
    [
        # Rare but genuinely possible, so they must NOT be refused: yearday 366
        # exists in leap years, week 53 in long years, Feb 29 every four years.
        "FREQ=SECONDLY;BYYEARDAY=366;BYHOUR=3",
        "FREQ=SECONDLY;BYMONTH=2;BYMONTHDAY=29;BYHOUR=1;BYMINUTE=1;BYSECOND=1",
        "FREQ=YEARLY;BYWEEKNO=53",
    ],
)
def test_rare_but_possible_rules_are_answered_not_refused(rule):
    """The feasibility check must not over-reach into merely-infrequent rules."""
    result, elapsed = timed(
        lambda: count(
            FakeContext(),
            CountRequest(recurrence=recurrence(rule, "20200101T000000"), limit=1),
        )
    )
    assert result.error.code == "", result.error.message
    assert elapsed < CEILING, f"took {elapsed:.1f}s"


def test_the_never_matching_rule_is_bounded_on_every_expansion_node():
    rule = f"FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30;BYSECOND={SECONDS};BYMINUTE={MINUTES}"
    rec = recurrence(rule, "00010101T000000")
    expected = "INVALID_RULE"
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
        assert result.error.code == expected, result
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
    # Fall back: 01:30 occurs twice on 2026-11-01, at 05:30Z (EDT) and 06:30Z
    # (EST). The wall-clock string is the same either way, so it cannot
    # distinguish them -- only an absolute UTC window can, and it is the FIRST.
    def utc_window(start, end):
        result = between(
            FakeContext(),
            BetweenRequest(
                recurrence=recurrence("FREQ=DAILY;COUNT=2", "20261031T013000", tzid=NY),
                start=start, end=end,
            ),
        )
        assert result.error.code == "", result.error.message
        return list(result.occurrences)

    assert utc_window("20261101T052000Z", "20261101T054000Z") == ["20261101T013000"]
    assert utc_window("20261101T062000Z", "20261101T064000Z") == []

    # Spring forward: 02:30 does not exist on 2026-03-08, and resolves to the
    # instant after the gap -- 07:30Z, which is 03:30 EDT.
    def spring(start, end):
        result = between(
            FakeContext(),
            BetweenRequest(
                recurrence=recurrence("FREQ=DAILY;COUNT=3", "20260307T023000", tzid=NY),
                start=start, end=end,
            ),
        )
        assert result.error.code == "", result.error.message
        return list(result.occurrences)

    assert spring("20260308T072000Z", "20260308T074000Z") == ["20260308T023000"]


# --- The axes that must CROSS: rule density x requested limit ---------------
#
# Sparse rules and large limits were each covered, but never together, which is
# where the defect lived: a rule yielding once a day still steps through every
# second in between, so its cost is invisible in both its result size and its
# occurrence count.

SPARSE_RULES = [
    "FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
    "FREQ=MINUTELY;BYHOUR=9;BYMINUTE=30",
    "FREQ=HOURLY;BYHOUR=9",
]


@pytest.mark.parametrize("rule", SPARSE_RULES)
@pytest.mark.parametrize("limit", [100, 1100, 10000])
def test_sparse_rules_at_every_limit_return_data_not_an_error(rule, limit):
    """A sparse rule must never be refused for being sparse.

    It may return fewer occurrences than asked for -- the scan budget is real --
    but what comes back must be real occurrences with `truncated` set, never an
    error blaming the rule for being too costly.
    """
    result, elapsed = timed(
        lambda: expand(
            FakeContext(),
            ExpandRequest(
                recurrence=recurrence(rule, "20200101T000000"), limit=limit
            ),
        )
    )
    assert result.error.code == "", result.error.message
    assert result.count > 0
    if result.count < limit:
        assert result.truncated is True, "a short answer must be flagged"
    assert elapsed < 2.5, f"took {elapsed:.1f}s"


@pytest.mark.parametrize("rule", SPARSE_RULES)
def test_count_at_its_documented_default_serves_every_valid_rule(rule):
    """Count's own default limit is 10000; the default request must work."""
    result, elapsed = timed(
        lambda: count(
            FakeContext(),
            CountRequest(recurrence=recurrence(rule, "20200101T000000")),
        )
    )
    assert result.error.code == "", result.error.message
    assert result.count > 0
    assert elapsed < 2.5, f"took {elapsed:.1f}s"


def test_identical_requests_give_identical_answers_near_the_budget():
    """The bound must be a count, not a clock.

    Sized so the request sits near the scan budget, which is exactly where a
    wall-clock bound returned a different answer run to run depending on load.
    """
    request = ExpandRequest(
        recurrence=recurrence(SPARSE_RULES[0], "20200101T000000"), limit=1040
    )
    outcomes = {
        (r.error.code, r.count, r.truncated)
        for r in (expand(FakeContext(), request) for _ in range(12))
    }
    assert len(outcomes) == 1, f"nondeterministic across 12 runs: {outcomes}"


def test_nodes_that_cannot_answer_partially_report_the_budget():
    """A single-answer node has no partial form, so silence would be a lie."""
    rec = recurrence(SPARSE_RULES[0], "20200101T000000")
    nxt = next_occurrence(
        FakeContext(), NextRequest(recurrence=rec, after="21000101T000000")
    )
    assert nxt.error.code == "LIMIT_EXCEEDED"
    assert nxt.found is False

    member = contains(
        FakeContext(), ContainsRequest(recurrence=rec, candidate="21000101T090000")
    )
    assert member.error.code == "LIMIT_EXCEEDED"
    assert member.contains is False


# --- Error propagation across a composed edge ------------------------------

def test_an_upstream_error_is_propagated_not_re_derived():
    """Chained nodes must report the mistake that happened, not invent one.

    Without an inbound error channel a downstream node sees only empty fields
    and confidently blames the wrong one -- reporting "candidate is required"
    for what was actually a BYSETPOS mistake.
    """
    upstream = {"code": "INVALID_RULE", "message": "BYSETPOS must be used together with another BY* rule part"}
    rec = dict(recurrence("", ""), error=upstream)

    for result in (
        expand(FakeContext(), ExpandRequest(recurrence=rec, limit=5)),
        count(FakeContext(), CountRequest(recurrence=rec)),
        next_occurrence(FakeContext(), NextRequest(recurrence=rec)),
        contains(FakeContext(), ContainsRequest(recurrence=rec, candidate="20260101T000000")),
        validate(FakeContext(), RuleInput(rrule="", error=upstream)),
        parse(FakeContext(), RuleInput(rrule="", error=upstream)),
    ):
        assert result.error.code == "INVALID_RULE", result
        assert "BYSETPOS" in result.error.message, result


def test_caller_strings_echoed_into_errors_are_bounded():
    """An error is a diagnostic, not a mirror: a huge input must not buy a
    huge response."""
    result = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=1", "20260101T000000", tzid="A" * 200000),
            limit=1,
        ),
    )
    assert result.error.code == "INVALID_ARGUMENT"
    assert len(result.error.message) < 500, len(result.error.message)
    assert "200000 characters" in result.error.message


def test_oversized_repeated_fields_are_refused_before_being_built():
    result = build(FakeContext(), RuleParts(freq="DAILY", bysetpos=[1] * 100000))
    assert result.error.code == "LIMIT_EXCEEDED"
    assert "100000 entries" in result.error.message


# --- The wall-clock backstop, which nothing previously exercised -----------

def test_the_wall_clock_backstop_actually_fires(monkeypatch):
    """The backstop was the least-tested bound and the most-claimed one.

    It cannot be triggered by any real input any more -- the deterministic scan
    budget and the feasibility check reach every case first, which is the point
    -- so it is exercised by shrinking the deadline instead. Otherwise the
    package's most emphasised safety mechanism would be entirely unverified.
    """
    monkeypatch.setattr(_recur, "SCAN_TIMEOUT_SECONDS", 0.05)
    result, elapsed = timed(
        lambda: expand(
            FakeContext(),
            ExpandRequest(
                recurrence=recurrence(SPARSE_RULES[0], "20200101T000000"), limit=10000
            ),
        )
    )
    assert result.error.code == "LIMIT_EXCEEDED", result
    assert "did not finish expanding" in result.error.message
    assert elapsed < 2.0, f"the deadline did not stop it: {elapsed:.1f}s"


def test_the_backstop_leaves_no_child_behind(monkeypatch):
    """A killed worker must be reaped, or repeated timeouts exhaust the host."""
    import multiprocessing

    monkeypatch.setattr(_recur, "SCAN_TIMEOUT_SECONDS", 0.05)
    for _ in range(5):
        expand(
            FakeContext(),
            ExpandRequest(
                recurrence=recurrence(SPARSE_RULES[0], "20200101T000000"), limit=10000
            ),
        )
    assert multiprocessing.active_children() == []
