"""Pins for the strict layer this package puts over python-dateutil.

Each `test_dateutil_*` test records a permissive behaviour that was OBSERVED in
python-dateutil 2.9.0.post0, paired with a test showing this package rejects it.
The pair matters: if a future dateutil tightens up, the observation test fails
loudly and tells us the guard is now redundant, rather than the guard quietly
becoming dead code nobody revisits.
"""

import itertools

import pytest
from dateutil.rrule import rrulestr
from datetime import datetime

from nodes._recur import (
    MAX_RULE_LEN,
    RecurError,
    canonical_rule,
    check_rule,
    effective_limit,
    parse_instant,
    probe_rule,
)

ANCHOR = datetime(1997, 9, 2, 9, 0, 0)


def reject(rule):
    """Return the RecurError code raised for a rule, or None if accepted."""
    try:
        check_rule(rule)
        probe_rule(rule)
    except RecurError as exc:
        return exc.code
    return None


# --- INTERVAL=0: dateutil yields the same instant forever -------------------

def test_dateutil_accepts_interval_zero_as_infinite_repeat():
    # Observed: not an error, and not progress either -- the same instant,
    # unboundedly. Anything iterating this without a step cap never terminates.
    got = list(itertools.islice(rrulestr("FREQ=DAILY;INTERVAL=0", dtstart=ANCHOR), 4))
    assert got == [ANCHOR] * 4


def test_rejects_interval_zero():
    assert reject("FREQ=DAILY;INTERVAL=0") == "INVALID_RULE"


# --- COUNT + UNTIL together: RFC 3.3.10 forbids it, dateutil allows it -------

def test_dateutil_accepts_count_and_until_together():
    got = list(rrulestr("FREQ=DAILY;COUNT=3;UNTIL=19970905T090000", dtstart=ANCHOR))
    assert len(got) == 3  # COUNT silently won; UNTIL was ignored


def test_rejects_count_with_until():
    assert reject("FREQ=DAILY;COUNT=3;UNTIL=19970905T090000Z") == "INVALID_RULE"


# --- Out-of-range BY* parts that dateutil turns into an empty result set -----

@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=YEARLY;BYMONTH=13",
        "FREQ=YEARLY;BYMONTHDAY=32",
        "FREQ=YEARLY;BYYEARDAY=400",
        "FREQ=YEARLY;BYWEEKNO=60",
    ],
)
def test_dateutil_silently_empties_out_of_range_by_parts(rule):
    # Observed: no exception; the rule simply never occurs. A typo becomes a
    # calendar that never fires, which is the worst possible failure mode.
    assert list(itertools.islice(rrulestr(rule, dtstart=ANCHOR), 1)) == []


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=YEARLY;BYMONTH=13",
        "FREQ=YEARLY;BYMONTHDAY=32",
        "FREQ=YEARLY;BYYEARDAY=400",
        "FREQ=YEARLY;BYWEEKNO=60",
    ],
)
def test_rejects_out_of_range_by_parts(rule):
    assert reject(rule) == "INVALID_RULE"


def test_accepts_legal_negative_by_parts():
    # The range checks must not over-reach: negative offsets are legal RFC.
    assert reject("FREQ=MONTHLY;BYMONTHDAY=-1") is None
    assert reject("FREQ=YEARLY;BYYEARDAY=-366") is None
    # "the last Sunday of the month" -- a numeric BYDAY prefix is legal on
    # MONTHLY/YEARLY only, which is why this is not FREQ=WEEKLY.
    assert reject("FREQ=MONTHLY;BYDAY=-1SU") is None
    assert reject("FREQ=WEEKLY;BYDAY=SU") is None


# --- Property smuggling through the rule field ------------------------------

def test_dateutil_lets_a_smuggled_dtstart_override_the_anchor():
    # Observed: the anchor argument is silently overridden by text inside the
    # rule string. A caller controlling only `rrule` could relocate the series.
    got = list(rrulestr("DTSTART:20200101T000000\nRRULE:FREQ=DAILY;COUNT=1", dtstart=ANCHOR))
    assert got == [datetime(2020, 1, 1, 0, 0, 0)]
    assert got[0] != ANCHOR


