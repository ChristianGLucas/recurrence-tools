from gen.messages_pb2 import RuleInput, RuleParts
from gen.axiom_context import AxiomContext

from nodes._recur import INT_LIST_PARTS, RecurError, check_rule, probe_rule


def parse(ax: AxiomContext, input: RuleInput) -> RuleParts:
    """Decompose an RFC 5545 recurrence rule into its individual parts.

    Absent parts come back empty or zero rather than filled with RFC defaults,
    so a caller can tell "INTERVAL was omitted" from "INTERVAL=1 was stated".
    The rule is validated first, with exactly the checks Validate applies, so a
    rule this node parses is one Validate also accepts. Whether expanding it
    completes within the expansion budget depends on the anchor and the request,
    which a rule alone does not determine.
    """
    try:
        if input.error.code:
            raise RecurError(input.error.code, input.error.message)
        parts = check_rule(input.rrule)
        probe_rule(input.rrule)
    except RecurError as exc:
        return RuleParts(error={"code": exc.code, "message": exc.message})
    except Exception:
        # This node runs in the parent process with no isolation, so nothing
        # else would stop an internal fault reaching the caller as a raw
        # traceback carrying host paths. Reported as ours, not as their rule's.
        return RuleParts(
            error={"code": "INTERNAL", "message": "the rule could not be processed"}
        )

    out = RuleParts()
    for key, value in parts:
        if key == "FREQ":
            out.freq = value.upper()
        elif key == "INTERVAL":
            out.interval = int(value)
        elif key == "COUNT":
            out.count = int(value)
        elif key == "UNTIL":
            out.until = value
        elif key == "WKST":
            out.wkst = value.upper()
        elif key == "BYDAY":
            out.byday.extend(item.upper() for item in value.split(","))
        elif key in INT_LIST_PARTS:
            getattr(out, key.lower()).extend(int(item) for item in value.split(","))
    return out
