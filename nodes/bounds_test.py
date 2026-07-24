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
from datetime import datetime, timedelta

import pytest

from gen.messages_pb2 import (
    BetweenRequest,
    ContainsRequest,
    CountRequest,
    ExpandRequest,
    RuleInput,
)
from gen.messages_pb2 import NextRequest, RuleInput, RuleParts
from nodes.between import between
from nodes.build import build
from nodes.contains import contains
from nodes.count import count
from nodes.expand import expand
from nodes.parse import parse
from nodes.next_occurrence import next_occurrence
from nodes import _recur
from nodes.testkit import NY, FakeContext, recurrence, recurrence_message
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
    """The feasibility check must not over-reach into merely-infrequent rules.

    Asserting only "no error" would be a rubber stamp: a zero-occurrence success
    would satisfy it while the node was in fact answering nothing. The property
    that matters is that the rule is not REFUSED as impossible -- it may still
    be too costly to reach, which is a different and honestly-reported outcome.
    """
    result, elapsed = timed(
        lambda: count(
            FakeContext(),
            CountRequest(recurrence=recurrence(rule, "20200101T000000"), limit=1),
        )
    )
    assert elapsed < CEILING, f"took {elapsed:.1f}s"
    assert result.error.code != "INVALID_RULE", (
        f"a possible rule was refused as impossible: {result.error.message}"
    )
    if result.error.code == "":
        assert result.count > 0, "a successful answer must contain an occurrence"
    else:
        assert result.error.code == "LIMIT_EXCEEDED", result


def test_finding_nothing_within_budget_is_reported_not_returned_as_empty():
    """Zero occurrences plus an unfinished search is no answer, not a partial one.

    Expand and Count previously returned count=0 with truncated=true and no
    error for rules that genuinely occur -- which reads as "this rule never
    fires" -- while NextOccurrence and Contains reported the same situation as
    LIMIT_EXCEEDED. Same envelope, opposite stories.
    """
    rule = "FREQ=SECONDLY;BYYEARDAY=366;BYHOUR=3"
    for result in (
        expand(
            FakeContext(),
            ExpandRequest(
                recurrence=recurrence(rule, "20200101T000000"), limit=10
            ),
        ),
        count(
            FakeContext(),
            CountRequest(recurrence=recurrence(rule, "20200101T000000"), limit=1),
        ),
    ):
        assert result.error.code == "LIMIT_EXCEEDED", result
        assert result.count == 0


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
    # Every rule here is unbounded (no COUNT, no UNTIL), so more occurrences
    # always exist beyond whatever was returned. Asserting that unconditionally
    # keeps the check alive: a version guarded on `count < limit` would quietly
    # stop running if the bound ever stopped firing.
    assert result.count <= limit
    assert result.truncated is True
    assert elapsed < 2.5, f"took {elapsed:.1f}s"


@pytest.mark.parametrize("rule", SPARSE_RULES)
def test_count_at_its_documented_default_serves_every_valid_rule(rule):
    """Count's own default limit is the walk's scan-step budget; the default
    request must work."""
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


def test_large_distinct_field_is_served_not_capped():
    """No package-level rule-text length cap: a field with hundreds of
    DISTINCT, individually valid entries (every positive and negative
    BYYEARDAY value RFC 5545 allows) serializes to well over the old 2048-char
    guard and must still be served -- payload size is the platform's job."""
    yeardays = list(range(1, 367)) + list(range(-366, 0))
    result = build(FakeContext(), RuleParts(freq="YEARLY", byyearday=yeardays))
    assert result.error.code == "", result.error.message
    assert len(result.rrule) > 2048, len(result.rrule)

    # The same COUNT of entries, all identical, collapses in the canonical form.
    collapsed = build(FakeContext(), RuleParts(freq="YEARLY", byyearday=[100] * 2000))
    assert collapsed.error.code == "", collapsed.error.message
    assert collapsed.rrule == "FREQ=YEARLY;BYYEARDAY=100"


# --- The wall-clock backstop, which nothing previously exercised -----------

