from gen.messages_pb2 import RuleOutput, RuleParts
from gen.axiom_context import AxiomContext

from nodes._recur import (
    INT_LIST_PARTS,
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
        #
        # NOTE for future refactors: this ordering is now defence in depth
        # rather than a gate. The sorter was also made non-raising, so reverting
        # this order alone does NOT break any test. Do not assume a test will
        # catch you if you swap these lines back.
        # De-duplicate list values so the assembled text matches what
        # canonical_rule would produce anyway (e.g. byday=['MO']*700 collapses
        # to 'BYDAY=MO') -- no repeated-entry noise reaches check_rule/probe_rule.
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
