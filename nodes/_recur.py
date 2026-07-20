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

# The DETERMINISTIC cost bound, counted in candidate instants the expander has
# to examine -- which is what the work actually is.
#
# A rule's cost is not its result size. "FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;
# BYSECOND=0" yields one occurrence a day, but the expander steps through every
# second in between: 86400 candidates per occurrence. Measured, that runs at a
# steady ~17.4 million candidates/second regardless of frequency, so the
# candidate count predicts the cost -- and unlike a clock, it is the same number
# on every machine and every run.
#
# It is computed from the occurrences themselves: the calendar distance between
# consecutive occurrences, divided by the frequency's step. No rule rewriting is
# involved, so this cannot change any answer -- it only decides when to stop.
MAX_SCAN_STEPS = 20_000_000

# Nominal seconds per step at each frequency, for converting a calendar span
# into a candidate count. Approximate for MONTHLY/YEARLY, which is fine: this
# sizes a budget, it does not decide any occurrence.
FREQ_STEP_SECONDS = {
    "SECONDLY": 1,
    "MINUTELY": 60,
    "HOURLY": 3600,
    "DAILY": 86_400,
    "WEEKLY": 604_800,
    "MONTHLY": 2_678_400,   # 31 days
    "YEARLY": 31_622_400,   # 366 days
}

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


# Caller strings are echoed into error messages so a mistake is easy to spot.
# They are truncated first: an error is a diagnostic, not a mirror, and echoing
# a 200KB value back would let a caller amplify a tiny request into a huge
# response for free.
MAX_ECHO = 80


def echo(value: str) -> str:
    """Render a caller-supplied value for an error message, bounded."""
    if len(value) <= MAX_ECHO:
        return value
    return f"{value[:MAX_ECHO]}... ({len(value)} characters)"


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

    # Only once every part has been range- and type-checked: this reasons over
    # the values, so running it first would hand it input nobody had validated.
    _check_month_day_feasible(parts)
    _check_yearday_month_feasible(parts)
    _check_setpos_feasible(parts, freq)
    return parts


# The longest each month can be. A BYMONTH/BYMONTHDAY pair outside this can
# never occur, and asking the expander to discover that costs seconds of
# scanning to the year ceiling -- so it is refused up front, deterministically.
DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
DAYS_IN_COMMON = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _check_month_day_feasible(parts: List[Tuple[str, str]]) -> None:
    by = {k: v for k, v in parts}
    if "BYMONTH" not in by or "BYMONTHDAY" not in by:
        return
    try:
        months = [int(v) for v in by["BYMONTH"].split(",")]
        days = [abs(int(v)) for v in by["BYMONTHDAY"].split(",")]
    except ValueError:  # pragma: no cover - _check_part rejects these first
        return
    in_range = [DAYS_IN_MONTH[m - 1] for m in months if 1 <= m <= 12]
    if not in_range or not days:
        return
    longest = max(in_range)
    if min(days) > longest:
        raise _err(
            "INVALID_RULE",
            f"BYMONTHDAY={by['BYMONTHDAY']} can never occur in "
            f"BYMONTH={by['BYMONTH']}; the longest of those months has "
            f"{longest} days",
        )


def _months_for_yearday(day: int) -> set:
    """Which months an ordinal day-of-year can fall in, over leap and common years."""
    months = set()
    for year_length, table in ((365, DAYS_IN_COMMON), (366, DAYS_IN_MONTH)):
        ordinal = day if day > 0 else year_length + 1 + day
        if not 1 <= ordinal <= year_length:
            continue
        running = 0
        for index, length in enumerate(table, start=1):
            running += length
            if ordinal <= running:
                months.add(index)
                break
    return months