def test_the_wall_clock_backstop_actually_fires(monkeypatch):
    """The backstop was the least-tested bound and the most-claimed one.

    No input has been found that reaches it: the deterministic scan budget and
    the feasibility checks get there first, which is the point. That is exactly
    why it is exercised by shrinking the deadline -- otherwise the package's most
    emphasised safety mechanism would be entirely unverified. "No input found"
    is not "no input exists", so the backstop stays and this test keeps it
    honest.
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


# --- Zones whose DST shift does not divide the rule's step ------------------
#
# Occurrences arrive in LOCAL order but are compared in UTC, and a spring-forward
# gap breaks the correspondence: in New York a rule at 02:00/02:30/03:00/03:30
# local maps to 07:00Z/07:30Z/07:00Z/07:30Z -- the third goes BACKWARDS. Every
# earlier DST test used New York at :30 past the hour, where a whole-hour shift
# lands the reordered instants on identical UTC keys and hides the disorder
# completely.

REORDERING_CASES = [
    # zone, rule, dtstart -- each crosses an offset change.
    #
    # The first five are ordinary DST shifts of 60 minutes or less. Calibrating
    # ONLY on those is how a 3-hour margin looked sufficient: every case sat
    # comfortably inside it, so the test could not fail for any zone the margin
    # did not already handle. The dateline cases below are the ones that matter
    # -- Pacific/Apia skipped 2011-12-30 entirely and Pacific/Kiritimati skipped
    # 1994-12-31, displacing occurrences by ~23 hours.
    ("Australia/Lord_Howe", "FREQ=MINUTELY;INTERVAL=20", "20261004T020000"),  # 30-min shift
    ("Pacific/Chatham", "FREQ=MINUTELY;INTERVAL=20", "20260927T020000"),      # 45-min offset
    ("America/Santiago", "FREQ=MINUTELY;INTERVAL=25", "20260906T230000"),
    ("America/New_York", "FREQ=DAILY;BYHOUR=2,3;BYMINUTE=0,30;BYSECOND=0", "20260308T020000"),
    ("Europe/Dublin", "FREQ=MINUTELY;INTERVAL=20", "20260329T003000"),
    ("Antarctica/Troll", "FREQ=MINUTELY;INTERVAL=25", "20260329T000000"),     # 2-hour shift
    ("Pacific/Apia", "FREQ=HOURLY", "20111230T200000"),                       # ~23h dateline move
    ("Pacific/Apia", "FREQ=MINUTELY;INTERVAL=25", "20111230T220000"),
    ("Pacific/Kiritimati", "FREQ=HOURLY", "19941230T200000"),                 # ~24h dateline move
    ("America/Juneau", "FREQ=HOURLY", "18671018T000000"),                     # Alaska purchase
    ("Antarctica/Vostok", "FREQ=HOURLY;INTERVAL=3", "19941031T000000"),       # +7h jump
    ("Antarctica/Macquarie", "FREQ=HOURLY;INTERVAL=6", "19480324T000000"),    # +10h jump
    ("Pacific/Kwajalein", "FREQ=HOURLY", "19930821T000000"),                  # ~24h dateline
]


def test_the_reorder_margin_exceeds_the_worst_offset_change_possible():
    """The margin is derived, not guessed.

    UTC offsets in the tz database span UTC-12:00 to UTC+14:00, so two instants'
    offsets differ by at most 26 hours and a later-in-local occurrence can be at
    most that much earlier in UTC. An earlier version used 3 hours, reasoning
    from ordinary DST shifts -- which is exactly the assumption the dateline
    cases above violate.
    """
    assert _recur.REORDER_MARGIN >= timedelta(hours=26)


def _utc_form(local_text, zone):
    from datetime import timezone
    from zoneinfo import ZoneInfo

    naive = datetime.strptime(local_text, "%Y%m%dT%H%M%S")
    return (
        naive.replace(tzinfo=ZoneInfo(zone))
        .astimezone(timezone.utc)
        .strftime("%Y%m%dT%H%M%SZ")
    )


@pytest.mark.parametrize("zone,rule,dtstart", REORDERING_CASES)
def test_every_occurrence_expand_emits_is_findable(zone, rule, dtstart):
    """The invariant that catches DST reordering: nodes must agree with Expand.

    For each occurrence Expand returns, Contains must confirm it -- in the form
    Expand emitted AND in absolute UTC form -- and Between must return it from a
    window bracketing its instant. Any disagreement means one node is silently
    losing a real occurrence.
    """
    def rec():
        return recurrence(rule, dtstart, tzid=zone)

    expanded = expand(FakeContext(), ExpandRequest(recurrence=rec(), limit=8))
    assert expanded.error.code == "", expanded.error.message
    assert expanded.count > 0

    for emitted in expanded.occurrences:
        as_utc = _utc_form(emitted, zone)

        local_hit = contains(
            FakeContext(), ContainsRequest(recurrence=rec(), candidate=emitted)
        )
        assert local_hit.contains is True, f"Contains lost {emitted} ({zone})"

        utc_hit = contains(
            FakeContext(), ContainsRequest(recurrence=rec(), candidate=as_utc)
        )
        assert utc_hit.contains is True, f"Contains lost {emitted} as {as_utc} ({zone})"

        # A one-minute window opening exactly on the occurrence's instant.
        end_utc = (
            datetime.strptime(as_utc, "%Y%m%dT%H%M%SZ") + timedelta(minutes=1)
        ).strftime("%Y%m%dT%H%M%SZ")
        window = between(
            FakeContext(),
            BetweenRequest(recurrence=rec(), start=as_utc, end=end_utc),
        )
        assert window.error.code == "", window.error.message
        assert emitted in list(window.occurrences), (
            f"Between lost {emitted} from [{as_utc},{end_utc}) ({zone})"
        )


@pytest.mark.parametrize("zone,rule,dtstart", REORDERING_CASES)
def test_next_occurrence_returns_the_earliest_not_the_first_seen(zone, rule, dtstart):
    """Across a gap the first occurrence in LOCAL order is not always the
    earliest in UTC, so a search that returns the first one it sees skips a
    real occurrence."""
    def rec():
        return recurrence(rule, dtstart, tzid=zone)

    expanded = expand(FakeContext(), ExpandRequest(recurrence=rec(), limit=8))
    keys = sorted((_utc_form(o, zone), o) for o in expanded.occurrences)
    earliest_utc, earliest_local = keys[0]

    # One second before the earliest, computed rather than string-sliced. The
    # previous expression was a no-op -- replacing the last three characters of
    # a string already ending "00Z" with "00Z" yields the same string -- so the
    # boundary this test is named for was never actually exercised.
    before = (
        datetime.strptime(earliest_utc, "%Y%m%dT%H%M%SZ") - timedelta(seconds=1)
    ).strftime("%Y%m%dT%H%M%SZ")
    result = next_occurrence(
        FakeContext(), NextRequest(recurrence=rec(), after=before)
    )
    assert result.error.code == "", result.error.message
    assert result.found is True
    # Whatever it returns must be the UTC-earliest of the candidates after that
    # instant -- never a later one skipped past.
    returned_utc = _utc_form(result.occurrence, zone)
    later = [u for u, _ in keys if u > before]
    assert returned_utc == min(later), (
        f"{zone}: returned {result.occurrence} ({returned_utc}), "
        f"earliest available was {min(later)}"
    )


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=YEARLY;BYMONTH=13;BYMONTHDAY=30",
        "FREQ=YEARLY;BYMONTH=0;BYMONTHDAY=1",
        "FREQ=YEARLY;BYMONTH=x;BYMONTHDAY=1",
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=y",
    ],
)
def test_feasibility_check_never_raises_on_unvalidated_values(rule):
    """It reasons over integers, so it must run AFTER they are validated.

    Running first meant an out-of-range or non-numeric BYMONTH reached it
    unchecked and escaped as a raw ValueError -- a traceback with internal paths
    reaching the caller, from the nodes that do not run in the isolated worker.
    """
    result = validate(FakeContext(), RuleInput(rrule=rule))
    assert result.valid is False
    assert result.error.code == "INVALID_RULE"
    assert "Traceback" not in result.error.message
    assert "ValueError" not in result.error.message


def test_serving_a_small_limit_never_pays_for_an_occurrence_not_asked_for():
    """A cheap answer must not be lost to an expensive one nobody requested.

    This rule's first occurrence is instant and its second is 28 years later.
    Fetching one past the limit to decide `truncated` spent the entire budget on
    the second and returned LIMIT_EXCEEDED with nothing -- discarding an answer
    that was already in hand.
    """
    rule = (
        "FREQ=SECONDLY;BYMONTH=2;BYMONTHDAY=29;BYHOUR=9;BYMINUTE=0;"
        "BYSECOND=0;BYDAY=SU"
    )
    result, elapsed = timed(
        lambda: expand(
            FakeContext(),
            ExpandRequest(
                recurrence=recurrence(rule, "20040228T000000"), limit=1
            ),
        )
    )
    assert result.error.code == "", result.error.message
    assert list(result.occurrences) == ["20040229T090000"]
    assert elapsed < 1.0, f"paid for an unrequested occurrence: {elapsed:.1f}s"


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=SECONDLY;BYYEARDAY=366;BYMONTH=1",
        "FREQ=SECONDLY;BYYEARDAY=1;BYMONTH=12",
        "FREQ=SECONDLY;BYYEARDAY=200;BYMONTH=1",
        "FREQ=MINUTELY;BYYEARDAY=1;BYMONTH=6",
    ],
)
def test_impossible_yearday_month_pairs_are_refused_immediately(rule):
    """Day 366 only lands in December, day 1 only in January.

    Without this the expander discovers it by stepping second by second toward
    its year ceiling -- the cheapest denial-of-service the package had, at ~3
    CPU-seconds per 109-byte request.
    """
    result, elapsed = timed(
        lambda: count(
            FakeContext(),
            CountRequest(recurrence=recurrence(rule, "20260101T000000"), limit=10),
        )
    )
    assert result.error.code == "INVALID_RULE", result
    assert "can never fall in" in result.error.message
    assert elapsed < 1.0, f"took {elapsed:.1f}s"


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=YEARLY;BYYEARDAY=366;BYMONTH=12",  # 366 IS in December
        "FREQ=YEARLY;BYYEARDAY=1;BYMONTH=1",
        "FREQ=YEARLY;BYYEARDAY=-1;BYMONTH=12",   # last day of the year
        "FREQ=YEARLY;BYYEARDAY=60;BYMONTH=2,3",  # 60 straddles Feb/Mar
    ],
)
def test_possible_yearday_month_pairs_are_not_refused(rule):
    """The check must not over-reach: day 60 is Feb 29 in a leap year and
    Mar 1 otherwise, so both months are legitimate."""
    assert validate(FakeContext(), RuleInput(rrule=rule)).valid is True


def test_a_package_fault_is_never_reported_as_the_callers_rule(monkeypatch):
    """INTERNAL exists so a caller is never sent to debug valid input."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated internal fault")

    # Patch the name the node actually calls: it imported check_rule directly,
    # so the binding in _recur is not the one it uses.
    import nodes.validate as validate_node

    monkeypatch.setattr(validate_node, "check_rule", boom)
    result = validate(FakeContext(), RuleInput(rrule="FREQ=DAILY;COUNT=3"))
    assert result.valid is False
    assert result.error.code == "INTERNAL"
    assert "Traceback" not in result.error.message
    assert "RuntimeError" not in result.error.message


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=SECONDLY;BYSETPOS=300;BYSECOND=0",
        "FREQ=SECONDLY;BYSETPOS=2;BYSECOND=0",
        "FREQ=MINUTELY;BYSETPOS=61;BYSECOND=0",
        "FREQ=HOURLY;BYSETPOS=-3601;BYSECOND=0",
    ],
)
def test_impossible_bysetpos_positions_are_refused_immediately(rule):
    """BYSETPOS selects the Nth instant in one interval; a SECONDLY interval
    holds exactly one. Asking for the 300th cost the full time budget to
    discover -- the last cheap way to buy it."""
    result, elapsed = timed(
        lambda: count(
            FakeContext(),
            CountRequest(recurrence=recurrence(rule, "20260101T000000"), limit=5),
        )
    )
    assert result.error.code == "INVALID_RULE", result
    assert "beyond what a" in result.error.message
    assert elapsed < 1.0, f"took {elapsed:.1f}s"


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=MONTHLY;BYSETPOS=-2;BYDAY=MO,TU,WE,TH,FR",  # RFC's own example
        "FREQ=MONTHLY;BYSETPOS=3;BYDAY=TU,WE,TH",         # RFC's own example
        "FREQ=YEARLY;BYSETPOS=366;BYDAY=MO",
        "FREQ=DAILY;BYSETPOS=1;BYHOUR=9",
    ],
)
def test_possible_bysetpos_positions_are_not_refused(rule):
    """The ceiling is per-interval capacity, which BY* parts only narrow -- so
    it can refuse the impossible but must never refuse the possible."""
    assert validate(FakeContext(), RuleInput(rrule=rule)).valid is True


