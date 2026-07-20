from gen.messages_pb2 import RuleOutput, RuleParts
from gen.axiom_context import AxiomContext

from nodes._recur import (
    INT_LIST_PARTS,
    MAX_RULE_LEN,
    RecurError,
    canonical_rule,
    check_rule,
    probe_rule,
)


def _dedupe(value: str) -> str:
    """Drop repeated entries from a comma list, preserving first-seen order.

    Purely textual: no parsing, so malformed entries stay intact for check_rule
    to reject with a message naming the actual problem.
    """
    if "," not in value:
        return value
    seen, kept = set(), []
    for item in value.split(","):
        marker = item.upper()
        if marker not in seen:
            seen.add(marker)
            kept.append(item)
    return ",".join(kept)


def build(ax: AxiomContext, input: RuleParts) -> RuleOutput:
    """Assemble an RFC 5545 recurrence rule from its individual parts.

    Emits the rule in canonical part order and validates it before returning,
    so this node never produces a rule that Validate would reject. Zero-valued
    scalars and empty lists are treated as "part omitted". Feeding Parse's
    output straight back in reproduces the canonical rule unchanged.
    """
    try:
        # Parse's output pipes straight into this node, so an upstream failure
        # arrives here as a populated `error` and no parts. Re-deriving a
        # diagnosis from the empty parts would replace Parse's precise reason
        # ("INTERVAL must be 1 or greater") with a false one ("no rule parts
        # were supplied"), which is exactly the case a caller most needs the
        # truth. Propagate it verbatim instead.
        if input.error.code:
            return RuleOutput(
                error={"code": input.error.code, "message": input.error.message}
            )

        # Each entry costs at least one character plus a separator, so a field
        # with more entries than the rule length allows can be refused before
        # anything is built. Joining first would materialize a 200KB string only
        # to reject it for exceeding 2048.
        for name in ("byday",) + tuple(p.lower() for p in INT_LIST_PARTS):
            # Count DISTINCT values: a repeated entry collapses in the canonical
            # form, so measuring the raw list refused input whose rule is short.
            # String-keyed, so it can only ever over-count relative to the
            # canonical form ('1MO' and '01MO' count as two) -- never under, so
            # no oversized distinct list slips through.
            values = list(dict.fromkeys(str(v).upper() for v in getattr(input, name)))
            if len(values) * 2 > MAX_RULE_LEN:
                raise RecurError(
                    "LIMIT_EXCEEDED",
                    f"{name.upper()} has {len(values)} entries, which cannot fit "
                    f"in a rule of at most {MAX_RULE_LEN} characters",
                )

        parts = []
        if input.freq:
            parts.append(("FREQ", input.freq))
        if input.interval:
            parts.append(("INTERVAL", str(input.interval)))
        if input.count:
            parts.append(("COUNT", str(input.count)))
        if input.until:
            parts.append(("UNTIL", input.until))
        if input.byday:
            parts.append(("BYDAY", ",".join(input.byday)))
        for key in INT_LIST_PARTS:
            values = getattr(input, key.lower())
            if values:
                parts.append((key, ",".join(str(v) for v in values)))
        if input.wkst:
            parts.append(("WKST", input.wkst))

        if not parts:
            # Same code as every other node gives for an empty request; an
            # identical caller mistake must not get a different classification
            # depending on which node received it.
            raise RecurError("INVALID_RULE", "no rule parts were supplied")

        # Validate BEFORE canonicalizing. Canonicalizing first meant a
        # malformed BYDAY entry reached the sorter, raised, and was caught by
        # the catch-all below as INTERNAL -- reporting a plain caller mistake as
        # a package fault, the exact inverse of what INTERNAL means.
        # De-duplicate list values before the length check. Measuring the raw
        # join meant byday=['MO']*700 was refused for exceeding 2048 characters
        # even though its canonical form is 'BYDAY=MO' -- input that worked
        # before and is explicitly allowed through by the per-field guard above.
        raw = ";".join(f"{key}={_dedupe(value)}" for key, value in parts)
        validated = check_rule(raw)
        probe_rule(raw)
        rule = canonical_rule(validated)
    except RecurError as exc:
        return RuleOutput(error={"code": exc.code, "message": exc.message})
    except Exception:
        # This node runs in the parent process with no isolation, so nothing
        # else would stop an internal fault reaching the caller as a raw
        # traceback carrying host paths. Reported as ours, not as their rule's.
        return RuleOutput(
            error={"code": "INTERNAL", "message": "the rule could not be assembled"}
        )
    return RuleOutput(rrule=rule)
