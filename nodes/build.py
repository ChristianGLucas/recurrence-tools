from gen.messages_pb2 import RuleOutput, RuleParts
from gen.axiom_context import AxiomContext

from nodes._recur import (
    INT_LIST_PARTS,
    RecurError,
    canonical_rule,
    check_rule,
    probe_rule,
)


def build(ax: AxiomContext, input: RuleParts) -> RuleOutput:
    """Assemble an RFC 5545 recurrence rule from its individual parts.

    Emits the rule in canonical part order and validates it before returning,
    so this node never produces a rule that Validate would reject. Zero-valued
    scalars and empty lists are treated as "part omitted". Feeding Parse's
    output straight back in reproduces the canonical rule unchanged.
    """
    try:
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
            raise RecurError("INVALID_ARGUMENT", "no rule parts were supplied")

        rule = canonical_rule(parts)
        check_rule(rule)
        probe_rule(rule)
    except RecurError as exc:
        return RuleOutput(error={"code": exc.code, "message": exc.message})
    return RuleOutput(rrule=rule)