# --- Killing tests for behaviours a mutation run found ungated --------------
#
# Each of these corresponds to a mutant that survived the suite: the code was
# deliberately broken and every test still passed. A constant the package
# documents but no test pins is a constant that can drift silently.

def test_between_flags_a_budget_stopped_window_as_truncated():
    """The silent-short-answer path, ungated in exactly the node it matters in."""
    result = between(
        FakeContext(),
        BetweenRequest(
            recurrence=recurrence("FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0", "20200101T000000"),
            start="20200101T000000",
            end="21000101T000000",
            limit=10000,
        ),
    )
    assert result.error.code == "", result.error.message
    assert result.count > 0
    assert result.truncated is True, "a budget-stopped window must not look complete"


def test_count_flags_a_budget_stopped_count_as_truncated():
    result = count(
        FakeContext(),
        CountRequest(
            recurrence=recurrence("FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0", "20200101T000000"),
            limit=10000,
        ),
    )
    assert result.error.code == "", result.error.message
    assert result.truncated is True


def test_the_deadline_is_finite_and_well_clear_of_real_work():
    """The bound exists to catch a HANG, not to police slowness.

    A rule that matches nothing scans inside a single library call where no
    deadline this code could check would fire, so the process must be killable.
    That requires the timeout to be finite -- but nothing more. Sizing it
    tightly (it was 5s) refused ordinary rules whenever container start-up ate
    the budget, so it is now far above the slowest measured expansion (~2.6s)
    and only ever trips on a genuine infinite scan.
    """
    assert 0 < _recur.SCAN_TIMEOUT_SECONDS <= 120.0
    assert _recur.SCAN_TIMEOUT_SECONDS >= 10.0, "too tight to survive a slow host"
    assert _recur.REAP_TIMEOUT_SECONDS <= 1.0
    # Startup is waited for separately and generously: exceeding it means the
    # PLATFORM failed to provide a worker, which no rule change can fix.
    assert _recur.STARTUP_TIMEOUT_SECONDS >= _recur.SCAN_TIMEOUT_SECONDS


