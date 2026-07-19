from gen.messages_pb2 import ContainsRequest, Membership
from gen.axiom_context import AxiomContext

from nodes._recur import RecurError, build, walk


def contains(ax: AxiomContext, input: ContainsRequest) -> Membership:
    """Test whether an instant is an occurrence of a recurrence.

    Occurrences ascend, so the search stops as soon as the recurrence passes the
    candidate. An instant excluded by EXDATE is not a member even when the rule
    alone would have produced it.
    """
    try:
        exp = build(input.recurrence)
        candidate = exp.instant(input.candidate, "candidate")
        for dt in walk(exp):
            if dt == candidate:
                return Membership(contains=True)
            if dt > candidate:
                break
    except RecurError as exc:
        ax.log.info("contains rejected input", code=exc.code)
        return Membership(error={"code": exc.code, "message": exc.message})
    return Membership(contains=False)
