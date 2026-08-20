"""Logging setup that actually emits what this codebase logs.

Nearly every log call here passes its detail through `extra=` -- the tenant
id on a webhook, the account id on an unresolved one, the length and preview
of a message. The stdlib's default formatter renders only the message text,
so all of that was being discarded silently: the calls looked like structured
logging while the deployment's log stream showed bare event names, which is
worse than either choice made deliberately, because the missing half is
invisible until someone needs it.

`configure_logging` renders those fields as `key=value` after the message,
and installs a handler at all -- without one, Python's fallback drops
everything below WARNING, so no INFO in this codebase ever reached a log
stream.
"""

import logging

# Attributes the stdlib puts on every LogRecord. Anything outside this set
# arrived via `extra=` and is what the call site actually wanted to say.
_STANDARD_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class ExtraFieldFormatter(logging.Formatter):
    """Appends a record's `extra=` fields to the formatted message."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS
        }
        if not extras:
            return formatted
        rendered = " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
        return f"{formatted} {rendered}"


def configure_logging(level: int = logging.INFO) -> None:
    """Install a single stream handler using ExtraFieldFormatter.

    Replaces any handlers already on the root logger rather than adding to
    them, so a second call (a test, a reimport) cannot double every line.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(ExtraFieldFormatter("%(levelname)s %(name)s %(message)s"))

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