def test_an_ambiguous_local_anchor_resolves_to_the_first_instant():
    """Fall-back 01:30 happens twice; which one is chosen changes the real UTC
    answer, and only an absolute window can observe it."""
    result = between(
        FakeContext(),
        BetweenRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=1", "20261101T013000", tzid=NY),
            start="20261101T052000Z",
            end="20261101T054000Z",
        ),
    )
    assert list(result.occurrences) == ["20261101T013000"], "must be 05:30Z, the first"
    later = between(
        FakeContext(),
        BetweenRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=1", "20261101T013000", tzid=NY),
            start="20261101T062000Z",
            end="20261101T064000Z",
        ),
    )
    assert list(later.occurrences) == [], "06:30Z is the second instant, not chosen"


def test_a_large_rdate_list_is_served_not_capped():
    """No package-level rdate/exdate count cap -- payload size is the
    platform's job. A list well over the old 1000-entry guard must still be
    accepted and merged in full."""
    at_old_cap = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence(
                "FREQ=DAILY;COUNT=1", "20260101T000000",
                rdate=[
                    (datetime(2027, 1, 1) + timedelta(days=n)).strftime("%Y%m%dT000000")
                    for n in range(1000)
                ],
            ),
            limit=10000,
        ),
    )
    assert at_old_cap.error.code == "", at_old_cap.error.message
    assert at_old_cap.count == 1001

    over_old_cap = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence(
                "FREQ=DAILY;COUNT=1", "20260101T000000",
                rdate=[
                    (datetime(2027, 1, 1) + timedelta(days=n)).strftime("%Y%m%dT000000")
                    for n in range(2000)
                ],
            ),
            limit=10000,
        ),
    )
    assert over_old_cap.error.code == "", over_old_cap.error.message
    assert over_old_cap.count == 2001


def test_a_trailing_newline_cannot_slip_through_the_rule_guard():
    """The guard uses \\Z rather than $ precisely because $ also matches before a
    trailing newline. Nothing pinned that choice."""
    assert validate(FakeContext(), RuleInput(rrule="FREQ=DAILY;COUNT=3\n")).valid is False
    assert validate(FakeContext(), RuleInput(rrule="FREQ=DAILY;COUNT=3")).valid is True


def test_years_below_1000_are_zero_padded():
    """strftime does not pad them, which produced instants dateutil then
    rejected. The docstring said so; no test did."""
    result = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=YEARLY;COUNT=2", "00010101T090000"), limit=5
        ),
    )
    assert list(result.occurrences) == ["00010101T090000", "00020101T090000"]


def test_a_host_dependent_tzid_is_refused():
    """'localtime' and 'Factory' resolve through host configuration, so the same
    request would expand differently on different machines. UTC does NOT --
    this test previously asserted it was refused, encoding the bug."""
    for host_dependent in ("localtime", "Factory"):
        result = expand(
            FakeContext(),
            ExpandRequest(
                recurrence=recurrence("FREQ=DAILY;COUNT=1", "20260101T000000", tzid=host_dependent),
                limit=1,
            ),
        )
        assert result.error.code == "INVALID_ARGUMENT", host_dependent


def test_negative_yeardays_resolve_to_the_right_months():
    """The negative arm of the yearday->month mapping changed 13 of 366 answers
    when broken, with the suite still green."""
    assert validate(FakeContext(), RuleInput(rrule="FREQ=YEARLY;BYYEARDAY=-1;BYMONTH=12")).valid is True
    assert validate(FakeContext(), RuleInput(rrule="FREQ=YEARLY;BYYEARDAY=-1;BYMONTH=1")).valid is False
    assert validate(FakeContext(), RuleInput(rrule="FREQ=YEARLY;BYYEARDAY=-365;BYMONTH=1")).valid is True
    assert validate(FakeContext(), RuleInput(rrule="FREQ=YEARLY;BYYEARDAY=-365;BYMONTH=6")).valid is False


def test_an_oversized_echoed_value_is_truncated_at_its_documented_length():
    result = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=1", "20260101T000000", tzid="A" * 5000),
            limit=1,
        ),
    )
    assert result.error.code == "INVALID_ARGUMENT"
    # The echoed VALUE is capped at MAX_ECHO; the surrounding sentence adds a
    # little more. What matters is that a 5000-character input cannot buy a
    # 5000-character response.
    assert len(result.error.message) < 300
    assert "5000 characters" in result.error.message


