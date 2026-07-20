"""Shared test helpers: a fake AxiomContext and Recurrence builders."""


class FakeContext:
    """Minimal AxiomContext stand-in.

    The logger mirrors the real AxiomLogger protocol exactly -- msg positional,
    attributes as keywords -- and records calls, so a node that logs with the
    wrong shape fails here instead of at runtime on an error path.
    """

    class _Logger:
        def __init__(self):
            self.records = []

        def debug(self, msg, **attrs): self.records.append(("debug", msg, attrs))
        def info(self, msg, **attrs): self.records.append(("info", msg, attrs))
        def warn(self, msg, **attrs): self.records.append(("warn", msg, attrs))
        def error(self, msg, **attrs): self.records.append(("error", msg, attrs))

    class _Secrets:
        def get(self, name):
            return ("", False)

    def __init__(self):
        self.log = self._Logger()
        self.secrets = self._Secrets()
        self.execution_id = "test-execution-id"
        self.flow_id = "test-flow-id"
        self.tenant_id = "test-tenant-id"


def recurrence(rrule, dtstart, tzid="", rdate=(), exdate=()):
    """Build a Recurrence dict for a request message."""
    return {
        "rrule": rrule,
        "dtstart": dtstart,
        "tzid": tzid,
        "rdate": list(rdate),
        "exdate": list(exdate),
    }


# The anchor used by most RFC 5545 examples in section 3.8.5.3.
NY = "America/New_York"


def recurrence_message(rrule, dtstart, tzid="", rdate=(), exdate=()):
    """A real Recurrence protobuf, for tests that call into _recur directly."""
    from gen.messages_pb2 import Recurrence

    return Recurrence(
        rrule=rrule, dtstart=dtstart, tzid=tzid, rdate=list(rdate), exdate=list(exdate)
    )
