"""Structured JSON logging configuration and formatter (R21.3).

Emits single-line JSON log messages containing the 5 mandatory correlation fields:
`trace_id, message_id, thread_id, job_id, organization_id`.
"""

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from packages.observability.context import CORRELATION_KEYS, get_correlation_context


class StructuredJSONFormatter(logging.Formatter):
    """Formatter emitting log records as single-line JSON with correlation fields."""

    def __init__(self, service_name: str | None = None) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        """Format the specified record as a JSON string."""
        context = get_correlation_context()

        # Build base structured payload
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if self.service_name:
            payload["service"] = self.service_name

        # Mandatory correlation fields (R21.3)
        for key in CORRELATION_KEYS:
            # Prefer explicit attribute on record, fall back to async contextvar
            val = getattr(record, key, None) or context.get(key)
            payload[key] = str(val) if val is not None else None

        # Caller location
        payload["location"] = {
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Any extra keys bound in context that are not standard correlation keys
        extra_context = {k: v for k, v in context.items() if k not in CORRELATION_KEYS}
        if extra_context:
            payload["extra"] = extra_context

        # Exception information if present
        if record.exc_info:
            payload["exception"] = {
                "type": getattr(record.exc_info[0], "__name__", "Exception"),
                "message": str(record.exc_info[1]),
                "stacktrace": traceback.format_exception(*record.exc_info),
            }

        return json.dumps(payload, default=str)


class CorrelationFilter(logging.Filter):
    """Logging filter that injects correlation context attributes directly into LogRecords."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = get_correlation_context()
        for key in CORRELATION_KEYS:
            if not hasattr(record, key):
                setattr(record, key, context.get(key))
        return True


def setup_logging(
    level: str = "INFO",
    json_format: bool = True,
    service_name: str | None = None,
    loggers_to_quiet: list[str] | None = None,
) -> None:
    """Configure the root logger with structured JSON formatting and correlation filtering.

    Parameters
    ----------
    level : str
        Root logging level (DEBUG, INFO, WARNING, ERROR).
    json_format : bool
        If True, emit structured JSON. If False, emit readable text format.
    service_name : str | None
        Optional service identifier attached to all log records.
    loggers_to_quiet : list[str] | None
        Optional list of noisy third-party loggers to set to WARNING level.
    """
    root_logger = logging.getLogger()
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(numeric_level)

    # Remove existing handlers to avoid duplicates
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.addFilter(CorrelationFilter())

    if json_format:
        stream_handler.setFormatter(StructuredJSONFormatter(service_name=service_name))
    else:
        text_fmt = "%(asctime)s [%(levelname)s] %(name)s (trace_id=%(trace_id)s): %(message)s"
        stream_handler.setFormatter(logging.Formatter(text_fmt))

    root_logger.addHandler(stream_handler)

    # Silence noisy dependencies by default
    quiet_defaults = ["aiormq", "aio_pika", "urllib3", "asyncio", "httpcore", "httpx"]
    quiet_list = loggers_to_quiet if loggers_to_quiet is not None else quiet_defaults
    for logger_name in quiet_list:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