def test_validate_accepting_a_by_part_value_means_expand_accepts_it_too():
    """The property that was unasserted anywhere, and that let Validate bless
    BYSECOND=60 while every expansion node rejected it."""
    probes = []
    for part, freq, edge in (
        ("BYSECOND", "MINUTELY", 60), ("BYMINUTE", "HOURLY", 60),
        ("BYHOUR", "DAILY", 24), ("BYMONTH", "YEARLY", 13),
        ("BYMONTHDAY", "MONTHLY", 32), ("BYSETPOS", "MONTHLY", 400),
    ):
        for value in (edge - 1, edge):
            probes.append(f"FREQ={freq};{part}={value}")
    for rule in probes:
        valid = validate(FakeContext(), RuleInput(rrule=rule)).valid
        served = expand(
            FakeContext(),
            ExpandRequest(recurrence=recurrence(rule, "20260101T000000"), limit=1),
        )
        assert valid == (served.error.code == ""), (
            f"{rule}: Validate says {valid} but Expand says {served.error.code or 'ok'}"
        )


# --- Killing tests for mutants that survived: min() vs max() ---------------
#
# Both feasibility checks ask whether the LEAST demanding value in a list is
# satisfiable, because one satisfiable value makes the whole rule possible.
# Every earlier test used a single-valued list, where min and max coincide, so
# swapping them changed nothing the suite noticed -- while turning each check
# into a false refusal of a perfectly valid rule.

@pytest.mark.parametrize(
    "rule",
    [
        # Feb 30 is impossible but Feb 1 is not, so the rule CAN occur.
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=1,30",
        "FREQ=YEARLY;BYMONTH=4;BYMONTHDAY=15,31",
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30,29",
        "FREQ=YEARLY;BYMONTH=2,4;BYMONTHDAY=31,15",
    ],
)
def test_a_month_day_list_with_one_possible_value_is_accepted(rule):
    assert validate(FakeContext(), RuleInput(rrule=rule)).valid is True


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30,31",
        "FREQ=YEARLY;BYMONTH=4;BYMONTHDAY=31",
        "FREQ=YEARLY;BYMONTH=2,4;BYMONTHDAY=31",
    ],
)
def test_a_month_day_list_with_no_possible_value_is_refused(rule):
    result = validate(FakeContext(), RuleInput(rrule=rule))
    assert result.valid is False
    assert "can never occur" in result.error.message


# Values stay inside dateutil's own +/-366 BYSETPOS limit, so what is being
# tested is THIS package's per-frequency capacity check rather than the
# library's unrelated hard bound.
@pytest.mark.parametrize(
    "freq,positions",
    [
        ("SECONDLY", "1,300"),   # a SECONDLY interval holds 1 instant; 1 is reachable
        ("MINUTELY", "1,300"),
        ("MINUTELY", "60,300"),  # 60 is exactly the capacity
        ("HOURLY", "1,300"),
        ("DAILY", "300,-300"),
    ],
)
def test_a_setpos_list_with_one_reachable_position_is_accepted(freq, positions):
    rule = f"FREQ={freq};BYMONTHDAY=1;BYSETPOS={positions}"
    assert validate(FakeContext(), RuleInput(rrule=rule)).valid is True


@pytest.mark.parametrize(
    "freq,positions",
    [("SECONDLY", "2,300"), ("SECONDLY", "5,100"), ("MINUTELY", "61,300")],
)
def test_a_setpos_list_with_no_reachable_position_is_refused(freq, positions):
    rule = f"FREQ={freq};BYMONTHDAY=1;BYSETPOS={positions}"
    result = validate(FakeContext(), RuleInput(rrule=rule))
    assert result.valid is False
    assert "beyond what a" in result.error.message


# --- The year-9999 ceiling must be flagged, whatever else the rule carries --

@pytest.mark.parametrize(
    "rdate,exdate",
    [((), ()), (("99991228T120000",), ()), ((), ("99991229T000000",))],
)
def test_an_endless_rule_at_the_ceiling_is_never_reported_complete(rdate, exdate):
    """An RDATE adds occurrences and an EXDATE removes them; neither gives an
    infinite rule an end. Conditioning endlessness on their absence made a rule
    that had merely run out of calendar look exhaustive."""
    rec = recurrence("FREQ=DAILY", "99991228T000000", rdate=rdate, exdate=exdate)
    expanded = expand(FakeContext(), ExpandRequest(recurrence=rec, limit=50))
    counted = count(FakeContext(), CountRequest(recurrence=rec, limit=50))
    assert expanded.truncated is True, "an endless rule cannot be complete"
    assert counted.truncated is True
    assert expanded.count == counted.count


def test_a_count_left_unmet_by_the_ceiling_is_flagged_even_with_exdate():
    rec = recurrence(
        "FREQ=DAILY;COUNT=10", "99991228T000000", exdate=("99991229T000000",)
    )
    result = expand(FakeContext(), ExpandRequest(recurrence=rec, limit=50))
    assert result.count < 10
    assert result.truncated is True


def test_no_caller_input_makes_any_node_report_an_internal_fault():
    """INTERNAL means the package broke. A caller mistake must never earn it,
    or callers get sent to debug input that was fine."""
    malformed = [
        RuleParts(freq="WEEKLY", byday=["XX"]),
        RuleParts(freq="WEEKLY", byday=["Monday"]),
        RuleParts(freq="WEEKLY", byday=["+"]),
        RuleParts(freq="NOPE"),
        RuleParts(freq="DAILY", bysetpos=[1]),
    ]
    for parts in malformed:
        result = build(FakeContext(), parts)
        assert result.error.code != "INTERNAL", f"{parts} -> {result.error.message}"


# --- Ceiling detection must be exact, not proximate -------------------------
#
# An earlier attempt guessed from proximity to year 9999, and was wrong in both
# directions: it fired 99 years early for a rule with INTERVAL=100 (whose step
# is itself a century), and missed entirely when several EXDATEs removed the
# tail. COUNT bounds the RRULE alone, so the question is exactly answerable --
# ask the RRULE how many it produced.