def _check_yearday_month_feasible(parts: List[Tuple[str, str]]) -> None:
    """Refuse a BYYEARDAY/BYMONTH pair that no calendar can satisfy.

    Day 366 only ever lands in December, day 1 only in January. Without this the
    expander discovers it the slow way -- stepping second by second toward its
    year ceiling -- which is the cheapest denial-of-service the package had.
    """
    by = {k: v for k, v in parts}
    if "BYYEARDAY" not in by or "BYMONTH" not in by:
        return
    try:
        yeardays = [int(v) for v in by["BYYEARDAY"].split(",")]
        months = {int(v) for v in by["BYMONTH"].split(",")}
    except ValueError:  # pragma: no cover - _check_part rejects these first
        return
    reachable = set()
    for day in yeardays:
        reachable |= _months_for_yearday(day)
    if reachable and months and not (reachable & months):
        raise _err(
            "INVALID_RULE",
            f"BYYEARDAY={by['BYYEARDAY']} can never fall in "
            f"BYMONTH={by['BYMONTH']}; those days occur in "
            f"month(s) {','.join(str(m) for m in sorted(reachable))}",
        )


# The most instants any single interval can contain, per frequency. A ceiling,
# not the true set size -- BY* parts only ever narrow it -- so comparing against
# it can refuse an impossible rule but can never refuse a possible one.
MAX_SET_SIZE = {
    "SECONDLY": 1,
    "MINUTELY": 60,
    "HOURLY": 3600,
    "DAILY": 86_400,
    "WEEKLY": 7 * 86_400,
    "MONTHLY": 31 * 86_400,
    "YEARLY": 366 * 86_400,
}


def _check_setpos_feasible(parts: List[Tuple[str, str]], freq: str) -> None:
    """Refuse a BYSETPOS position no interval could ever contain.

    BYSETPOS selects the Nth instant within one interval. A SECONDLY interval
    holds exactly one instant, so BYSETPOS=300 selects a 300th that cannot
    exist -- and the expander discovers that only by scanning to its year
    ceiling, which is the last cheap way to buy the full time budget.
    """
    by = {k: v for k, v in parts}
    if "BYSETPOS" not in by:
        return
    ceiling = MAX_SET_SIZE.get(freq)
    if ceiling is None:
        return
    try:
        positions = [abs(int(v)) for v in by["BYSETPOS"].split(",")]
    except ValueError:  # pragma: no cover - _check_part rejects these first
        return
    if positions and min(positions) > ceiling:
        raise _err(
            "INVALID_RULE",
            f"BYSETPOS={by['BYSETPOS']} selects a position beyond what a "
            f"FREQ={freq} interval can contain (at most {ceiling})",
        )


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
        # Sorted like every other list part -- BY* order does not affect the
        # recurrence set, so two spellings of one rule must converge on one text.
        entries = {
            (item[1:] if item.startswith("+") else item).upper()
            for item in value.split(",")
        }
        return ",".join(
            sorted(entries, key=lambda e: (WEEKDAYS.index(e[-2:]), int(e[:-2] or 0)))
        )
    if key in INT_LIST_PARTS:
        # De-duplicated and ordered: two spellings of the same rule must produce
        # the same canonical text, or `normalized` cannot serve as a key.
        return ",".join(
            str(n) for n in sorted({int(item) for item in value.split(",")})
        )
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
        f"{field} '{echo(value)}' is not an RFC 5545 DATE ('19970902') or "
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
        # "localtime" and "Factory" resolve through host configuration rather
        # than to a fixed zone, so the same request would expand differently on
        # different machines. A canonical IANA id always names a region.
        if "/" not in tzid:
            raise _err(
                "INVALID_ARGUMENT",
                f"tzid '{echo(tzid)}' is not a canonical IANA time-zone id; "
                "use a region-qualified name such as 'America/New_York'",
            )
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(tzid)
        except Exception:
            raise _err(
                "INVALID_ARGUMENT", f"unknown IANA time-zone id '{echo(tzid)}'"
            )
    return timezone.utc if kind == KIND_UTC else None


