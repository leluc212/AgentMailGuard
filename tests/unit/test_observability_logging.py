"""Unit tests for structured JSON logging and async correlation context (R21.3)."""

import json
import logging
from collections.abc import Iterator

import pytest

from packages.observability.context import (
    bind_log_context,
    clear_correlation_context,
    get_correlation_context,
    set_correlation_context,
)
from packages.observability.logging import CorrelationFilter, StructuredJSONFormatter, setup_logging


@pytest.fixture(autouse=True)
def clean_context() -> Iterator[None]:
    """Ensure correlation context is pristine before and after every test."""
    clear_correlation_context()
    yield
    clear_correlation_context()


def test_structured_json_formatter_standard_fields() -> None:
    """Verify formatter produces valid JSON with timestamp, level, logger, and message."""
    formatter = StructuredJSONFormatter(service_name="test-worker")
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="/app/test.py",
        lineno=42,
        msg="Processing item %s",
        args=("123",),
        exc_info=None,
    )

    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.logger"
    assert parsed["message"] == "Processing item 123"
    assert parsed["service"] == "test-worker"
    assert "timestamp" in parsed
    assert parsed["location"]["line"] == 42
    assert parsed["location"]["module"] == "test"


def test_mandatory_correlation_keys_present_in_json() -> None:
    """Verify all 5 correlation keys are present in JSON output (R21.3)."""
    formatter = StructuredJSONFormatter()

    with bind_log_context(
        trace_id="trace-001",
        message_id="msg-002",
        thread_id="th-003",
        job_id="job-004",
        organization_id="org-005",
    ):
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="/app/test.py",
            lineno=10,
            msg="Action with context",
            args=(),
            exc_info=None,
        )
        parsed = json.loads(formatter.format(record))

        assert parsed["trace_id"] == "trace-001"
        assert parsed["message_id"] == "msg-002"
        assert parsed["thread_id"] == "th-003"
        assert parsed["job_id"] == "job-004"
        assert parsed["organization_id"] == "org-005"


def test_bind_log_context_resets_on_exit() -> None:
    """Verify bind_log_context restores previous context upon exiting the context block."""
    set_correlation_context(trace_id="outer-trace", organization_id="org-1")
    assert get_correlation_context()["trace_id"] == "outer-trace"

    with bind_log_context(trace_id="inner-trace", job_id="job-99"):
        ctx = get_correlation_context()
        assert ctx["trace_id"] == "inner-trace"
        assert ctx["job_id"] == "job-99"
        assert ctx["organization_id"] == "org-1"

    # Restored to outer context
    ctx = get_correlation_context()
    assert ctx["trace_id"] == "outer-trace"
    assert "job_id" not in ctx


def test_structured_json_formatter_records_exception() -> None:
    """Verify exceptions are parsed into type, message, and stacktrace in JSON output."""
    formatter = StructuredJSONFormatter()

    try:
        raise ValueError("Invalid configuration state")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="test.err",
        level=logging.ERROR,
        pathname="/app/test.py",
        lineno=80,
        msg="Failure occurred",
        args=(),
        exc_info=exc_info,
    )

    parsed = json.loads(formatter.format(record))
    assert parsed["level"] == "ERROR"
    assert "exception" in parsed
    assert parsed["exception"]["type"] == "ValueError"
    assert "Invalid configuration state" in parsed["exception"]["message"]
    assert len(parsed["exception"]["stacktrace"]) > 0


def test_setup_logging_and_filter() -> None:
    """Verify setup_logging configures root logger and CorrelationFilter passes context."""
    setup_logging(level="DEBUG", json_format=True, service_name="api-test")
    logger = logging.getLogger("test.filter")

    with bind_log_context(trace_id="filt-trace-123"):
        record = logger.makeRecord("test.filter", logging.DEBUG, "fn.py", 12, "Msg", (), None)
        f = CorrelationFilter()
        assert f.filter(record) is True
        assert getattr(record, "trace_id", None) == "filt-trace-123"  # noqa: B009
