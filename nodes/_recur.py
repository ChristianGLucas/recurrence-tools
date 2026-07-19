"""Shared marshalling between the Recurrence envelope and python-dateutil.

dateutil owns the algorithmically hard part: RFC 5545 recurrence expansion. This
module does three jobs around it, and deliberately nothing more:

1. **Bounds.** dateutil's iterators are lazy and frequently infinite. Every
   traversal here goes through `_walk`, which caps the number of iterator steps
   taken, not merely the number of results kept. Those are different numbers: a
   window query over "FREQ=SECONDLY" starting a century earlier would keep zero
   results while taking billions of steps.

2. **A strict layer over dateutil's lax parsing.** dateutil accepts several
   inputs RFC 5545 forbids, and it accepts them *silently*. Each check in
   `check_rule` exists because the permissive behaviour was observed, not
   assumed; `_recur_test.py` pins the observed behaviour so the check cannot rot
   into a no-op. The checks are confined to what dateutil gets wrong — the
   expansion semantics themselves are dateutil's and are not second-guessed.

3. **Form preservation.** The RFC 5545 form of `dtstart` (DATE / floating
   DATE-TIME / UTC DATE-TIME) is carried through expansion so occurrences are
   emitted the way the caller spelled the anchor.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Iterable, Iterator, List, Optional, Tuple

from dateutil.rrule import rrulestr, rruleset

# --- Bounds. Every one of these is enforced against the RAW input, before any
# --- parsing, allocation, or iteration that the value could make expensive.

MAX_RULE_LEN = 2048
MAX_DATE_LIST = 1000
MAX_LIMIT = 10000
DEFAULT_LIMIT = 100

# The iterator-step ceiling, counted over occurrences the expander YIELDS.
MAX_STEPS = 200_000

# The scan ceiling, counted over work the expander DOES. These are different
# numbers and the difference is a denial-of-service:
#
#   FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30
#
# is a valid rule that matches NOTHING (February has no 30th), so it yields no
# occurrences at all -- the yield-counting budget above never advances even once
# -- while the expander grinds hour by hour toward its year-9999 ceiling. Adding
# BYSECOND and BYMINUTE lists multiplies the cost of every one of those steps.
# Measured: ~4.5s for five years at one time-of-day combination, ~20s at 25.
#
# So the scan is bounded ahead of time instead, by rewriting the rule with an
# UNTIL the expander cannot run past. The budget is spent in units of
# "step x time-of-day combination", which is what the work actually costs, and
# is sized so the worst case stays around a second or two. In reach, that is
# ~82 years of a DAILY rule, ~575 of WEEKLY, and millennia of MONTHLY/YEARLY --
# past any real calendar horizon -- while sub-daily rules, where each step is
# cheap but there are vastly more of them, get correspondingly less.
#
# An UNTIL alone is NOT sufficient: with BYWEEKNO present the expander ignores
# it entirely and scans to its year ceiling regardless, which is one reason the
# RFC's part/frequency constraints below are enforced rather than assumed.
MAX_SCAN_WORK = 30_000

# Nominal seconds per step at each frequency, used only to convert a step
# allowance into an UNTIL instant. Approximate for MONTHLY/YEARLY, which is
# fine: this sizes a safety ceiling, it does not decide any occurrence.
FREQ_STEP_SECONDS = {
    "SECONDLY": 1,
    "MINUTELY": 60,
    "HOURLY": 3600,
    "DAILY": 86_400,
    "WEEKLY": 604_800,
    "MONTHLY": 2_678_400,   # 31 days
    "YEARLY": 31_622_400,   # 366 days
}

MAX_YEAR = 9999

FREQS = ("SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

# Canonical part order used when this package re-serializes a rule.
CANONICAL_ORDER = (
    "FREQ", "INTERVAL", "COUNT", "UNTIL", "BYSECOND", "BYMINUTE", "BYHOUR",
    "BYDAY", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH", "BYSETPOS", "WKST",
)

# BY* parts dateutil silently drops to an empty result set when out of range,
# rather than reporting the mistake. Range-checked here so a typo surfaces as an
# error instead of as "this rule simply never occurs".
SILENT_RANGES = {
    "BYMONTH": (1, 12, False),
    "BYMONTHDAY": (1, 31, True),
    "BYYEARDAY": (1, 366, True),
    "BYWEEKNO": (1, 53, True),
}

# RFC 5545 3.3.10 part/frequency constraints, quoted by their effect:
#   "BYWEEKNO ... MUST NOT be used when the FREQ rule part is set to anything
#    other than YEARLY."
#   "BYYEARDAY ... MUST NOT be specified when the FREQ rule part is set to
#    DAILY, WEEKLY, or MONTHLY."
#   "BYMONTHDAY ... MUST NOT be specified when the FREQ rule part is set to
#    WEEKLY."
PART_FREQUENCIES = {
    "BYWEEKNO": ("YEARLY",),
    "BYYEARDAY": ("YEARLY", "HOURLY", "MINUTELY", "SECONDLY"),
    "BYMONTHDAY": ("YEARLY", "MONTHLY", "DAILY", "HOURLY", "MINUTELY", "SECONDLY"),
}

INT_LIST_PARTS = (
    "BYSECOND", "BYMINUTE", "BYHOUR", "BYMONTHDAY", "BYYEARDAY",
    "BYWEEKNO", "BYMONTH", "BYSETPOS",
)

# \Z, not $: in Python `$` also matches just before a single trailing
# newline, which would let "FREQ=DAILY;COUNT=3\n" through the guard.
_BARE_RECUR = re.compile(r"\A[A-Za-z0-9=;,+\-]+\Z")
# RFC 5545: byday = [weeknum] weekday, weeknum = [plus/minus] ordwk,
# ordwk = 1*2DIGIT ;1 to 53. Two digits max, and the value is range-checked
# below -- dateutil crashes with an IndexError on an out-of-range ordinal.
_BYDAY = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")
# A month holds at most 5 of any weekday; a year at most 53.
MAX_BYDAY_ORDINAL_MONTHLY = 5
MAX_BYDAY_ORDINAL_YEARLY = 53
_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATETIME = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")

# The three RFC 5545 value forms this package round-trips.
KIND_DATE = "date"
KIND_FLOATING = "floating"
KIND_UTC = "utc"


class RecurError(Exception):
    """A deterministic, caller-facing rejection carrying a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(code: str, message: str) -> RecurError:
    return RecurError(code, message)