@pytest.mark.parametrize(
    "rule,dtstart",
    [
        ("FREQ=YEARLY;INTERVAL=100;COUNT=3", "97000101T000000"),
        ("FREQ=MONTHLY;INTERVAL=1200;COUNT=3", "97000101T000000"),
        ("FREQ=YEARLY;INTERVAL=50;COUNT=3", "97000101T000000"),
        ("FREQ=DAILY;COUNT=3", "20260101T000000"),
    ],
)
def test_an_exdate_shortfall_away_from_the_ceiling_is_not_truncation(rule, dtstart):
    """The RRULE delivered everything it promised and the caller removed one.
    That is a complete answer, however wide the rule's own step happens to be."""
    excluded = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence(rule, dtstart, exdate=("98000101T000000", "20260102T000000")),
            limit=50,
        ),
    )
    assert excluded.error.code == "", excluded.error.message
    assert excluded.truncated is False, "an EXDATE the caller asked for is not truncation"


@pytest.mark.parametrize("exdates", [
    ("99991229T000000",),
    ("99991229T000000", "99991230T000000"),
    ("99991229T000000", "99991230T000000", "99991231T000000"),
])
def test_a_count_unmet_at_the_ceiling_is_flagged_however_many_exdates(exdates):
    """Parametrised over the tail-removal shape: a single-EXDATE test passed
    while the same bug survived with three."""
    result = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=10", "99991228T000000", exdate=exdates),
            limit=50,
        ),
    )
    assert result.error.code == "", result.error.message
    assert result.count < 10
    assert result.truncated is True, "COUNT was unmet because the calendar ended"


# --- A redundant list must not be refused for a length it never has ---------

@pytest.mark.parametrize(
    "freq,entry,repeats,expected",
    [
        ("WEEKLY", "MO", 700, "FREQ=WEEKLY;BYDAY=MO"),
        ("WEEKLY", "MO", 1000, "FREQ=WEEKLY;BYDAY=MO"),
        ("MONTHLY", "+1MO", 500, "FREQ=MONTHLY;BYDAY=1MO"),
    ],
)
def test_a_repeated_list_is_measured_after_deduplication(freq, entry, repeats, expected):
    """Validating the raw join measured a length the canonical form never has,
    so input that worked before was refused as too long."""
    result = build(FakeContext(), RuleParts(freq=freq, byday=[entry] * repeats))
    assert result.error.code == "", result.error.message
    assert result.rrule == expected


def test_deciding_the_ceiling_does_not_double_the_work():
    """The second RRULE pass must be charged, not free.

    Answering "did the RRULE reach its COUNT" re-walks the rule. Left uncharged
    it doubled the worst case (1.1s -> 2.3s) against a 3s deadline, so a host
    only 1.5x slower turned working input into a timeout.
    """
    request = CountRequest(
        recurrence=recurrence(
            "FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0;COUNT=230",
            "20200101T000000",
            exdate=("20200105T090000",),
        ),
        limit=10000,
    )
    result, elapsed = timed(lambda: count(FakeContext(), request))
    # This path costs ~2.3s of a 3s deadline on a fast machine, so on a slower
    # host -- the CI builder is about 2x slower -- it legitimately refuses. The
    # contract is a correct answer OR an honest refusal, never a wrong answer,
    # and asserting the fast-host outcome encoded this machine's speed as if it
    # were behaviour.
    if result.error.code:
        assert result.error.code == "LIMIT_EXCEEDED", result.error.message
    else:
        assert result.count == 229
        assert result.truncated is False, "an EXDATE shortfall is not truncation"
    assert elapsed < 6.0, f"neither answered nor refused promptly: {elapsed:.2f}s"


def test_repeated_entries_collapse_in_the_canonical_form():
    """A repeated entry collapses to one in the canonical form, regardless of
    how many times it was repeated -- no package-level count cap needed to
    serve this; see test_large_distinct_field_is_served_not_capped for the
    genuinely-distinct-and-large case."""
    for repeats in (1024, 1025, 3000):
        result = build(
            FakeContext(), RuleParts(freq="WEEKLY", byday=["MO"] * repeats)
        )
        assert result.error.code == "", f"{repeats}: {result.error.message}"
        assert result.rrule == "FREQ=WEEKLY;BYDAY=MO"


def test_the_same_byday_ordinal_spelled_two_ways_is_one_entry():
    """'1MO' and '01MO' are the same ordinal. Keeping both left the sort key
    non-total, so canonical text order depended on set iteration order."""
    messy = validate(
        FakeContext(), RuleInput(rrule="FREQ=YEARLY;BYDAY=53MO,1MO,53MO,01MO,SU,mo")
    )
    tidy = validate(FakeContext(), RuleInput(rrule="FREQ=YEARLY;BYDAY=MO,1MO,53MO,SU"))
    assert messy.valid and tidy.valid
    assert messy.normalized == tidy.normalized


def test_build_names_the_offending_byday_entry():
    """A malformed entry must be named, and must never be called our fault.

    This does NOT pin the validate-before-canonicalize ordering: the sorter is
    now defensive, so reverting the ordering alone leaves this green. The
    ordering is defence in depth rather than the load-bearing mechanism, and
    claiming otherwise would be a test asserting something it cannot see.
    """
    result = build(FakeContext(), RuleParts(freq="WEEKLY", byday=["MO", "XX", "WE"]))
    assert result.error.code == "INVALID_RULE"
    assert "XX" in result.error.message, result.error.message


# 110 is a deliberate BELOW-threshold control: it passed even with the bug, so
# it proves the assertion is not simply always-true. 130/200/230 are the cases
# that actually failed.
@pytest.mark.parametrize("rule_count", [110, 130, 200, 230])
def test_an_exdate_shortfall_is_never_asserted_as_truncation(rule_count):
    """The second walk costs what the first walk cost, so funding it from the
    first walk's leftovers left it unfunded exactly when it was needed -- and an
    undetermined answer was then reported as a positive claim of truncation.
    Abstention is not a finding.
    """
    result = count(
        FakeContext(),
        CountRequest(
            recurrence=recurrence(
                f"FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0;COUNT={rule_count}",
                "20200101T000000",
                exdate=("20200105T090000",),
            ),
            limit=10000,
        ),
    )
    # Near the top of the budget this can legitimately hit the documented
    # wall-clock deadline, especially under parallel load. The contract is a
    # correct answer OR an honest structured refusal -- never a wrong answer --
    # so both are accepted, but a WRONG answer is not.
    if result.error.code:
        assert result.error.code == "LIMIT_EXCEEDED", result.error.message
    else:
        assert result.count == rule_count - 1
        assert result.truncated is False


