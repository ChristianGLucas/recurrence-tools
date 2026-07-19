from gen.messages_pb2 import CountRequest, OccurrenceCount
from gen.axiom_context import AxiomContext

from nodes._recur import MAX_LIMIT, RecurError, build, effective_limit, walk


def count(ax: AxiomContext, input: CountRequest) -> OccurrenceCount:
    """Count the occurrences a recurrence produces, up to a limit.

    Counting stops at `limit` (default and maximum 10000). `truncated` is true
    when the recurrence produces more than that, in which case `count` is a
    floor rather than the exact total — an unbounded rule can have no total.
    """
    try:
        limit = effective_limit(input.limit, default=MAX_LIMIT)
        exp = build(input.recurrence)
        total = 0
        truncated = False
        for _ in walk(exp):
            if total == limit:
                truncated = True
                break
            total += 1
    except RecurError as exc:
        ax.log.info("count rejected input", code=exc.code)
        return OccurrenceCount(error={"code": exc.code, "message": exc.message})
    return OccurrenceCount(count=total, truncated=truncated)