@pytest.mark.parametrize(
    "rule",
    [
        "DTSTART:20200101T000000\nRRULE:FREQ=DAILY;COUNT=1",
        "RRULE:FREQ=DAILY;COUNT=1",
        "FREQ=DAILY;COUNT=3\nEXDATE:19970903T090000",
        "FREQ=DAILY;COUNT=1\r\nRDATE:19970903T090000",
    ],
)
def test_rejects_anything_that_is_not_a_bare_recur_value(rule):
    assert reject(rule) == "INVALID_RULE"


# --- Structural rule checks -------------------------------------------------

@pytest.mark.parametrize(
    "rule,code",
    [
        ("", "INVALID_RULE"),
        ("COUNT=2", "INVALID_RULE"),                 # no FREQ
        ("FREQ=FORTNIGHTLY;COUNT=2", "INVALID_RULE"),
        ("FREQ=DAILY;XYZ=1", "INVALID_RULE"),        # unknown part
        ("FREQ=DAILY;;COUNT=2", "INVALID_RULE"),     # stray separator
        ("FREQ=DAILY;COUNT=2;COUNT=3", "INVALID_RULE"),  # duplicate part
        ("FREQ=DAILY;COUNT=", "INVALID_RULE"),       # empty value
        ("FREQ=DAILY;COUNT=abc", "INVALID_RULE"),
        ("FREQ=DAILY;COUNT=0", "INVALID_RULE"),
        ("FREQ=DAILY;BYSETPOS=1", "INVALID_RULE"),   # BYSETPOS needs a partner
        ("FREQ=DAILY;COUNT=999999", "LIMIT_EXCEEDED"),
        ("FREQ=WEEKLY;BYDAY=XX", "INVALID_RULE"),
        ("FREQ=DAILY;UNTIL=notadate", "INVALID_DATETIME"),
    ],
)
def test_structural_rejections(rule, code):
    assert reject(rule) == code


def test_rejects_oversized_rule_before_parsing():
    # The length guard fires on the raw string, so a pathological input never
    # reaches the parser at all.
    assert reject("FREQ=DAILY;" + "BYMONTH=1;" * 5000) == "LIMIT_EXCEEDED"
    assert len("FREQ=DAILY;" + "BYMONTH=1;" * 5000) > MAX_RULE_LEN


def test_case_is_normalized_not_rejected():
    assert reject("freq=daily;count=2") is None
    assert canonical_rule(check_rule("freq=daily;count=2")) == "FREQ=DAILY;COUNT=2"


def test_canonical_order_is_stable_regardless_of_input_order():
    a = canonical_rule(check_rule("COUNT=5;BYDAY=mo,we;FREQ=WEEKLY;WKST=SU"))
    b = canonical_rule(check_rule("FREQ=WEEKLY;WKST=SU;BYDAY=MO,WE;COUNT=5"))
    assert a == b == "FREQ=WEEKLY;COUNT=5;BYDAY=MO,WE;WKST=SU"


# --- Instants ---------------------------------------------------------------

@pytest.mark.parametrize(
    "value,kind",
    [
        ("19970902", "date"),
        ("19970902T090000", "floating"),
        ("19970902T090000Z", "utc"),
    ],
)
def test_parses_the_three_rfc_forms(value, kind):
    _, got = parse_instant(value, "dtstart")
    assert got == kind


@pytest.mark.parametrize(
    "value",
    ["", "1997-09-02", "19970902T0900", "19970230", "19971302", "19970902T250000", "tomorrow"],
)
def test_rejects_malformed_instants(value):
    with pytest.raises(RecurError) as exc:
        parse_instant(value, "dtstart")
    assert exc.value.code == "INVALID_DATETIME"


def test_leap_second_is_clamped_not_rejected():
    # RFC 5545 permits :60; Python's datetime does not model it.
    dt, _ = parse_instant("19970902T090060", "dtstart")
    assert dt.second == 59


