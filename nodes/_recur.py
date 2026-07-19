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

# The iterator-step ceiling, counted over occurrences the expander YIELDS.
MAX_STEPS = 200_000

# Bounding COST is the isolated worker's job (see `isolate`). Earlier versions
# tried to bound it by rewriting the caller's rule with a synthesized UNTIL,
# which silently changed the answer for sparse rules; a bound that returns the
# wrong answer is worse than no bound. The rule is now expanded as written.

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

class Expansion:
    """A parsed recurrence, ready to be walked."""

    def __init__(self, rset: rruleset, kind: str, zone: Optional[tzinfo]):
        self.rset = rset
        self.kind = kind
        self.zone = zone

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
    return Expansion(rset, kind, zone)


def walk(exp: Expansion, budget: int = MAX_STEPS) -> Iterator[datetime]:
    """Yield occurrences in ascending order under a hard step budget.

    The budget counts occurrences *visited*, including ones a caller discards,
    which is what makes a far-future window or an unreachable candidate
    terminate instead of running away.
    """
    steps = 0
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
        steps += 1
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
        import importlib

        return importlib.import_module(module_name).compute(
            input_msg.SerializeToString()
        ), None

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
        proc.terminate()
        proc.join(1.0)
        if proc.is_alive():  # pragma: no cover - terminate is reliable here
            proc.kill()
    proc.join(1.0)
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
    """Collect up to `limit` formatted occurrences; report whether more remain."""
    out: List[str] = []
    for dt in walk(exp):
        if len(out) == limit:
            return out, True
        out.append(exp.format(dt))
    return out, False