# How far an occurrence can move relative to its neighbours when a zone shifts.
#
# Occurrences arrive in LOCAL order, but comparisons happen in UTC, and a
# spring-forward gap breaks the correspondence: in New York a rule firing at
# 02:00, 02:30, 03:00 and 03:30 local on a transition day maps to 07:00Z,
# 07:30Z, 07:00Z, 07:30Z -- the third instant goes BACKWARDS in UTC. Any search
# that stops the moment it passes its target would stop one occurrence too
# early and silently lose a real one.
#
# The displacement is bounded by the size of the shift (never more than a couple
# of hours anywhere in the tz database), so every early break carries this
# margin: keep looking a little past the target before concluding there is
# nothing more.
REORDER_MARGIN = timedelta(hours=3)


def cmp_key(dt: datetime) -> datetime:
    """A comparison key that survives PEP 495 fold semantics.

    On a fall-back day a local time occurs twice, and Python marks such an
    instant "fold-affected". Comparing one against an equal instant in another
    zone returns False for ==, > AND < simultaneously -- it is neither equal nor
    ordered. Comparing zone-local values directly therefore makes an occurrence
    vanish from an equality test while still passing a range test, which is how
    Contains and Between came to disagree about the same instant.

    Normalising to UTC first removes the ambiguity, because a UTC instant is
    never fold-affected.
    """
    return dt.astimezone(timezone.utc) if dt.tzinfo is not None else dt


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

class Expansion:
    """A parsed recurrence, ready to be walked."""

    def __init__(self, rset: rruleset, kind: str, zone: Optional[tzinfo],
                 anchor: Optional[datetime] = None, step_seconds: int = 86_400):
        self.rset = rset
        self.kind = kind
        self.zone = zone
        # Where the scan starts, and how much calendar one candidate covers --
        # together these turn occurrence gaps into a candidate count.
        self.anchor = anchor
        self.step_seconds = max(1, step_seconds)
        # Set by walk() when the scan budget stopped it. Distinguishes "no more
        # occurrences" from "stopped counting", which callers report differently.
        self.budget_exhausted = False

    def format(self, dt: datetime) -> str:
        return format_instant(dt, self.kind, self.zone)

    def instant(self, value: str, field: str) -> datetime:
        return coerce(value, field, self.zone, self.kind)


def build(recurrence) -> Expansion:
    """Turn a Recurrence message into a bounded, validated Expansion."""
    if recurrence.error.code:
        # An upstream node already diagnosed this precisely. Re-deriving from the
        # empty fields that came with it would replace the real reason with a
        # wrong one, exactly where a caller most needs the truth.
        raise _err(recurrence.error.code, recurrence.error.message)

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

    try:
        # The rule is expanded EXACTLY as the caller wrote it. An earlier version
        # rewrote it with a synthesized UNTIL to bound the scan; that silently
        # changed the answer for sparse rules -- "FREQ=SECONDLY;BYHOUR=9;
        # BYMINUTE=0;BYSECOND=0" is one occurrence per day, but sizing a horizon
        # from SECONDLY steps landed it before the rule's own first occurrence.
        # Bounding cost is the isolated worker's job (see `isolate`); it must not
        # be paid for with wrong answers.
        rule = rrulestr(recurrence.rrule, dtstart=dtstart, cache=False)
    except RecurError:
        raise
    except Exception as exc:
        raise _err("INVALID_RULE", f"rrule could not be parsed: {exc}")

    # COUNT stays inside the rule, so the expander applies it to the RRULE's own
    # occurrences before RDATEs are merged and EXDATEs removed, per RFC 5545.
    rset = rruleset(cache=False)
    rset.rrule(rule)
    for value in recurrence.rdate:
        rset.rdate(coerce(value, "rdate", zone, kind))
    for value in recurrence.exdate:
        rset.exdate(coerce(value, "exdate", zone, kind))
    by_part = {k: v for k, v in parts}
    freq = by_part.get("FREQ", "DAILY").upper()
    interval = max(1, int(by_part.get("INTERVAL", "1")))
    step = FREQ_STEP_SECONDS.get(freq, 86_400) * interval
    return Expansion(rset, kind, zone, anchor=dtstart, step_seconds=step)


