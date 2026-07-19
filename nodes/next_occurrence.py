from gen.messages_pb2 import NextRequest, Occurrence
from gen.axiom_context import AxiomContext

from nodes import _recur
from nodes._recur import RecurError, build, walk


def _compute(ax: AxiomContext, input: NextRequest) -> Occurrence:
    try:
        exp = build(input.recurrence)
        after = exp.instant(input.after, "after") if input.after else None
        for dt in walk(exp):
            if after is None or dt > after:
                return Occurrence(occurrence=exp.format(dt), found=True)
        if exp.budget_exhausted:
            # Unlike a list, a single "next" cannot be returned partially: the
            # search stopped early, so "none remains" would be a wrong answer.
            raise RecurError(
                "LIMIT_EXCEEDED",
                "the search passed its scan budget before reaching an "
                "occurrence after this instant; narrow the rule or move `after` "
                "closer to the recurrence",
            )
    except RecurError as exc:
        ax.log.info("next rejected input", code=exc.code)
        return Occurrence(error={"code": exc.code, "message": exc.message})
    return Occurrence(found=False)


def compute(data: bytes) -> bytes:
    """Entry point for the isolated worker process (see _recur.isolate)."""
    request = NextRequest()
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


def next_occurrence(ax: AxiomContext, input: NextRequest) -> Occurrence:
    """Find the first occurrence of a recurrence strictly after a given instant.

    With `after` empty, returns the recurrence's own first occurrence. When the
    recurrence is exhausted before any occurrence exceeds `after`, `found` is
    false and `occurrence` is empty — that is a normal answer, not an error.
    """
    data, failure = _recur.isolate("nodes.next_occurrence", input)
    output = Occurrence()
    if failure is not None:
        ax.log.info("next_occurrence rejected input", code=failure["code"])
        output.error.code = failure["code"]
        output.error.message = failure["message"]
        return output
    output.ParseFromString(data)
    if output.error.code:
        ax.log.info("next_occurrence rejected input", code=output.error.code)
    return output
