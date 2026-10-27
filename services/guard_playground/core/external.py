"""External_Data from the committee: .eml / .json / .jsonl uploads (Requirement 21).

Files and their emails are kept in memory only and are never written to disk (21.13).
"""

from __future__ import annotations

import email
import json
from dataclasses import dataclass, field
from email import policy
from html.parser import HTMLParser
from typing import Any

from services.guard_playground.core.limits import check_email, clean_chunks

MAX_BYTES = 5 * 1024 * 1024
MAX_EMAILS = 50
EXTENSIONS = (".eml", ".json", ".jsonl")


@dataclass(frozen=True)
class ExternalEmail:
    position: int  # 1-based position in the file
    ext_id: str | None
    subject: str
    body: str
    chunks: tuple[str, ...] = ()


@dataclass
class ParseResult:
    emails: list[ExternalEmail] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)  # "#3 (id x): Body is required"
    error: str | None = None  # whole file rejected (21.5)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return "".join(parser.parts).strip()


def _from_object(obj: Any) -> tuple[str | None, str, str, list[str]] | str:
    """Minimal ({subject, body, id, chunks}) or benchmark case schema -> fields, or a rule."""
    if not isinstance(obj, dict):
        return "not an object"
    if isinstance(obj.get("email"), dict):  # benchmark case schema
        mail = obj["email"]
        body = mail.get("body_text") or mail.get("body")
        subject = mail.get("subject", "")
        ext_id = obj.get("case_id")
        raw_chunks = [
            c.get("content", "") if isinstance(c, dict) else c for c in obj.get("chunks") or []
        ]
    else:
        body = obj.get("body")
        subject = obj.get("subject", "")
        ext_id = obj.get("id")
        raw_chunks = obj.get("chunks") or []
    if not isinstance(body, str):
        return "missing body string"
    if not isinstance(subject, str):
        return "subject must be a string"
    if not isinstance(raw_chunks, list) or not all(isinstance(c, str) for c in raw_chunks):
        return "chunks must be a list of strings"
    return (str(ext_id) if ext_id is not None else None, subject, body, list(raw_chunks))


def _parse_eml(data: bytes) -> list[Any]:
    msg = email.message_from_bytes(data, policy=policy.default)
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        body = part.get_content()
    else:
        html = msg.get_body(preferencelist=("html",))
        body = _html_to_text(html.get_content()) if html is not None else ""
    return [{"subject": str(msg.get("Subject", "")), "body": body}]


def parse_upload(filename: str, data: bytes) -> ParseResult:
    name = (filename or "").lower()
    ext = next((e for e in EXTENSIONS if name.endswith(e)), None)
    if ext is None:
        return ParseResult(error="Unsupported file type: use .eml, .json or .jsonl")
    if len(data) > MAX_BYTES:
        return ParseResult(error="File is larger than 5 MB")
    try:
        if ext == ".eml":
            objects = _parse_eml(data)
        else:
            text = data.decode("utf-8-sig")
            if ext == ".json":
                loaded = json.loads(text)
                objects = loaded if isinstance(loaded, list) else [loaded]
            else:
                objects = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (ValueError, UnicodeDecodeError):
        return ParseResult(error="File could not be parsed")
    if len(objects) > MAX_EMAILS:
        return ParseResult(error=f"File holds {len(objects)} emails; the limit is 50")
    result = ParseResult()
    for position, obj in enumerate(objects, start=1):
        fields = _from_object(obj)
        if isinstance(fields, str):
            result.excluded.append(f"#{position}: {fields}")
            continue
        ext_id, subject, body, raw_chunks = fields
        chunks = clean_chunks(raw_chunks)
        errors = check_email(subject, body, chunks)
        label = f"#{position}" + (f" (id {ext_id})" if ext_id else "")
        if errors:
            result.excluded.append(f"{label}: {'; '.join(errors)}")
            continue
        result.emails.append(ExternalEmail(position, ext_id, subject, body, tuple(chunks)))
    if not result.emails and not result.excluded:
        result.error = "File holds no email"
    return result
