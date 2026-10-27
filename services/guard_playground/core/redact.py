"""Secret redaction for errors, Run results and logs (design.md "core/redact.py"; Req 8.3, 8.4).

Secret values are read from the environment at call time, so a key set or rotated after the
app started is still redacted and nothing caches the values.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel

SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
MIN_SECRET_LENGTH = 8
REDACTED = "[redacted]"


def secrets() -> list[str]:
    """Values of env vars named ``*_KEY``/``*_TOKEN``/``*_SECRET``/``*_PASSWORD``, len >= 8.

    Sorted longest first so a secret that contains another one is replaced as a whole.
    """
    values = {
        value
        for name, value in os.environ.items()
        if name.upper().endswith(SECRET_SUFFIXES) and len(value) >= MIN_SECRET_LENGTH
    }
    return sorted(values, key=len, reverse=True)


def redact(s: str) -> str:
    """Replace every secret value in ``s`` with ``"[redacted]"``."""
    for value in secrets():
        if value in s:
            s = s.replace(value, REDACTED)
    return s


def redact_data(data: Any) -> Any:
    """Redact strings in dicts, lists, tuples and pydantic models; keeps the input's type."""
    if isinstance(data, str):
        return redact(data)
    if isinstance(data, BaseModel):
        return type(data).model_validate(redact_data(data.model_dump()))
    if isinstance(data, dict):
        return {redact_data(key): redact_data(value) for key, value in data.items()}
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_data(item) for item in data)
    return data


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the formatted message and any exception text of a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed %-args: keep the raw template, still redacted
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Attach one ``RedactingFilter`` to each handler of ``logger`` (root by default).

    Idempotent: a handler that already has a ``RedactingFilter`` is left unchanged.
    """
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
