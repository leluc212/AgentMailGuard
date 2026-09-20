"""MIME parsing, header extraction, and part selection.

Requirements:
- R4.1: Parse MIME and extract canonical headers and body parts.
- R4.2: Extract plain text, converting HTML to text when no text part exists.
- Charset resilience: fallback decode cascade for non-UTF8 and malformed charsets.
"""

from __future__ import annotations

import email.policy
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

from packages.domain.entities import EmailAddress
from services.email_worker.html import html_to_text

logger = logging.getLogger(__name__)

# Subject reply/forward prefix pattern
_SUBJECT_PREFIX_RE = re.compile(
    r"^(\s*(re|fwd|fw|aw|tr|sv|vs)(\[\d+\])?\s*:\s*)+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedHeaders:
    """Extracted and normalized RFC 822 email headers."""

    rfc822_message_id: str | None
    in_reply_to: str | None
    references_ids: list[str] = field(default_factory=list)
    sender: EmailAddress = field(default_factory=lambda: EmailAddress(email=""))
    recipients: list[EmailAddress] = field(default_factory=list)
    cc: list[EmailAddress] = field(default_factory=list)
    subject: str = ""
    subject_normalized: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def normalize_subject(subject: str) -> str:
    """Strip repeated reply/forward prefixes and normalize whitespace."""
    if not subject:
        return ""
    cleaned = _SUBJECT_PREFIX_RE.sub("", subject).strip()
    return re.sub(r"\s+", " ", cleaned)


def decode_header_value(header_value: Any, fallback_charset: str | None = None) -> str:
    """Safely decode RFC 2047 encoded and raw non-ASCII header values."""
    if not header_value:
        return ""

    # If it's a string, it may contain surrogateescapes from raw 8-bit bytes
    if isinstance(header_value, str):
        try:
            raw_bytes = header_value.encode("utf-8", errors="surrogateescape")
            if raw_bytes != header_value.encode("utf-8", errors="ignore"):
                charsets = [
                    fallback_charset,
                    "utf-8",
                    "latin-1",
                    "windows-1252",
                    "shift_jis",
                    "gb2312",
                ]
                for cs in charsets:
                    if not cs:
                        continue
                    try:
                        return raw_bytes.decode(cs)
                    except (LookupError, UnicodeDecodeError):
                        continue
                return raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            pass

    try:
        chunks = decode_header(str(header_value))
        parts: list[str] = []
        for chunk, encoding in chunks:
            if isinstance(chunk, bytes):
                enc = encoding
                if enc in (None, "unknown-8bit"):
                    enc = fallback_charset or "utf-8"
                try:
                    parts.append(chunk.decode(enc, errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    for alt in ("latin-1", "windows-1252", "shift_jis", "gb2312"):
                        try:
                            parts.append(chunk.decode(alt))
                            break
                        except (LookupError, UnicodeDecodeError):
                            continue
                    else:
                        parts.append(chunk.decode("utf-8", errors="replace"))
            else:
                parts.append(str(chunk))
        return " ".join(parts).strip()
    except Exception:
        return str(header_value).strip()


def decode_part_payload(part: Message) -> str:
    """Decode message payload with robust charset fallback."""
    raw_bytes = part.get_payload(decode=True)
    if raw_bytes is None:
        raw_text = part.get_payload()
        if isinstance(raw_text, str):
            return raw_text
        return ""

    if not isinstance(raw_bytes, bytes):
        return str(raw_bytes)

    charset = part.get_content_charset()
    charsets_to_try: list[str] = []
    if charset:
        charsets_to_try.append(charset)
    charsets_to_try.extend(["utf-8", "latin-1", "windows-1252", "shift_jis", "gb2312", "ascii"])

    for cs in charsets_to_try:
        try:
            return raw_bytes.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue

    return raw_bytes.decode("utf-8", errors="replace")


def parse_mime_bytes(raw_mime: bytes) -> Message:
    """Parse raw bytes into an email Message using modern default policy."""
    try:
        return BytesParser(policy=email.policy.default).parsebytes(raw_mime)
    except Exception as exc:
        logger.warning("BytesParser failed with default policy: %s. Retrying compat policy.", exc)
        return BytesParser(policy=email.policy.compat32).parsebytes(raw_mime)


def _clean_message_id(msg_id: str | None) -> str | None:
    if not msg_id:
        return None
    cleaned = msg_id.strip("<> \t\r\n")
    return cleaned if cleaned else None


def _parse_address_list(
    header_val: str | None, fallback_charset: str | None = None
) -> list[EmailAddress]:
    if not header_val:
        return []
    addresses = getaddresses([str(header_val)])
    result: list[EmailAddress] = []
    for name, addr in addresses:
        if addr:
            decoded_name = (
                decode_header_value(name, fallback_charset=fallback_charset) if name else None
            )
            result.append(EmailAddress(email=addr.strip(), name=decoded_name))
    return result


def extract_email_headers(message: Message) -> ParsedHeaders:
    """Extract standard and normalized headers from parsed MIME message."""
    fallback_charset = message.get_content_charset()

    # Collect raw headers to preserve surrogateescape bytes if present
    raw_headers: dict[str, str] = {}
    if hasattr(message, "raw_items"):
        for k, v in message.raw_items():
            raw_headers.setdefault(k.lower(), str(v))

    # Message-ID
    raw_msg_id = raw_headers.get("message-id", message.get("Message-ID"))
    rfc822_message_id = _clean_message_id(raw_msg_id)

    # In-Reply-To
    raw_in_reply_to = raw_headers.get("in-reply-to", message.get("In-Reply-To"))
    in_reply_to = _clean_message_id(raw_in_reply_to)

    # References
    raw_refs = raw_headers.get("references", message.get("References"))
    references_ids: list[str] = []
    if raw_refs:
        for ref_match in re.findall(r"<([^>]+)>", str(raw_refs)):
            cleaned_ref = ref_match.strip()
            if cleaned_ref and cleaned_ref not in references_ids:
                references_ids.append(cleaned_ref)

    # Sender (From)
    from_header = raw_headers.get("from", message.get("From", ""))
    sender_name, sender_email = parseaddr(str(from_header))
    decoded_sender_name = (
        decode_header_value(sender_name, fallback_charset=fallback_charset) if sender_name else None
    )
    sender = EmailAddress(
        email=sender_email.strip() if sender_email else "",
        name=decoded_sender_name,
    )

    # Recipients (To) and CC
    to_header = raw_headers.get("to", message.get("To"))
    cc_header = raw_headers.get("cc", message.get("Cc"))
    recipients = _parse_address_list(to_header, fallback_charset=fallback_charset)
    cc = _parse_address_list(cc_header, fallback_charset=fallback_charset)

    # Subject
    raw_subject = raw_headers.get("subject", message.get("Subject", ""))
    subject = decode_header_value(raw_subject, fallback_charset=fallback_charset)
    subject_normalized = normalize_subject(subject)

    # Date / Received At
    date_header = raw_headers.get("date", message.get("Date"))
    received_at: datetime
    if date_header:
        try:
            parsed_dt = parsedate_to_datetime(str(date_header))
            if parsed_dt.tzinfo is None:
                received_at = parsed_dt.replace(tzinfo=UTC)
            else:
                received_at = parsed_dt.astimezone(UTC)
        except Exception:
            received_at = datetime.now(UTC)
    else:
        received_at = datetime.now(UTC)

    return ParsedHeaders(
        rfc822_message_id=rfc822_message_id,
        in_reply_to=in_reply_to,
        references_ids=references_ids,
        sender=sender,
        recipients=recipients,
        cc=cc,
        subject=subject,
        subject_normalized=subject_normalized,
        received_at=received_at,
    )


def select_message_body(message: Message) -> tuple[str, str | None, bool]:
    """Extract plain text and HTML bodies with fallback conversion (R4.2).

    Returns:
        tuple[str, str | None, bool]:
            - body_text: Extracted plain text (or converted from HTML).
            - html_body: Raw HTML content string if present, else None.
            - is_html_fallback: True if plain text was generated by HTML conversion.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            # Skip attachment parts
            if part.get_content_disposition() == "attachment":
                continue
            # Skip multipart containers
            if part.is_multipart():
                continue

            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(decode_part_payload(part))
            elif content_type == "text/html":
                html_parts.append(decode_part_payload(part))
    else:
        content_type = message.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(decode_part_payload(message))
        elif content_type == "text/html":
            html_parts.append(decode_part_payload(message))
        else:
            plain_parts.append(decode_part_payload(message))

    html_body = "\n".join(html_parts).strip() if html_parts else None

    # Part selection: prefer plain text (R4.2)
    if plain_parts and any(p.strip() for p in plain_parts):
        body_text = "\n\n".join(p.strip() for p in plain_parts if p.strip())
        return (body_text, html_body, False)

    # Fallback to HTML conversion when no plain text part exists (R4.2)
    if html_body:
        converted_text = html_to_text(html_body)
        return (converted_text, html_body, True)

    return ("", None, False)