def test_rule_reached_its_count_abstains_when_it_cannot_tell():
    """The three-state return itself. `None` means undetermined -- neither a
    ceiling nor a completeness finding."""
    exp = _recur.build(
        recurrence_message(
            "FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0;COUNT=230",
            "20200101T000000",
            exdate=("20200105T090000",),
        )
    )
    assert exp.rule_reached_its_count(1.0) is None
    assert exp.rule_reached_its_count(_recur.MAX_SCAN_STEPS) is True


# --- Ceiling coverage partitioned by BUDGET GEOMETRY ------------------------
#
# Chosen from the three bands the second walk can land in, rather than from
# whatever last broke. The middle band is the one that produced a silent wrong
# answer: the first walk finished, but had spent more than half the budget, so
# funding the ceiling check from the remainder left the question unanswerable
# and the short answer was reported as complete.

@pytest.mark.parametrize(
    "dtstart,band",
    [
        ("99991001T000000", "first walk spends under half the budget"),
        ("99990601T000000", "first walk spends over half -- the silent-wrong-answer band"),
        # Not a ceiling-logic band: here the FIRST walk stops on the scan
        # budget, so the ceiling code is never reached and the assertion is
        # satisfied by budget exhaustion alone. Kept as coverage of that path,
        # labelled honestly rather than counted as a third band.
        ("99990401T000000", "first walk exhausts the budget outright"),
    ],
)
def test_a_ceiling_is_reported_in_every_budget_band(dtstart, band):
    """A COUNT the calendar cannot satisfy must never look complete, wherever
    the first walk's cost happens to land."""
    result = count(
        FakeContext(),
        CountRequest(
            recurrence=recurrence(
                "FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0;COUNT=1000",
                dtstart,
                exdate=("99990605T090000",),
            ),
            limit=10000,
        ),
    )
    assert result.count < 1000
    incomplete = result.truncated or bool(result.error.code)
    assert incomplete, f"claimed complete in the band where {band}"


def test_a_caller_s_own_exdates_are_never_reported_as_truncation():
    """The mirror of the ceiling bug, and the case that makes `None` reachable.

    Trailing EXDATEs decouple the two walks: they shorten the MERGED span, so
    the first walk finishes cheaply, while the rule-only walk still has to cross
    the whole span and may not afford it. The check then abstains -- and an
    abstention folded in with "did not reach its COUNT" reports a complete
    answer as truncated.

    Ground truth: the RRULE alone produces all 240 occurrences, so the calendar
    never ran out. The caller excluded 180. Sixty is the complete answer.
    """
    base = datetime(9990, 1, 1, 9, 0, 0)
    excluded = [
        (base + timedelta(days=day)).strftime("%Y%m%dT%H%M%S")
        for day in range(60, 240)
    ]
    result = count(
        FakeContext(),
        CountRequest(
            recurrence=recurrence(
                "FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0;COUNT=240",
                "99900101T000000",
                exdate=excluded,
            ),
            limit=10000,
        ),
    )
    # Same trade as above: expensive enough to refuse on a slow host, and a
    # refusal is contract-correct. What must never happen is count=60 reported
    # as truncated.
    if result.error.code:
        assert result.error.code == "LIMIT_EXCEEDED", result.error.message
    else:
        assert result.count == 60
        assert result.truncated is False, "an EXDATE the caller asked for is not truncation"


# --- The calendar edges, in zones that push instants over them --------------

@pytest.mark.parametrize("zone", ["Asia/Tokyo", "Europe/Berlin", "Asia/Kathmandu"])
def test_year_one_in_a_positive_offset_zone_is_comparable(zone):
    """Expand never compares, so it emitted occurrences its siblings crashed on.

    A year-0001 instant in a positive-offset zone has a UTC equivalent before
    year 1, and converting it raised OverflowError inside the comparison key --
    so Contains, Between and NextOccurrence returned INTERNAL for occurrences
    Expand had just produced.
    """
    rec = lambda: recurrence("FREQ=DAILY;COUNT=3", "00010101T000000", tzid=zone)
    expanded = expand(FakeContext(), ExpandRequest(recurrence=rec(), limit=3))
    assert expanded.error.code == "", expanded.error.message

    for emitted in expanded.occurrences:
        member = contains(
            FakeContext(), ContainsRequest(recurrence=rec(), candidate=emitted)
        )
        assert member.error.code == "", f"{zone}/{emitted}: {member.error.message}"
        assert member.contains is True

    # The NEGATIVE half, without which this test is satisfied BY the bug it
    # guards: saturating overflowing instants to one sentinel made everything in
    # the band compare equal, so `contains` was True unconditionally.
    for near_miss in ("00010101T003000", "00010101T081500"):
        stray = contains(
            FakeContext(), ContainsRequest(recurrence=rec(), candidate=near_miss)
        )
        assert stray.error.code == "", f"{zone}/{near_miss}: {stray.error.message}"
        assert stray.contains is False, f"{zone}: {near_miss} is not an occurrence"


def test_the_last_hours_of_the_calendar_in_a_negative_offset_zone():
    result = between(
        FakeContext(),
        BetweenRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=6", "20260307T013000", tzid=NY),
            start="20260307T013000",
            end="99991231T235959",
            limit=10,
        ),
    )
    assert result.error.code == "", result.error.message
    assert result.count == 6