def walk(exp: Expansion, budget: int = MAX_STEPS,
         scan_budget: int = MAX_SCAN_STEPS) -> Iterator[datetime]:
    """Yield occurrences in ascending order under two deterministic budgets.

    `budget` counts occurrences visited, including ones the caller discards --
    what stops a far-future window from running away. `scan_budget` counts the
    candidate instants the expander had to examine to produce them, which is
    the real cost of a sparse rule and is invisible in the result size.

    Both are counts, so the same request always stops at the same place on any
    machine. Exhausting either sets `budget_exhausted` and ends the walk rather
    than raising: a partial list of real occurrences is a better answer than an
    error, and each node decides how to report it.
    """
    steps = 0
    scanned = 0.0
    previous = exp.anchor
    exp.budget_exhausted = False
    iterator = iter(exp.rset)
    while True:
        # Failures inside the expander surface HERE, on iteration, not when the
        # rule was constructed -- so no amount of up-front rule checking can be
        # relied on to have caught them. Anything that escapes becomes a
        # structured error, because the alternative is a raw traceback (and its
        # internal paths) reaching the caller. The node contract is that bad
        # input is reported, never raised.
        try:
            dt = next(iterator)
        except StopIteration:
            return
        except RecurError:
            raise
        except Exception as exc:
            raise _err(
                "INVALID_RULE",
                f"the expander failed while producing occurrences: "
                f"{type(exc).__name__}: {exc}",
            )
        # Charge the caller for the candidates the expander had to step over to
        # reach this occurrence, not merely for the occurrence itself.
        if previous is not None:
            gap = (dt - previous).total_seconds()
            if gap > 0:
                scanned += gap / exp.step_seconds
        previous = dt
        if scanned > scan_budget:
            exp.budget_exhausted = True
            return

        steps += 1
        if steps > budget:
            exp.budget_exhausted = True
            return
        yield dt


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------

# The wall-clock backstop, enforced in a separate process.
#
# This is deliberately NOT the primary bound. A clock is not deterministic: when
# it decides requests, identical input returns different answers under different
# load, which is precisely what "deterministic" must not mean. MAX_SCAN_STEPS
# above bounds every rule that yields anything, and does so identically on every
# run.
#
# What a count cannot bound is a rule that yields NOTHING -- no occurrences means
# no gaps to charge for -- while the expander scans toward its year ceiling
# inside a single library call, where no deadline could be checked. That case,
# and only that case, is what this kills. It is set well above the slowest
# request the scan budget permits (measured at ~1.2s) so it cannot fire on a
# request that is making progress.
SCAN_TIMEOUT_SECONDS = 3.0

# How long reaping a killed worker may add on top. Kept small deliberately: the
# caller's worst case is SCAN_TIMEOUT_SECONDS + this, and that total is the
# number worth documenting, not the internal deadline alone.
REAP_TIMEOUT_SECONDS = 0.5


def _isolated_entry(module_name: str, data: bytes, queue) -> None:
    import importlib

    try:
        queue.put(("ok", importlib.import_module(module_name).compute(data)))
    except RecurError as exc:
        queue.put(("err", exc.code, exc.message))
    except Exception as exc:  # pragma: no cover - defence in depth
        # INTERNAL, not INVALID_RULE: the caller's rule may be perfectly fine,
        # and telling them otherwise sends them to debug the wrong thing.
        queue.put(("err", "INTERNAL", f"{type(exc).__name__}: {exc}"))


