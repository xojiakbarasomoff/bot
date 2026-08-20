import logging

from app.core.logging import ExtraFieldFormatter, configure_logging


def _record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_extra_fields_are_rendered() -> None:
    """The whole point: this codebase logs its detail through extra=, and
    the stdlib formatter drops all of it.
    """
    formatter = ExtraFieldFormatter("%(levelname)s %(name)s %(message)s")
    output = formatter.format(_record("webhook_unknown_ig_account", ig_account_id="1784143"))
    assert output == "INFO app.test webhook_unknown_ig_account ig_account_id=1784143"


def test_message_without_extras_is_unchanged() -> None:
    formatter = ExtraFieldFormatter("%(levelname)s %(name)s %(message)s")
    assert formatter.format(_record("startup_complete")) == "INFO app.test startup_complete"


def test_fields_are_ordered_so_lines_are_comparable() -> None:
    # Sorted, not insertion-ordered: two lines for the same event should
    # diff against each other cleanly.
    formatter = ExtraFieldFormatter("%(message)s")
    output = formatter.format(_record("event", zebra=1, alpha=2))
    assert output == "event alpha=2 zebra=1"


def test_configure_logging_is_idempotent() -> None:
    """Called from both app.main and app.workers.tasks, and a reimport must
    not start doubling every line.
    """
    configure_logging()
    configure_logging()
    assert len(logging.getLogger().handlers) == 1