# --- Limits -----------------------------------------------------------------

def test_effective_limit_bounds():
    assert effective_limit(0) == 100
    assert effective_limit(0, default=10000) == 10000
    assert effective_limit(50) == 50
    with pytest.raises(RecurError):
        effective_limit(10001)
    with pytest.raises(RecurError):
        effective_limit(-1)


# --- UNTIL form, which is tied to the anchor form by RFC 5545 3.3.10 --------

def test_accepts_utc_until_the_rfc_recommended_form():
    # Regression: probing every rule against one naive anchor rejected this,
    # which is the most common real-world way to bound a rule.
    assert reject("FREQ=WEEKLY;UNTIL=20260110T000000Z") is None


def test_accepts_floating_until():
    assert reject("FREQ=WEEKLY;UNTIL=20260110T000000") is None


def test_accepts_date_valued_until():
    assert reject("FREQ=WEEKLY;UNTIL=20260110") is None


# --- BYDAY ordinal: dateutil crashes from INSIDE the iterator --------------

def test_dateutil_crashes_on_an_out_of_range_byday_ordinal():
    # Observed: constructing the rule succeeds, so a construction-time probe
    # cannot see this. The IndexError only appears once the iterator walks off
    # its weekday mask -- which is why the guard has to be a range check and why
    # walk() also has to contain anything the expander throws.
    rule = rrulestr("FREQ=MONTHLY;BYDAY=54MO", dtstart=ANCHOR)  # no exception
    with pytest.raises(IndexError):
        next(iter(rule))


@pytest.mark.parametrize("ordinal", [0, 6, 8, 54, 99, -6, -54, -99])
def test_rejects_out_of_range_monthly_byday_ordinals(ordinal):
    # A month has at most 5 of any weekday. dateutil returns empty for 6..7 and
    # crashes from 8 up; both are rejected here as the nonsense they are.
    assert reject(f"FREQ=MONTHLY;BYDAY={ordinal}MO") == "INVALID_RULE"


@pytest.mark.parametrize("ordinal", [0, 54, 56, 99, -54, -99])
def test_rejects_out_of_range_yearly_byday_ordinals(ordinal):
    assert reject(f"FREQ=YEARLY;BYDAY={ordinal}MO") == "INVALID_RULE"


@pytest.mark.parametrize("entry", ["1MO", "5MO", "-1SU", "-5FR", "+2WE", "TH"])
def test_accepts_in_range_monthly_byday_ordinals(entry):
    assert reject(f"FREQ=MONTHLY;BYDAY={entry}") is None


@pytest.mark.parametrize("entry", ["1MO", "53MO", "-53FR", "20TU", "TH"])
def test_accepts_in_range_yearly_byday_ordinals(entry):
    assert reject(f"FREQ=YEARLY;BYDAY={entry}") is None


@pytest.mark.parametrize("freq", ["DAILY", "WEEKLY", "HOURLY", "SECONDLY"])
def test_rejects_a_numeric_byday_prefix_on_the_wrong_frequency(freq):
    # RFC 5545 forbids it; dateutil silently drops the prefix, widening
    # "the 2nd Monday" into "every Monday".
    assert reject(f"FREQ={freq};BYDAY=2MO") == "INVALID_RULE"
    assert reject(f"FREQ={freq};BYDAY=MO") is None  # bare weekday still fine


def test_dateutil_silently_ignores_a_numeric_byday_prefix_on_weekly():
    got = list(itertools.islice(rrulestr("FREQ=WEEKLY;BYDAY=2MO", dtstart=ANCHOR), 3))
    plain = list(itertools.islice(rrulestr("FREQ=WEEKLY;BYDAY=MO", dtstart=ANCHOR), 3))
    assert got == plain  # the "2nd" was discarded, not honoured


def test_three_digit_byday_ordinal_is_rejected_by_shape():
    assert reject("FREQ=MONTHLY;BYDAY=999MO") == "INVALID_RULE"