def isolate(module_name: str, input_msg):
    """Run a node's computation in a process that can be killed.

    A recurrence rule can be valid and match NOTHING, so it never yields and any
    budget counted over yielded results never advances -- while the expander
    scans on toward its year ceiling. That hang happens inside a single call
    into the library, where no deadline this code could check would ever be
    reached. Some failures can only be prevented, not caught, so the work runs
    in a child process with a hard wall-clock limit: on overrun the process is
    killed and the caller gets a structured LIMIT_EXCEEDED, with nothing left
    behind burning CPU.

    The result is drained from the queue BEFORE the child is joined. That order
    is load-bearing: a queue is backed by a pipe, and a payload larger than the
    pipe buffer (~64KiB) leaves the child blocked in its feeder thread with
    nothing reading the other end. Joining first would deadlock on exactly the
    large-but-cheap results a caller is most likely to ask for -- an expansion
    at the documented maximum limit -- and report it as "too costly to
    evaluate", which is a wrong answer wearing a bound's clothing.

    Returns (serialized_output, None) on success, or (None, error_dict). The
    caller constructs its own output message from that -- both because the
    error has to be attached to the node's own type, and because `axiom
    validate` requires a node body to visibly construct its declared output.
    """
    import multiprocessing
    import queue as queue_module

    try:
        ctx = multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - platform without fork
        # Running inline here would silently drop the wall-clock backstop, so a
        # rule that returns LIMIT_EXCEEDED elsewhere would hang the worker
        # instead. Refusing is the honest outcome: the guarantee is either
        # enforced or the request is declined, never quietly unenforced.
        return None, {
            "code": "LIMIT_EXCEEDED",
            "message": (
                "expansion requires an isolated worker, which this platform "
                "cannot provide, so the time bound cannot be enforced"
            ),
        }

    channel = ctx.Queue(1)
    proc = ctx.Process(
        target=_isolated_entry,
        args=(module_name, input_msg.SerializeToString(), channel),
        daemon=True,
    )
    proc.start()
    try:
        try:
            result = channel.get(timeout=SCAN_TIMEOUT_SECONDS)
        except queue_module.Empty:
            return None, {
                "code": "LIMIT_EXCEEDED",
                "message": (
                    f"the recurrence did not finish expanding within "
                    f"{SCAN_TIMEOUT_SECONDS:.0f}s; it is too costly to evaluate. "
                    "Narrow the rule, window, or limit"
                ),
            }
        if result[0] == "err":
            return None, {"code": result[1], "message": result[2]}
        return result[1], None
    finally:
        _reap(proc, channel)


def _reap(proc, channel) -> None:
    """Stop the child and release its handles.

    Every invocation forks, so file descriptors and process-table entries leak
    under sustained load unless both are released explicitly.
    """
    if proc.is_alive():
        # SIGKILL immediately rather than SIGTERM-then-wait. The child is a
        # daemon holding no resource that needs an orderly shutdown, and a
        # graceful path added seconds AFTER the deadline -- so the bound the
        # caller experienced was not the bound that was advertised.
        proc.kill()
    proc.join(REAP_TIMEOUT_SECONDS)
    try:
        channel.close()
        channel.join_thread()
    except Exception:  # pragma: no cover - already closed
        pass
    try:
        proc.close()
    except Exception:  # pragma: no cover - not yet reaped
        pass


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
    """Collect up to `limit` formatted occurrences; report whether more remain.

    `truncated` covers both reasons a list can be short: the caller's limit, and
    the scan budget. Both mean "there are more"; neither is an error, because
    the occurrences returned are real and correct either way.
    """
    out: List[str] = []
    for dt in walk(exp):
        out.append(exp.format(dt))
        if len(out) == limit:
            # Stop HERE rather than looping once more to discover whether another
            # occurrence exists. That extra pull is unbounded: for a rule whose
            # next gap is decades wide it can consume the entire budget and lose
            # the answer the caller actually asked for. `truncated` therefore
            # means "collection stopped early, more may exist", not "more exist".
            return out, True
    return out, exp.budget_exhausted
