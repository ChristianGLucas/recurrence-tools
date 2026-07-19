from gen.messages_pb2 import ExpandRequest, OccurrenceList
from gen.axiom_context import AxiomContext

from nodes._recur import RecurError, build, effective_limit, take


def expand(ax: AxiomContext, input: ExpandRequest) -> OccurrenceList:
    """Expand a recurrence into its first occurrences, earliest first.

    Returns up to `limit` occurrences (default 100, maximum 10000) in the same
    RFC 5545 form as the anchoring dtstart. `truncated` is true when the
    recurrence continues past the returned occurrences, so an unbounded rule
    such as "FREQ=DAILY" yields a bounded page rather than running forever.
    """
    try:
        limit = effective_limit(input.limit)
        occurrences, truncated = take(build(input.recurrence), limit)
    except RecurError as exc:
        ax.log.info("expand rejected input", code=exc.code)
        return OccurrenceList(error={"code": exc.code, "message": exc.message})
    return OccurrenceList(
        occurrences=occurrences, count=len(occurrences), truncated=truncated
    )
