from gen.messages_pb2 import ExpandRequest, OccurrenceList
from gen.axiom_context import AxiomContext

from nodes import _recur
from nodes._recur import RecurError, build, effective_limit, take


def _compute(ax: AxiomContext, input: ExpandRequest) -> OccurrenceList:
    try:
        limit = effective_limit(input.limit)
        exp = build(input.recurrence)
        occurrences, truncated = take(exp, limit)
        if not occurrences and exp.budget_exhausted:
            # Nothing was found AND the search stopped early, so "no
            # occurrences" was never established. An empty success reads as a
            # finding; the sibling nodes already report this case rather than
            # implying one.
            raise RecurError(
                "LIMIT_EXCEEDED",
                "the scan budget ran out before finding any occurrence; "
                "narrow the rule or move dtstart closer to the occurrences",
            )
    except RecurError as exc:
        ax.log.info("expand rejected input", code=exc.code)
        return OccurrenceList(error={"code": exc.code, "message": exc.message})
    return OccurrenceList(
        occurrences=occurrences, count=len(occurrences), truncated=truncated
    )


def compute(data: bytes) -> bytes:
    """Entry point for the isolated worker process (see _recur.isolate)."""
    request = ExpandRequest()
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


def expand(ax: AxiomContext, input: ExpandRequest) -> OccurrenceList:
    """Expand a recurrence into its first occurrences, earliest first.

    Returns up to `limit` occurrences (default 100, maximum 10000) in the same
    RFC 5545 form as the anchoring dtstart. `truncated` is true when the
    recurrence continues past the returned occurrences, so an unbounded rule
    such as "FREQ=DAILY" yields a bounded page rather than running forever.
    """
    data, failure = _recur.isolate("nodes.expand", input)
    output = OccurrenceList()
    if failure is not None:
        ax.log.info("expand rejected input", code=failure["code"])
        output.error.code = failure["code"]
        output.error.message = failure["message"]
        return output
    output.ParseFromString(data)
    if output.error.code:
        ax.log.info("expand rejected input", code=output.error.code)
    return output
