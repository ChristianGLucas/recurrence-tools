from gen.messages_pb2 import BetweenRequest, OccurrenceList
from gen.axiom_context import AxiomContext

from nodes._recur import RecurError, build, effective_limit, walk


def between(ax: AxiomContext, input: BetweenRequest) -> OccurrenceList:
    """List the occurrences of a recurrence inside a half-open time window.

    The window is [start, end): an occurrence exactly at `start` is included and
    one exactly at `end` is not. Occurrences are returned earliest first, capped
    at `limit` (default 100, maximum 10000), in the same RFC 5545 form as the
    anchoring dtstart. `truncated` is true when the window holds more
    occurrences than were returned.
    """
    try:
        limit = effective_limit(input.limit)
        exp = build(input.recurrence)
        start = exp.instant(input.start, "start")
        end = exp.instant(input.end, "end")
        if end <= start:
            raise RecurError(
                "INVALID_ARGUMENT",
                f"end '{input.end}' must be strictly after start '{input.start}'",
            )

        occurrences = []
        truncated = False
        for dt in walk(exp):
            if dt < start:
                continue
            if dt >= end:
                break
            if len(occurrences) == limit:
                truncated = True
                break
            occurrences.append(exp.format(dt))
    except RecurError as exc:
        ax.log.info("between rejected input", code=exc.code)
        return OccurrenceList(error={"code": exc.code, "message": exc.message})
    return OccurrenceList(
        occurrences=occurrences, count=len(occurrences), truncated=truncated
    )
