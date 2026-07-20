from gen.messages_pb2 import CountRequest, OccurrenceCount
from gen.axiom_context import AxiomContext

from nodes import _recur
from nodes._recur import MAX_LIMIT, RecurError, build, effective_limit, walk


def _compute(ax: AxiomContext, input: CountRequest) -> OccurrenceCount:
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
        truncated = truncated or exp.budget_exhausted or exp.ceiling_reached
        if total == 0 and (exp.budget_exhausted or exp.ceiling_reached):
            raise RecurError(
                "LIMIT_EXCEEDED",
                "the scan budget ran out before finding any occurrence; "
                "narrow the rule or move dtstart closer to the occurrences",
            )
    except RecurError as exc:
        ax.log.info("count rejected input", code=exc.code)
        return OccurrenceCount(error={"code": exc.code, "message": exc.message})
    return OccurrenceCount(count=total, truncated=truncated)


def compute(data: bytes) -> bytes:
    """Entry point for the isolated worker process (see _recur.isolate)."""
    request = CountRequest()
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


def count(ax: AxiomContext, input: CountRequest) -> OccurrenceCount:
    """Count the occurrences a recurrence produces, up to a limit.

    Counting stops at `limit` (default and maximum 10000). `truncated` is true
    when the recurrence produces more than that, in which case `count` is a
    floor rather than the exact total — an unbounded rule can have no total.
    """
    data, failure = _recur.isolate("nodes.count", input)
    output = OccurrenceCount()
    if failure is not None:
        ax.log.info("count rejected input", code=failure["code"])
        output.error.code = failure["code"]
        output.error.message = failure["message"]
        return output
    output.ParseFromString(data)
    if output.error.code:
        ax.log.info("count rejected input", code=output.error.code)
    return output
