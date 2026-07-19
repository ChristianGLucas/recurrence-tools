from gen.messages_pb2 import RuleInput, ValidationResult
from gen.axiom_context import AxiomContext

from nodes._recur import RecurError, canonical_rule, check_rule, probe_rule


def validate(ax: AxiomContext, input: RuleInput) -> ValidationResult:
    """Check whether a string is a valid RFC 5545 recurrence rule.

    Applies RFC 5545 rules that the underlying expander accepts silently: a
    lone BYSETPOS, COUNT together with UNTIL, INTERVAL=0, and out-of-range BY*
    values are all reported as invalid rather than quietly changing what the
    rule means. On success `normalized` is the same rule in canonical part
    order; on failure `error` names the part that was rejected.
    """
    try:
        parts = check_rule(input.rrule)
        probe_rule(input.rrule)
    except RecurError as exc:
        return ValidationResult(
            valid=False, error={"code": exc.code, "message": exc.message}
        )
    return ValidationResult(valid=True, normalized=canonical_rule(parts))