# --------------------------------------------------------------------------
# Rule text
# --------------------------------------------------------------------------

def check_rule(rule: str) -> List[Tuple[str, str]]:
    """Validate a bare RECUR value and return its parts in source order.

    Raises RecurError for anything RFC 5545 forbids, including the cases
    dateutil would otherwise accept silently.
    """
    if rule is None or rule == "":
        raise _err("INVALID_RULE", "rrule is required and must not be empty")
    if len(rule) > MAX_RULE_LEN:
        raise _err(
            "LIMIT_EXCEEDED",
            f"rrule is {len(rule)} characters; the maximum is {MAX_RULE_LEN}",
        )
    if not _BARE_RECUR.match(rule):
        # A RECUR value is only KEY=VALUE pairs joined by ';'. Anything else --
        # most importantly ':' or a line break -- means the caller passed a whole
        # iCalendar property or several lines. dateutil would parse a smuggled
        # "DTSTART:" line and silently override the envelope's own anchor, so the
        # value is refused rather than guessed at.
        raise _err(
            "INVALID_RULE",
            "rrule must be a bare RECUR value (KEY=VALUE pairs joined by ';') "
            "with no 'RRULE:' prefix, ':' character, or line break",
        )

    parts: List[Tuple[str, str]] = []
    seen = set()
    for chunk in rule.split(";"):
        if chunk == "":
            raise _err("INVALID_RULE", "rrule contains an empty part (stray ';')")
        if "=" not in chunk:
            raise _err("INVALID_RULE", f"rule part '{chunk}' is not KEY=VALUE")
        key, _, value = chunk.partition("=")
        key = key.upper()
        if key in seen:
            raise _err("INVALID_RULE", f"rule part '{key}' appears more than once")
        seen.add(key)
        if value == "":
            raise _err("INVALID_RULE", f"rule part '{key}' has an empty value")
        parts.append((key, value))

    keys = {k for k, _ in parts}
    if "FREQ" not in keys:
        raise _err("INVALID_RULE", "rrule must include FREQ")
    if "COUNT" in keys and "UNTIL" in keys:
        # RFC 5545 3.3.10: "UNTIL and COUNT MUST NOT occur in the same 'recur'".
        # dateutil accepts both and silently honours COUNT.
        raise _err("INVALID_RULE", "COUNT and UNTIL must not both appear in a rule")
    if "BYSETPOS" in keys and not (keys & {
        "BYSECOND", "BYMINUTE", "BYHOUR", "BYDAY", "BYMONTHDAY",
        "BYYEARDAY", "BYWEEKNO", "BYMONTH",
    }):
        # RFC 5545 3.3.10: BYSETPOS "MUST only be used in conjunction with
        # another BYxxx rule part". dateutil ignores a lone BYSETPOS.
        raise _err(
            "INVALID_RULE",
            "BYSETPOS must be used together with another BY* rule part",
        )

    freq = next((v.upper() for k, v in parts if k == "FREQ"), "")

    # RFC 5545 3.3.10 forbids certain BY* parts at certain frequencies. dateutil
    # accepts them all, and some combinations are actively dangerous: BYWEEKNO on
    # a sub-yearly frequency defeats even an explicit UNTIL, leaving the expander
    # scanning toward its year ceiling with no way to stop it. Enforcing the
    # spec's own constraints is both correct and what makes the scan bounded.
    for part, allowed in PART_FREQUENCIES.items():
        if part in keys and freq not in allowed:
            raise _err(
                "INVALID_RULE",
                f"{part} must not be used with FREQ={freq}; "
                f"RFC 5545 allows it only with {', '.join(allowed)}",
            )

    for key, value in parts:
        _check_part(key, value, freq)
    return parts


