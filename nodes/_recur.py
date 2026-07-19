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
from datetime import datetime, timezone, tzinfo
from typing import Iterable, Iterator, List, Optional, Tuple

from dateutil.rrule import rrulestr, rruleset

# --- Bounds. Every one of these is enforced against the RAW input, before any
# --- parsing, allocation, or iteration that the value could make expensive.

MAX_RULE_LEN = 2048
MAX_DATE_LIST = 1000
MAX_LIMIT = 10000
DEFAULT_LIMIT = 100

# The iterator-step ceiling. Occurrences are produced lazily and in ascending
# order, so a query can be forced to step over an unbounded number of
# occurrences it will never return (a far-future window, a candidate that is
# never reached). Capping results alone would not bound that work.
MAX_STEPS = 200_000

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

INT_LIST_PARTS = (
    "BYSECOND", "BYMINUTE", "BYHOUR", "BYMONTHDAY", "BYYEARDAY",
    "BYWEEKNO", "BYMONTH", "BYSETPOS",
)

_BARE_RECUR = re.compile(r"^[A-Za-z0-9=;,+\-]+$")
_BYDAY = re.compile(r"^([+-]?\d{1,3})?(MO|TU|WE|TH|FR|SA|SU)$")
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

    for key, value in parts:
        _check_part(key, value)
    return parts


def _check_part(key: str, value: str) -> None:
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
            if not _BYDAY.match(item.upper()):
                raise _err("INVALID_RULE", f"invalid BYDAY entry '{item}'")
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
        return ",".join(item.upper() for item in value.split(","))
    if key in INT_LIST_PARTS:
        return ",".join(str(int(item)) for item in value.split(","))
    if key in ("INTERVAL", "COUNT"):
        return str(int(value))
    return value


# --------------------------------------------------------------------------
# Instants
# --------------------------------------------------------------------------

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


def coerce(value: str, field: str, zone: Optional[tzinfo]) -> datetime:
    """Parse an auxiliary instant into something comparable with the anchor.

    Mixing an aware anchor with a naive auxiliary value (or the reverse) raises
    TypeError deep inside dateutil's comparisons, so the mismatch is caught here
    and reported against the field that caused it.
    """
    dt, kind = parse_instant(value, field)
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
        return dt.strftime("%Y%m%d")
    if kind == KIND_UTC:
        aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Floating, or local to tzid: emit wall-clock time, which is what the
    # TZID form means. The zone travels separately, in Recurrence.tzid.
    return dt.strftime("%Y%m%dT%H%M%S")


# --------------------------------------------------------------------------
# Recurrence set
# --------------------------------------------------------------------------

class Expansion:
    """A parsed recurrence, ready to be walked under a step budget."""

    def __init__(self, rset: rruleset, kind: str, zone: Optional[tzinfo]):
        self.rset = rset
        self.kind = kind
        self.zone = zone

    def format(self, dt: datetime) -> str:
        return format_instant(dt, self.kind, self.zone)

    def instant(self, value: str, field: str) -> datetime:
        return coerce(value, field, self.zone)


def build(recurrence) -> Expansion:
    """Turn a Recurrence message into a bounded, validated Expansion."""
    if recurrence is None:
        raise _err("INVALID_ARGUMENT", "recurrence is required")

    check_rule(recurrence.rrule)

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
        rule = rrulestr(recurrence.rrule, dtstart=dtstart, cache=False)
    except RecurError:
        raise
    except Exception as exc:
        raise _err("INVALID_RULE", f"rrule could not be parsed: {exc}")

    rset = rruleset(cache=False)
    rset.rrule(rule)
    for value in recurrence.rdate:
        rset.rdate(coerce(value, "rdate", zone))
    for value in recurrence.exdate:
        rset.exdate(coerce(value, "exdate", zone))
    return Expansion(rset, kind, zone)


def walk(exp: Expansion, budget: int = MAX_STEPS) -> Iterator[datetime]:
    """Yield occurrences in ascending order under a hard step budget.

    The budget counts occurrences *visited*, including ones a caller discards,
    which is what makes a far-future window or an unreachable candidate
    terminate instead of running away.
    """
    steps = 0
    for dt in exp.rset:
        steps += 1
        if steps > budget:
            raise _err(
                "LIMIT_EXCEEDED",
                f"the recurrence produced more than {budget} occurrences before "
                "satisfying this request; narrow the rule, window, or limit",
            )
        yield dt


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