def test_both_window_endpoints_inside_the_overflow_band():
    """The case the window test above never reached: its start does not
    overflow. With both endpoints saturated to the same sentinel, `end > start`
    came out false and a plainly valid window was rejected as inverted."""
    rec = lambda: recurrence("FREQ=HOURLY;COUNT=3", "99991231T210000", tzid=NY)
    window = between(
        FakeContext(),
        BetweenRequest(
            recurrence=rec(), start="99991231T210000", end="99991231T235959", limit=10
        ),
    )
    assert window.error.code == "", window.error.message
    assert list(window.occurrences) == [
        "99991231T210000",
        "99991231T220000",
        "99991231T230000",
    ]

    # And ordering must survive in the band: a non-occurrence is still not one.
    for near_miss in ("99991231T213000", "99991231T224500", "99991231T235959"):
        stray = contains(
            FakeContext(), ContainsRequest(recurrence=rec(), candidate=near_miss)
        )
        assert stray.contains is False, f"{near_miss} is not an occurrence"


# --- tzid: refuse host-dependent ids, not every short one -------------------

@pytest.mark.parametrize("zone", ["UTC", "GMT", "EST", "CET", "Etc/UTC", "America/New_York"])
def test_fixed_zones_are_accepted_however_they_are_spelled(zone):
    """TZID=UTC is among the commonest tzids in real calendar data. Requiring a
    '/' refused it -- along with GMT, EST, CET and ten more real fixed zones --
    while accepting the identical Etc/UTC."""
    result = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=2", "20260101T000000", tzid=zone),
            limit=3,
        ),
    )
    assert result.error.code == "", f"{zone}: {result.error.message}"
    assert result.count == 2


@pytest.mark.parametrize("zone", ["localtime", "Factory", "LOCALTIME", "Nope/Nope", "../etc/passwd"])
def test_host_dependent_or_unknown_zones_are_refused(zone):
    result = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=DAILY;COUNT=2", "20260101T000000", tzid=zone),
            limit=3,
        ),
    )
    assert result.error.code == "INVALID_ARGUMENT", zone


# --- RDATEs must not disguise a rule the calendar cut short -----------------

@pytest.mark.parametrize(
    "rdates,expected_count",
    [((), 1), (("99970101T000000", "99980101T000000"), 3)],
)
def test_rdates_cannot_mask_a_count_the_calendar_cut_short(rdates, expected_count):
    """A merged total reaching COUNT proves the RRULE was not truncated only if
    every occurrence came from the RRULE. RDATEs are added independently, so
    they padded the total back to COUNT and the shortfall check never ran --
    reporting a truncated recurrence as complete.
    """
    rec = lambda: recurrence("FREQ=YEARLY;COUNT=3", "99990101T000000", rdate=rdates)
    expanded = expand(FakeContext(), ExpandRequest(recurrence=rec(), limit=10))
    counted = count(FakeContext(), CountRequest(recurrence=rec(), limit=10))
    assert expanded.count == expected_count
    assert expanded.truncated is True, "the RRULE asked for 3 and the calendar gave 1"
    assert counted.truncated is True


# --- The interval-capacity ceiling must account for what populates it -------

@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=MINUTELY;BYSECOND=0,30;BYSETPOS=60",   # interval holds 2, not 60
        "FREQ=HOURLY;BYMINUTE=0;BYSECOND=0;BYSETPOS=2",
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0;BYSETPOS=2",
    ],
)
def test_a_setpos_beyond_the_narrowed_capacity_is_refused(rule):
    """A ceiling counted in seconds-per-interval was far too coarse: with
    BYSECOND=0,30 a MINUTELY interval holds two instants, so BYSETPOS=60 can
    never occur -- yet it passed the check and burned the whole time budget
    proving it, which falsified the README's own headroom claims."""
    result, elapsed = timed(
        lambda: expand(
            FakeContext(),
            ExpandRequest(
                recurrence=recurrence(rule, "19970902T090000"), limit=5
            ),
        )
    )
    assert result.error.code == "INVALID_RULE", result
    assert "beyond what a" in result.error.message
    assert elapsed < 1.0, f"took {elapsed:.1f}s"


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=MINUTELY;BYSECOND=0,30;BYSETPOS=2",    # exactly the capacity
        "FREQ=MINUTELY;BYSECOND=0,30;BYSETPOS=-2",
        "FREQ=HOURLY;BYMINUTE=0,30;BYSECOND=0;BYSETPOS=2",
        "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-2",  # the RFC's own example
        "FREQ=YEARLY;BYDAY=MO;BYSETPOS=20",
    ],
)
def test_the_narrowed_capacity_never_refuses_a_reachable_position(rule):
    """Still a one-sided over-approximation: BYDAY and BYMONTHDAY narrow the day
    count further and are deliberately NOT counted, so the ceiling can only ever
    be too generous, never too strict."""
    assert validate(FakeContext(), RuleInput(rrule=rule)).valid is True


def test_wkst_monday_collapses_like_interval_one():
    """Both are RFC defaults, so stating one and omitting it are the same rule.
    Dropping only INTERVAL=1 left `normalized` unusable as an equality key for
    any rule that spelled out WKST=MO."""
    with_wkst = validate(FakeContext(), RuleInput(rrule="FREQ=WEEKLY;BYDAY=MO,WE;WKST=MO"))
    without = validate(FakeContext(), RuleInput(rrule="FREQ=WEEKLY;BYDAY=MO,WE"))
    assert with_wkst.valid and without.valid
    assert with_wkst.normalized == without.normalized
    # A non-default WKST is still meaningful and must survive.
    sunday = validate(FakeContext(), RuleInput(rrule="FREQ=WEEKLY;BYDAY=MO,WE;WKST=SU"))
    assert sunday.normalized != without.normalized


def test_a_recurrence_that_never_occurs_before_year_9999_says_so():
    """An empty list flagged `truncated` reads as "nothing found, and more may
    exist" -- the opposite of what happened. The budget-exhausted case already
    reported this; the calendar-ceiling case did not."""
    result = expand(
        FakeContext(),
        ExpandRequest(
            recurrence=recurrence("FREQ=MONTHLY;BYDAY=MO;BYSETPOS=6", "19970902T090000"),
            limit=5,
        ),
    )
    assert result.count == 0
    assert result.error.code == "LIMIT_EXCEEDED", result
