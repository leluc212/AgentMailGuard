"""Input limits for a Custom_Email (design.md Property 9; Req 8.5, 8.6, 2.2, 2.3).

Pure functions: no I/O, no framework imports. Lengths are ``len()`` of the Python ``str``.
"""

from __future__ import annotations

MAX_SUBJECT = 500
MAX_BODY = 20_000
MAX_CHUNKS = 5
MAX_CHUNK = 4_000


def clean_chunks(chunks: list[str]) -> list[str]:
    """Drop empty or whitespace-only chunks; keep the rest in order, text unchanged (Req 2.3)."""
    return [chunk for chunk in chunks if chunk.strip()]


def check_email(subject: str, body: str, chunks: list[str]) -> list[str]:
    """Return every violated limit as an English message, in a fixed order.

    ``chunks`` must already be cleaned with :func:`clean_chunks`. An empty subject is accepted;
    a value exactly at its limit is accepted.
    """
    errors: list[str] = []
    if not body.strip():
        errors.append("Body is required")
    if len(subject) > MAX_SUBJECT:
        errors.append(f"Subject is longer than {MAX_SUBJECT:,} characters ({len(subject):,})")
    if len(body) > MAX_BODY:
        errors.append(f"Body is longer than {MAX_BODY:,} characters ({len(body):,})")
    if len(chunks) > MAX_CHUNKS:
        errors.append(f"More than {MAX_CHUNKS} KB chunks ({len(chunks)})")
    for index, chunk in enumerate(chunks, start=1):
        if len(chunk) > MAX_CHUNK:
            errors.append(
                f"KB chunk {index} is longer than {MAX_CHUNK:,} characters ({len(chunk):,})"
            )
    return errors


def limits_message(errors: list[str]) -> str | None:
    """Join all violations into one validation message, or ``None`` when there are none."""
    if not errors:
        return None
    return "Fix these before running: " + "; ".join(errors)
