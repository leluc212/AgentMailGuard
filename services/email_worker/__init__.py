"""Package initialization for services/email_worker."""

from services.email_worker.attachments import (
    ExtractedAttachment,
    extract_attachments_from_message,
    offload_attachments,
)
from services.email_worker.consumer import EmailNormalizationConsumer
from services.email_worker.history import (
    clean_email_body,
    detect_and_strip_signature,
    separate_quoted_history,
)
from services.email_worker.html import HTMLToTextConverter, html_to_text
from services.email_worker.normalizer import (
    EmailNormalizer,
    NormalizationContext,
    NormalizationResult,
)
from services.email_worker.parser import (
    ParsedHeaders,
    decode_header_value,
    decode_part_payload,
    extract_email_headers,
    normalize_subject,
    parse_mime_bytes,
    select_message_body,
)
from services.email_worker.persister import (
    EmailPersistenceResult,
    EmailPersister,
)
from services.email_worker.threading import (
    ThreadAssociationReason,
    ThreadAssociationResult,
    ThreadAssociator,
    extract_participant_emails,
)

__all__ = [
    "EmailNormalizationConsumer",
    "EmailNormalizer",
    "EmailPersistenceResult",
    "EmailPersister",
    "ExtractedAttachment",
    "HTMLToTextConverter",
    "NormalizationContext",
    "NormalizationResult",
    "ParsedHeaders",
    "ThreadAssociationReason",
    "ThreadAssociationResult",
    "ThreadAssociator",
    "clean_email_body",
    "decode_header_value",
    "decode_part_payload",
    "detect_and_strip_signature",
    "extract_attachments_from_message",
    "extract_email_headers",
    "extract_participant_emails",
    "html_to_text",
    "normalize_subject",
    "offload_attachments",
    "parse_mime_bytes",
    "select_message_body",
    "separate_quoted_history",
]
