"""Logging that cannot print the API key or the tool secret."""

from __future__ import annotations

import logging

from moviexfone.sanitize import redact


class QueryRedactFilter(logging.Filter):
    """Strip token and ticket query values before any handler, including uvicorn's."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = secrets or []

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg), self._secrets)
        if record.args:
            record.args = tuple(
                redact(str(arg), self._secrets) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


class RedactFormatter(logging.Formatter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self._secrets = secrets or []

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


def configure_logging(secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactFormatter(secrets))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # httpx logs full request URLs at INFO. Keep it quieter.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    redactor = QueryRedactFilter(secrets)
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addFilter(redactor)
