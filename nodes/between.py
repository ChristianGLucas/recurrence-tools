from gen.messages_pb2 import BetweenRequest, OccurrenceList
from gen.axiom_context import AxiomContext

from nodes import _recur
from nodes._recur import REORDER_MARGIN, RecurError, build, cmp_key, effective_limit, walk


def _compute(ax: AxiomContext, input: BetweenRequest) -> OccurrenceList:
    try:
        limit = effective_limit(input.limit)
        exp = build(input.recurrence)
        start = exp.instant(input.start, "start")
        end = exp.instant(input.end, "end")
        start_key, end_key = cmp_key(start), cmp_key(end)
        if end_key <= start_key:
            raise RecurError(
                "INVALID_ARGUMENT",
                f"end '{input.end}' must be strictly after start '{input.start}'",
            )

        occurrences = []
        truncated = False
        for dt in walk(exp):
            if cmp_key(dt) < start_key:
                continue
            if cmp_key(dt) >= end_key + REORDER_MARGIN:
                break
            if cmp_key(dt) >= end_key:
                continue
            if len(occurrences) == limit:
                truncated = True
                break
            occurrences.append(exp.format(dt))
        truncated = truncated or exp.budget_exhausted
    except RecurError as exc:
        ax.log.info("between rejected input", code=exc.code)
        return OccurrenceList(error={"code": exc.code, "message": exc.message})
    return OccurrenceList(
        occurrences=occurrences, count=len(occurrences), truncated=truncated
    )


def compute(data: bytes) -> bytes:
    """Entry point for the isolated worker process (see _recur.isolate)."""
    request = BetweenRequest()
    request.ParseFromString(data)
    return _compute(_SilentContext(), request).SerializeToString()


class _SilentContext:
    """The child process has no sidecar; logging is the parent's job."""

    class _Log:
        def debug(self, msg, **attrs): pass
        def info(self, msg, **attrs): pass
        def warn(self, msg, **attrs): pass
        def error(self, msg, **attrs): pass

    log = _Log()


def between(ax: AxiomContext, input: BetweenRequest) -> OccurrenceList:
    """List the occurrences of a recurrence inside a half-open time window.

    The window is [start, end): an occurrence exactly at `start` is included and
    one exactly at `end` is not. Occurrences are returned earliest first, capped
    at `limit` (default 100, maximum 10000), in the same RFC 5545 form as the
    anchoring dtstart. `truncated` is true when the window holds more
    occurrences than were returned.
    """
    data, failure = _recur.isolate("nodes.between", input)
    output = OccurrenceList()
    if failure is not None:
        ax.log.info("between rejected input", code=failure["code"])
        output.error.code = failure["code"]
        output.error.message = failure["message"]
        return output
    output.ParseFromString(data)
    if output.error.code:
        ax.log.info("between rejected input", code=output.error.code)
    return output