def _check_part(key: str, value: str, freq: str = "") -> None:
    if key == "FREQ":
        if value.upper() not in FREQS:
            raise _err(
                "INVALID_RULE",
                f"FREQ must be one of {', '.join(FREQS)}, got '{value}'",
            )
        return

    if key == "INTERVAL":
        n = _int_part(key, value)
        if n < 1:
            # dateutil accepts INTERVAL=0 and yields the same instant forever --
            # an unbounded generator that never advances. Refused outright.
            raise _err("INVALID_RULE", f"INTERVAL must be 1 or greater, got {n}")
        return

    if key == "COUNT":
        n = _int_part(key, value)
        if n < 1:
            raise _err("INVALID_RULE", f"COUNT must be 1 or greater, got {n}")
        if n > MAX_LIMIT:
            raise _err(
                "LIMIT_EXCEEDED",
                f"COUNT is {n}; the maximum this package expands is {MAX_LIMIT}",
            )
        return

    if key == "UNTIL":
        parse_instant(value, "UNTIL")
        return

    if key == "WKST":
        if value.upper() not in WEEKDAYS:
            raise _err("INVALID_RULE", f"WKST must be a weekday, got '{value}'")
        return

    if key == "BYDAY":
        for item in value.split(","):
            match = _BYDAY.match(item.upper())
            if not match:
                raise _err("INVALID_RULE", f"invalid BYDAY entry '{item}'")
            ordinal = match.group(1)
            if ordinal is not None:
                n = int(ordinal)
                # RFC 5545 3.3.10: "The BYDAY rule part MUST NOT be specified
                # with a numeric value when the FREQ rule part is not set to
                # MONTHLY or YEARLY." dateutil silently ignores the prefix
                # instead, quietly widening "the 2nd Monday" into "every Monday".
                if freq not in ("MONTHLY", "YEARLY"):
                    raise _err(
                        "INVALID_RULE",
                        f"BYDAY entry '{item}' has a numeric prefix, which "
                        f"requires FREQ=MONTHLY or FREQ=YEARLY, not FREQ={freq or '?'}",
                    )
                # The meaningful range depends on the frequency: a month holds at
                # most 5 of any weekday, a year at most 53. dateutil range-checks
                # neither -- past its limit the iterator walks off its weekday
                # mask and raises IndexError, and it does so on ITERATION, not on
                # construction, so probing the rule alone cannot catch it.
                limit = MAX_BYDAY_ORDINAL_MONTHLY if freq == "MONTHLY" else MAX_BYDAY_ORDINAL_YEARLY
                if not 1 <= abs(n) <= limit:
                    raise _err(
                        "INVALID_RULE",
                        f"BYDAY ordinal {n} in '{item}' is out of range for "
                        f"FREQ={freq}; allowed 1..{limit} or -{limit}..-1",
                    )
        return

    if key in INT_LIST_PARTS:
        rng = SILENT_RANGES.get(key)
        for item in value.split(","):
            n = _int_part(key, item)
            if rng is not None:
                lo, hi, signed = rng
                ok = lo <= n <= hi or (signed and -hi <= n <= -lo)
                if not ok:
                    allowed = f"{lo}..{hi}" + (f" or -{hi}..-{lo}" if signed else "")
                    raise _err(
                        "INVALID_RULE",
                        f"{key} entry {n} is out of range; allowed {allowed}",
                    )
        return

    raise _err("INVALID_RULE", f"unknown rule part '{key}'")


