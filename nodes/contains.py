from gen.messages_pb2 import ContainsRequest, Membership
from gen.axiom_context import AxiomContext

from nodes import _recur
from nodes._recur import RecurError, build, cmp_key, walk


def _compute(ax: AxiomContext, input: ContainsRequest) -> Membership:
    try:
        exp = build(input.recurrence)
        candidate = exp.instant(input.candidate, "candidate")
        key = cmp_key(candidate)
        exhausted = False
        for dt in walk(exp):
            if cmp_key(dt) == key:
                return Membership(contains=True)
            if cmp_key(dt) > key:
                break
        else:
            # Reaching the end without passing the candidate means either the
            # recurrence really ended, or the scan stopped early -- and only the
            # first of those makes "not a member" a true answer.
            exhausted = exp.budget_exhausted
        if exhausted:
            raise RecurError(
                "LIMIT_EXCEEDED",
                "the search passed its scan budget before reaching the "
                "candidate instant; narrow the rule or move the candidate "
                "closer to the recurrence",
            )
    except RecurError as exc:
        ax.log.info("contains rejected input", code=exc.code)
        return Membership(error={"code": exc.code, "message": exc.message})
    return Membership(contains=False)


def compute(data: bytes) -> bytes:
    """Entry point for the isolated worker process (see _recur.isolate)."""
    request = ContainsRequest()
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


def contains(ax: AxiomContext, input: ContainsRequest) -> Membership:
    """Test whether an instant is an occurrence of a recurrence.

    Occurrences ascend, so the search stops as soon as the recurrence passes the
    candidate. An instant excluded by EXDATE is not a member even when the rule
    alone would have produced it.
    """
    data, failure = _recur.isolate("nodes.contains", input)
    output = Membership()
    if failure is not None:
        ax.log.info("contains rejected input", code=failure["code"])
        output.error.code = failure["code"]
        output.error.message = failure["message"]
        return output
    output.ParseFromString(data)
    if output.error.code:
        ax.log.info("contains rejected input", code=output.error.code)
    return output
