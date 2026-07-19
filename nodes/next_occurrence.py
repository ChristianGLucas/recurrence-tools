from gen.messages_pb2 import NextRequest, Occurrence
from gen.axiom_context import AxiomContext

from nodes._recur import RecurError, build, walk


def next_occurrence(ax: AxiomContext, input: NextRequest) -> Occurrence:
    """Find the first occurrence of a recurrence strictly after a given instant.

    With `after` empty, returns the recurrence's own first occurrence. When the
    recurrence is exhausted before any occurrence exceeds `after`, `found` is
    false and `occurrence` is empty — that is a normal answer, not an error.
    """
    try:
        exp = build(input.recurrence)
        after = exp.instant(input.after, "after") if input.after else None
        for dt in walk(exp):
            if after is None or dt > after:
                return Occurrence(occurrence=exp.format(dt), found=True)
    except RecurError as exc:
        ax.log.info("next rejected input", code=exc.code)
        return Occurrence(error={"code": exc.code, "message": exc.message})
    return Occurrence(found=False)