def _int_part(key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise _err("INVALID_RULE", f"{key} expects an integer, got '{value}'")


# A fixed anchor used only to make dateutil parse a rule, pinned so that
# Validate/Parse/Build stay deterministic rather than depending on today's date.
PROBE_DTSTART = datetime(1997, 9, 2, 9, 0, 0)


def probe_rule(rule: str) -> None:
    """Let dateutil reject anything `check_rule` does not cover.

    Constructing the rule is enough to surface dateutil's own argument errors
    (BYHOUR, BYMINUTE, BYSECOND and BYSETPOS ranges among them). The rule is
    deliberately not iterated here: a rule that is valid but matches nothing --
    "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30" -- would iterate to dateutil's year
    ceiling before yielding, and that cost belongs to expansion, not validation.

    The probe anchor is matched to the rule's own UNTIL form. RFC 5545 3.3.10
    ties the two together -- a UTC UNTIL belongs with a UTC/TZID anchor, a
    floating UNTIL with a floating one -- and dateutil enforces that pairing.
    Probing every rule against one fixed naive anchor would therefore reject
    every rule using the RFC's recommended UTC UNTIL form, purely as an artifact
    of how this rule-only check anchors itself.
    """
    anchor = PROBE_DTSTART
    for key, value in _split_parts(rule):
        if key == "UNTIL" and value.endswith("Z"):
            anchor = PROBE_DTSTART.replace(tzinfo=timezone.utc)
            break
    try:
        rrulestr(rule, dtstart=anchor, cache=False)
    except RecurError:
        raise
    except Exception as exc:
        raise _err("INVALID_RULE", f"rrule was rejected by the expander: {exc}")


def _split_parts(rule: str) -> List[Tuple[str, str]]:
    """Best-effort KEY=VALUE split, for inspection only (no validation)."""
    out = []
    for chunk in rule.split(";"):
        key, _, value = chunk.partition("=")
        out.append((key.upper(), value))
    return out


def canonical_rule(parts: Iterable[Tuple[str, str]]) -> str:
    """Re-serialize validated parts in this package's canonical order."""
    by_key = {k: v for k, v in parts}
    ordered = [k for k in CANONICAL_ORDER if k in by_key]
    return ";".join(f"{k}={_canonical_value(k, by_key[k])}" for k in ordered)


def _canonical_value(key: str, value: str) -> str:
    if key in ("FREQ", "WKST"):
        return value.upper()
    if key == "BYDAY":
        # Strip a redundant leading '+' so "+1MO" and "1MO" -- the same rule --
        # normalize identically. INT_LIST_PARTS already does this via int();
        # leaving BYDAY out made `normalized` unusable as an equality key.
        return ",".join(
            (item[1:] if item.startswith("+") else item).upper()
            for item in value.split(",")
        )
    if key in INT_LIST_PARTS:
        return ",".join(str(int(item)) for item in value.split(","))
    if key in ("INTERVAL", "COUNT"):
        return str(int(value))
    return value


# --------------------------------------------------------------------------
# Instants
# --------------------------------------------------------------------------

def _fmt(dt: datetime, with_time: bool = True, utc_suffix: bool = False) -> str:
    """Format an instant in RFC 5545 form with an explicitly padded year.

    strftime("%Y") does not zero-pad years below 1000 on Linux, turning year 1
    into "1" and producing "10101T130000" -- which is not a valid RFC 5545
    instant and which dateutil rejects. Every instant this package emits goes
    through here so an early-year anchor round-trips like any other.
    """
    text = f"{dt.year:04d}{dt.month:02d}{dt.day:02d}"
    if with_time:
        text += f"T{dt.hour:02d}{dt.minute:02d}{dt.second:02d}"
        if utc_suffix:
            text += "Z"
    return text


def parse_instant(value: str, field: str) -> Tuple[datetime, str]:
    """Parse an RFC 5545 DATE or DATE-TIME into a naive datetime plus its form."""
    if not value:
        raise _err("INVALID_DATETIME", f"{field} is required and must not be empty")

    m = _DATE.match(value)
    if m:
        return _assemble(value, field, *(int(g) for g in m.groups())), KIND_DATE

    m = _DATETIME.match(value)
    if m:
        y, mo, d, h, mi, s, z = m.groups()
        dt = _assemble(value, field, int(y), int(mo), int(d), int(h), int(mi), int(s))
        return dt, (KIND_UTC if z == "Z" else KIND_FLOATING)

    raise _err(
        "INVALID_DATETIME",
        f"{field} '{value}' is not an RFC 5545 DATE ('19970902') or "
        f"DATE-TIME ('19970902T090000' / '19970902T090000Z')",
    )


def _assemble(raw: str, field: str, y: int, mo: int, d: int,
              h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    if s == 60:
        # RFC 5545 permits a leap second in BYSECOND; Python's datetime does not
        # model one. Clamp so a legal calendar file is not rejected outright.
        s = 59
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError as exc:
        raise _err("INVALID_DATETIME", f"{field} '{raw}' is not a real instant: {exc}")


def zone_for(kind: str, tzid: str) -> Optional[tzinfo]:
    """Resolve the tzinfo an anchor of this form and tzid expands in."""
    if tzid:
        if kind == KIND_UTC:
            raise _err(
                "INVALID_ARGUMENT",
                "tzid must not be set when dtstart is already UTC (ends with 'Z')",
            )
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(tzid)
        except Exception:
            raise _err("INVALID_ARGUMENT", f"unknown IANA time-zone id '{tzid}'")
    return timezone.utc if kind == KIND_UTC else None


def localize(dt: datetime, zone: Optional[tzinfo]) -> datetime:
    return dt if zone is None else dt.replace(tzinfo=zone)


def coerce(value: str, field: str, zone: Optional[tzinfo],
           anchor_kind: Optional[str] = None) -> datetime:
    """Parse an auxiliary instant into something comparable with the anchor.

    Mixing an aware anchor with a naive auxiliary value (or the reverse) raises
    TypeError deep inside dateutil's comparisons, so the mismatch is caught here
    and reported against the field that caused it.

    A DATE/DATE-TIME mismatch is rejected for a different reason: it compares
    cleanly but answers the wrong question. Testing the DATE-TIME "19970902T090000"
    against a DATE-valued recurrence would silently compare 09:00 against
    midnight and report a confident "not an occurrence" for a day the recurrence
    does occur on.
    """
    dt, kind = parse_instant(value, field)
    if anchor_kind is not None and (anchor_kind == KIND_DATE) != (kind == KIND_DATE):
        expected = "a DATE ('19970902')" if anchor_kind == KIND_DATE else \
            "a DATE-TIME ('19970902T090000')"
        raise _err(
            "INVALID_ARGUMENT",
            f"{field} '{value}' must use the same form as dtstart; expected {expected}",
        )
    if kind == KIND_UTC:
        if zone is None:
            raise _err(
                "INVALID_ARGUMENT",
                f"{field} '{value}' is UTC but dtstart is not; use a matching form",
            )
        return dt.replace(tzinfo=timezone.utc)
    if zone is None:
        return dt
    return dt.replace(tzinfo=zone)


def format_instant(dt: datetime, kind: str, zone: Optional[tzinfo]) -> str:
    """Emit an occurrence in the same RFC 5545 form as the anchor."""
    if kind == KIND_DATE:
        return _fmt(dt, with_time=False)
    if kind == KIND_UTC:
        aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        return _fmt(aware.astimezone(timezone.utc), utc_suffix=True)
    # Floating, or local to tzid: emit wall-clock time, which is what the
    # TZID form means. The zone travels separately, in Recurrence.tzid.
    return _fmt(dt)


# --------------------------------------------------------------------------
# Recurrence set
# --------------------------------------------------------------------------

def _scan_horizon(dtstart: datetime, parts: List[Tuple[str, str]]) -> Tuple[datetime, int]:
    """Pick the instant past which the expander is not allowed to scan.

    The allowance shrinks as each step gets more expensive, so the product --
    the real cost -- stays inside MAX_SCAN_WORK however the rule is shaped.
    """
    by = {k: v for k, v in parts}
    freq = by.get("FREQ", "DAILY").upper()
    interval = max(1, int(by.get("INTERVAL", "1")))

    # Every step materializes one time-of-day per combination of these.
    combos = 1
    for part in ("BYSECOND", "BYMINUTE", "BYHOUR"):
        if part in by:
            combos *= max(1, len(by[part].split(",")))

    steps = max(1, MAX_SCAN_WORK // combos)
    span = steps * interval * FREQ_STEP_SECONDS.get(freq, 86_400)
    try:
        return dtstart + timedelta(seconds=span), steps
    except OverflowError:
        return dtstart.replace(year=MAX_YEAR, month=12, day=31), steps


def _rule_with_horizon(parts: List[Tuple[str, str]], horizon: datetime,
                       zone: Optional[tzinfo]) -> Tuple[str, Optional[int], bool]:
    """Rewrite a rule so the expander stops at `horizon`.

    Returns the rewritten rule, the COUNT this package must now enforce itself,
    and whether the horizon actually cut the rule short.

    COUNT is stripped rather than kept: RFC 5545 forbids COUNT and UNTIL in one
    rule, and a COUNT rule that matches rarely would otherwise scan forever
    hunting for its Nth occurrence. Counting here instead preserves the exact
    semantics -- the first N occurrences -- under a bounded scan.
    """
    by = {k: v for k, v in parts}
    count = int(by["COUNT"]) if "COUNT" in by else None

    existing = by.get("UNTIL")
    capped = True
    if existing is not None:
        until_dt, until_kind = parse_instant(existing, "UNTIL")
        if until_kind == KIND_UTC and zone is not None:
            until_dt = until_dt.replace(tzinfo=timezone.utc).astimezone(
                zone).replace(tzinfo=None)
        if until_dt <= horizon.replace(tzinfo=None):
            # The rule already ends before the ceiling, so nothing is truncated.
            return canonical_rule(parts), count, False

    naive_horizon = horizon.replace(tzinfo=None)
    if zone is not None:
        # RFC 5545 3.3.10 and dateutil both require UNTIL in UTC once the anchor
        # carries a zone, so the ceiling is converted rather than passed through.
        until_text = _fmt(
            naive_horizon.replace(tzinfo=zone).astimezone(timezone.utc),
            utc_suffix=True,
        )
    else:
        until_text = _fmt(naive_horizon)

    kept = [(k, v) for k, v in parts if k not in ("COUNT", "UNTIL")]
    kept.append(("UNTIL", until_text))
    return canonical_rule(kept), count, capped


class Expansion:
    """A parsed recurrence, ready to be walked under a bounded scan."""

    def __init__(self, rset: rruleset, kind: str, zone: Optional[tzinfo],
                 count: Optional[int] = None, capped: bool = False,
                 horizon: Optional[datetime] = None):
        self.rset = rset
        self.kind = kind
        self.zone = zone
        # COUNT lifted out of the rule, now enforced during the walk.
        self.count = count
        # True when the scan ceiling cut the rule short, so running out of
        # occurrences means "stopped early", not "genuinely exhausted".
        self.capped = capped
        self.horizon = horizon

    def format(self, dt: datetime) -> str:
        return format_instant(dt, self.kind, self.zone)

    def instant(self, value: str, field: str) -> datetime:
        return coerce(value, field, self.zone, self.kind)


def build(recurrence) -> Expansion:
    """Turn a Recurrence message into a bounded, validated Expansion."""
    if recurrence is None:
        raise _err("INVALID_ARGUMENT", "recurrence is required")

    parts = check_rule(recurrence.rrule)

    if len(recurrence.rdate) > MAX_DATE_LIST:
        raise _err(
            "LIMIT_EXCEEDED",
            f"rdate has {len(recurrence.rdate)} entries; the maximum is {MAX_DATE_LIST}",
        )
    if len(recurrence.exdate) > MAX_DATE_LIST:
        raise _err(
            "LIMIT_EXCEEDED",
            f"exdate has {len(recurrence.exdate)} entries; the maximum is {MAX_DATE_LIST}",
        )

    dt, kind = parse_instant(recurrence.dtstart, "dtstart")
    zone = zone_for(kind, recurrence.tzid)
    dtstart = localize(dt, zone)

    horizon, _ = _scan_horizon(dt, parts)
    bounded_rule, count, capped = _rule_with_horizon(parts, horizon, zone)

    try:
        rule = rrulestr(bounded_rule, dtstart=dtstart, cache=False)
    except RecurError:
        raise
    except Exception as exc:
        raise _err("INVALID_RULE", f"rrule could not be parsed: {exc}")

    rset = rruleset(cache=False)
    if count is None:
        rset.rrule(rule)
    else:
        # COUNT bounds the RRULE's OWN occurrences; RDATEs are added on top and
        # EXDATEs removed afterwards (RFC 5545 3.8.5.3). Applying it to the
        # merged stream instead would let an RDATE consume the rule's quota and
        # let an EXDATE be silently backfilled. Since COUNT had to be lifted out
        # of the rule to bound the scan, the rule's own occurrences are
        # materialized here -- at most MAX_LIMIT of them -- and re-added as
        # explicit dates, which keeps the ordering the RFC specifies.
        satisfied = False
        for occurrence in rule:
            rset.rdate(occurrence)
            count -= 1
            if count == 0:
                satisfied = True
                break
        capped = capped and not satisfied
    count = None  # fully applied above; the walk must not re-apply it
    for value in recurrence.rdate:
        rset.rdate(coerce(value, "rdate", zone, kind))
    for value in recurrence.exdate:
        rset.exdate(coerce(value, "exdate", zone, kind))
    return Expansion(rset, kind, zone, count=count, capped=capped, horizon=horizon)


def walk(exp: Expansion, budget: int = MAX_STEPS) -> Iterator[datetime]:
    """Yield occurrences in ascending order under a hard step budget.

    The budget counts occurrences *visited*, including ones a caller discards,
    which is what makes a far-future window or an unreachable candidate
    terminate instead of running away.
    """
    steps = 0
    yielded = 0
    iterator = iter(exp.rset)
    while True:
        if exp.count is not None and yielded >= exp.count:
            # The rule's own COUNT, lifted out so the scan could be bounded.
            return
        # Failures inside the expander surface HERE, on iteration, not when the
        # rule was constructed -- so no amount of up-front rule checking can be
        # relied on to have caught them. Anything that escapes becomes a
        # structured error, because the alternative is a raw traceback (and its
        # internal paths) reaching the caller. The node contract is that bad
        # input is reported, never raised.
        try:
            dt = next(iterator)
        except StopIteration:
            if exp.capped:
                # The expander did not run out of occurrences -- it ran into the
                # scan ceiling. Returning "no more" here would be a wrong answer
                # dressed as an empty one, so it is reported instead.
                raise _err(
                    "LIMIT_EXCEEDED",
                    "the recurrence was scanned as far as "
                    f"{_fmt(exp.horizon)} without satisfying this "
                    "request; narrow the rule, window, or limit",
                )
            return
        except RecurError:
            raise
        except Exception as exc:
            raise _err(
                "INVALID_RULE",
                f"the expander failed while producing occurrences: "
                f"{type(exc).__name__}: {exc}",
            )
        steps += 1
        yielded += 1
        if steps > budget:
            raise _err(
                "LIMIT_EXCEEDED",
                f"the recurrence produced more than {budget} occurrences before "
                "satisfying this request; narrow the rule, window, or limit",
            )
        yield dt


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------

# The wall-clock ceiling for one expansion, enforced in a separate process.
SCAN_TIMEOUT_SECONDS = 5.0


def _isolated_entry(module_name: str, data: bytes, queue) -> None:
    import importlib

    try:
        queue.put(("ok", importlib.import_module(module_name).compute(data)))
    except RecurError as exc:
        queue.put(("err", exc.code, exc.message))
    except Exception as exc:  # pragma: no cover - defence in depth
        queue.put(("err", "INVALID_RULE", f"{type(exc).__name__}: {exc}"))


def isolate(module_name: str, input_msg):
    """Run a node's computation in a process that can be killed.

    The scan ceiling and the step budget bound the cases that can be bounded
    from the outside, but they cannot bound all of them. A rule that matches
    NOTHING never yields, so a yield-counting budget never advances; and the
    expander does not reliably honour the UNTIL used to cap its scan -- with
    certain part combinations it keeps going regardless. The hang then happens
    inside a single call into the library, where no deadline this code could
    check would ever be reached.

    Some failures can only be prevented, not caught. So the work runs in a
    child process with a hard wall-clock limit: if it overruns, the process is
    killed and the caller gets a structured LIMIT_EXCEEDED, with no runaway
    left behind burning CPU.

    Returns (serialized_output, None) on success, or (None, error_dict). The
    caller constructs its own output message from that -- both because the
    error has to be attached to the node's own type, and because `axiom
    validate` requires a node body to visibly construct its declared output.
    """
    import multiprocessing

    try:
        ctx = multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - platform without fork
        import importlib

        return importlib.import_module(module_name).compute(
            input_msg.SerializeToString()
        ), None

    queue = ctx.Queue(1)
    proc = ctx.Process(
        target=_isolated_entry,
        args=(module_name, input_msg.SerializeToString(), queue),
        daemon=True,
    )
    proc.start()
    proc.join(SCAN_TIMEOUT_SECONDS)

    if proc.is_alive():
        proc.terminate()
        proc.join(1.0)
        if proc.is_alive():  # pragma: no cover - terminate is reliable here
            proc.kill()
            proc.join(1.0)
        return None, {
            "code": "LIMIT_EXCEEDED",
            "message": (
                f"the recurrence did not finish expanding within "
                f"{SCAN_TIMEOUT_SECONDS:.0f}s; it is too costly to evaluate. "
                "Narrow the rule, window, or limit"
            ),
        }

    if queue.empty():  # pragma: no cover - child died without reporting
        return None, {
            "code": "LIMIT_EXCEEDED",
            "message": "the recurrence could not be expanded within its budget",
        }

    result = queue.get()
    if result[0] == "err":
        return None, {"code": result[1], "message": result[2]}
    return result[1], None


def effective_limit(limit: int, default: int = DEFAULT_LIMIT) -> int:
    if limit < 0:
        raise _err("INVALID_ARGUMENT", f"limit must not be negative, got {limit}")
    if limit == 0:
        return default
    if limit > MAX_LIMIT:
        raise _err(
            "INVALID_ARGUMENT",
            f"limit must be at most {MAX_LIMIT}, got {limit}",
        )
    return limit


def take(exp: Expansion, limit: int) -> Tuple[List[str], bool]:
    """Collect up to `limit` formatted occurrences; report whether more remain."""
    out: List[str] = []
    for dt in walk(exp):
        if len(out) == limit:
            return out, True
        out.append(exp.format(dt))
    return out, False
