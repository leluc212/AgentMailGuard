"""Unit tests for pure email normalization components.

Requirements:
- R4.1: MIME parsing to canonical NormalizedMessage.
- R4.2: Plain text extraction with HTML-to-text fallback.
- R4.3: Quoted-history separation (body_text vs body_text_clean).
- R4.4: Signature detection and stripping.
- R4.7, R5.8: Attachment extraction, SHA-256 checksums, object key generation.
- R24.3: Pure unit tests for every pure component.
"""

from __future__ import annotations

import hashlib
from email.message import EmailMessage
from typing import Any
from uuid import uuid4

import pytest

from packages.domain.entities import EmailAddress
from services.email_worker.attachments import (
    ExtractedAttachment,
    extract_attachments_from_message,
    offload_attachments,
)
from services.email_worker.history import (
    detect_and_strip_signature,
    separate_quoted_history,
)
from services.email_worker.html import html_to_text
from services.email_worker.normalizer import (
    EmailNormalizer,
    NormalizationContext,
)
from services.email_worker.parser import (
    normalize_subject,
)


def _create_sample_message_with_attachment() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "test@example.com"
    msg["To"] = "dest@example.com"
    msg["Subject"] = "Attachment"
    msg.set_content("Body")
    msg.add_attachment(b"data", maintype="text", subtype="plain", filename="test.txt")
    return msg


class TestHTMLToText:
    """Tests for pure HTMLToTextConverter and html_to_text (R4.2)."""

    def test_preserves_hyperlinks_with_anchor_text(self) -> None:
        html = '<p>Visit <a href="https://example.com/pricing">Our Pricing</a> for details.</p>'
        text = html_to_text(html)
        assert text == "Visit Our Pricing (https://example.com/pricing) for details."

    def test_hyperlink_without_distinct_text_shows_url(self) -> None:
        html = '<a href="https://example.com">https://example.com</a>'
        assert html_to_text(html) == "https://example.com"

        html_empty_anchor = '<p>Click <a href="https://example.com"></a></p>'
        assert html_to_text(html_empty_anchor) == "Click https://example.com"

    def test_strips_tracking_pixels(self) -> None:
        html = (
            "<p>Email content here.</p>"
            '<img src="https://tracker.com/pixel.gif" width="1" height="1" alt="track" />'
            '<img src="https://tracker.com/pixel2.gif" width="0" height="0" />'
            '<img src="https://tracker.com/pixel3.gif" style="display:none;" />'
            '<img src="https://tracker.com/pixel4.gif" style="visibility: hidden;" />'
            '<img src="https://tracker.com/pixel5.gif" style="width: 1px; height: 1px;" />'
            '<img src="https://example.com/logo.png" width="300" height="100" alt="Company Logo" />'
        )
        text = html_to_text(html)
        assert "track" not in text
        assert "pixel" not in text
        assert text == "Email content here.\n[Company Logo]"

    def test_strips_scripts_and_styles(self) -> None:
        html = (
            "<html><head><style>body { color: red; }</style></head>"
            "<body>"
            "<script>alert('malicious');</script>"
            "<noscript>Enable JS</noscript>"
            "<p>Safe content.</p>"
            "</body></html>"
        )
        text = html_to_text(html)
        assert text == "Safe content."
        assert "alert" not in text
        assert "color: red" not in text

    def test_handles_block_elements_and_tables(self) -> None:
        html = (
            "<h1>Header Title</h1>"
            "<p>Paragraph 1</p>"
            "<ul><li>Item A</li><li>Item B</li></ul>"
            "<table><tr><td>Cell 1</td><td>Cell 2</td></tr></table>"
        )
        text = html_to_text(html)
        assert "Header Title" in text
        assert "Paragraph 1" in text
        assert "- Item A" in text
        assert "- Item B" in text
        assert "Cell 1\tCell 2" in text

    def test_unescapes_html_entities(self) -> None:
        html = "<p>Tom &amp; Jerry &gt; Mickey &lt; Pluto &quot;Friends&#39; Day&quot; &nbsp;</p>"
        text = html_to_text(html)
        assert text == 'Tom & Jerry > Mickey < Pluto "Friends\' Day"'

    def test_empty_html_returns_empty_string(self) -> None:
        assert html_to_text("") == ""
        assert html_to_text("   ") == ""


class TestQuotedHistorySeparation:
    """Tests for separate_quoted_history (R4.3)."""

    def test_on_date_wrote_header(self) -> None:
        body = (
            "Hi Alice,\n\n"
            "Here is the updated quote.\n\n"
            "On Wed, Sep 16, 2026 at 4:32 PM Bob <bob@acme.com> wrote:\n"
            "> Can you send the quote?\n"
            "> Thanks!"
        )
        new_content, quoted = separate_quoted_history(body)
        assert new_content == "Hi Alice,\n\nHere is the updated quote."
        assert "On Wed, Sep 16, 2026 at 4:32 PM Bob <bob@acme.com> wrote:" in quoted
        assert "> Can you send the quote?" in quoted

    def test_outlook_original_message_header(self) -> None:
        body = (
            "Approved.\n\n"
            "-----Original Message-----\n"
            "From: Sarah [mailto:sarah@corp.com]\n"
            "Sent: Tuesday, September 15, 2026 10:00 AM\n"
            "To: Dave\n"
            "Subject: Budget signoff\n\n"
            "Please approve the attached budget."
        )
        new_content, quoted = separate_quoted_history(body)
        assert new_content == "Approved."
        assert "-----Original Message-----" in quoted
        assert "Please approve the attached budget." in quoted

    def test_forwarded_message_divider(self) -> None:
        body = (
            "FYI on this thread.\n\n"
            "---------- Forwarded message ---------\n"
            "From: Support <support@vendor.com>\n"
            "Subject: Ticket #1234\n\n"
            "Issue is resolved."
        )
        new_content, quoted = separate_quoted_history(body)
        assert new_content == "FYI on this thread."
        assert "Forwarded message" in quoted

    def test_trailing_greater_than_quote_lines(self) -> None:
        body = (
            "Sounds good to me.\n\n"
            "> Let's schedule the call for Thursday at 3pm.\n"
            "> Does that work?"
        )
        new_content, quoted = separate_quoted_history(body)
        assert new_content == "Sounds good to me."
        assert "> Let's schedule the call" in quoted

    def test_no_quote_present(self) -> None:
        body = "Hello, just checking in on the progress. Thanks!"
        new_content, quoted = separate_quoted_history(body)
        assert new_content == body
        assert quoted == ""


class TestSignatureDetection:
    """Tests for detect_and_strip_signature (R4.4)."""

    def test_rfc3676_dash_dash_space_delimiter(self) -> None:
        body = (
            "The invoice is attached.\n\n"
            "-- \n"
            "John Smith\n"
            "Senior Accountant\n"
            "Acme Corp | +1-555-0100"
        )
        cleaned, stripped = detect_and_strip_signature(body)
        assert stripped is True
        assert cleaned == "The invoice is attached."

    def test_mobile_device_signature(self) -> None:
        body = "I'll be there in 10 minutes.\n\nSent from my iPhone"
        cleaned, stripped = detect_and_strip_signature(body)
        assert stripped is True
        assert cleaned == "I'll be there in 10 minutes."

    def test_standard_signoff_closing(self) -> None:
        body = (
            "Please find the documents requested.\n\n"
            "Best regards,\n"
            "Elena Rostova\n"
            "Director of Operations"
        )
        cleaned, stripped = detect_and_strip_signature(body)
        assert stripped is True
        assert cleaned == "Please find the documents requested."

    def test_signoff_alone_is_not_stripped(self) -> None:
        body = "Thanks,"
        cleaned, stripped = detect_and_strip_signature(body)
        assert stripped is False
        assert cleaned == "Thanks,"

    def test_no_signature_returns_original(self) -> None:
        body = "Here is the data: 123, 456, 789."
        cleaned, stripped = detect_and_strip_signature(body)
        assert stripped is False
        assert cleaned == body


class TestSubjectNormalization:
    """Tests for normalize_subject."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Re: Urgent Meeting", "Urgent Meeting"),
            ("FW: Quarterly Report", "Quarterly Report"),
            ("re: fw: re: Contract Renewal", "Contract Renewal"),
            ("Re[2]: Ticket #44", "Ticket #44"),
            ("Fwd: Re: Aw: Status update", "Status update"),
            ("Clean Subject Line", "Clean Subject Line"),
            ("  Re:   Spaced   Subject  ", "Spaced Subject"),
            ("", ""),
        ],
    )
    def test_subject_cleaning(self, raw: str, expected: str) -> None:
        assert normalize_subject(raw) == expected


class TestAttachmentExtraction:
    """Tests for attachment metadata extraction and SHA-256 (R4.7, R5.8)."""

    def test_extracts_attachments_and_inline_media(self) -> None:
        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "Files"
        msg.set_content("Please see attachments.")

        pdf_payload = b"%PDF-1.4 simulated pdf data 12345"
        msg.add_attachment(
            pdf_payload,
            maintype="application",
            subtype="pdf",
            filename="invoice.pdf",
        )

        png_payload = b"\x89PNG\r\n\x1a\n simulated png"
        msg.add_attachment(
            png_payload,
            maintype="image",
            subtype="png",
            filename="logo.png",
            cid="logo_cid_01",
        )

        org_id = uuid4()
        msg_id = uuid4()

        extracted = extract_attachments_from_message(msg, org_id, msg_id)
        assert len(extracted) == 2

        pdf_att = next(e for e in extracted if e.ref.filename == "invoice.pdf")
        assert pdf_att.ref.mime_type == "application/pdf"
        assert pdf_att.ref.size_bytes == len(pdf_payload)
        assert pdf_att.ref.checksum == hashlib.sha256(pdf_payload).hexdigest()
        assert pdf_att.ref.object_key.startswith(f"attachments/{org_id}/{msg_id}/")
        assert pdf_att.ref.object_key.endswith("/invoice.pdf")

        png_att = next(e for e in extracted if e.ref.filename == "logo.png")
        assert png_att.ref.mime_type == "image/png"
        assert png_att.content_id == "logo_cid_01"

    @pytest.mark.asyncio
    async def test_offload_attachments_calls_storage(self, mocker: Any) -> None:
        mock_storage = mocker.AsyncMock()
        extracted_att = ExtractedAttachment(
            ref=extract_attachments_from_message(
                _create_sample_message_with_attachment(),
                uuid4(),
                uuid4(),
            )[0].ref,
            payload=b"sample payload",
            attachment_id=uuid4(),
            content_id=None,
        )

        refs = await offload_attachments([extracted_att], mock_storage, bucket="test-bucket")
        assert len(refs) == 1
        mock_storage.put_bytes.assert_awaited_once()


class TestEmailNormalizer:
    """Tests for full EmailNormalizer orchestration (R4.1, R4.2, R4.3, R4.4, design.md §5.2)."""

    def test_normalize_multipart_alternative_prefers_plain_text(self) -> None:
        msg = EmailMessage()
        msg["From"] = "Sender Name <sender@example.com>"
        msg["To"] = "Recipient <recipient@example.com>"
        msg["Subject"] = "Re: Project Update"
        msg["Message-ID"] = "<msg_test_001@example.com>"
        msg.set_content("Plain text body content.\n\nBest regards,\nSender")
        msg.add_alternative(
            "<p>HTML body with <a href='https://example.com'>link</a></p>",
            subtype="html",
        )

        normalizer = EmailNormalizer()
        context = NormalizationContext(
            organization_id=uuid4(),
            mailbox_id=uuid4(),
            message_id=uuid4(),
            provider="gmail",
            provider_message_id="gm_001",
        )

        result = normalizer.normalize(msg.as_bytes(), context)
        norm_msg = result.message

        assert norm_msg.message_id == context.message_id
        assert norm_msg.rfc822_message_id == "msg_test_001@example.com"
        assert norm_msg.sender == EmailAddress("sender@example.com", "Sender Name")
        assert norm_msg.subject == "Re: Project Update"
        assert norm_msg.subject_normalized == "Project Update"
        assert norm_msg.body_text == "Plain text body content.\n\nBest regards,\nSender"
        assert norm_msg.body_text_clean == "Plain text body content."
        assert norm_msg.signature_stripped is True
        assert norm_msg.flags == {"normalization_failed": False, "signature_stripped": True}
        assert result.raw_html is not None

        # Verify contract dictionary format
        contract = norm_msg.to_contract_dict()
        assert contract["provider"] == "gmail"
        assert contract["sender"]["email"] == "sender@example.com"
        assert contract["subject_normalized"] == "Project Update"
        assert contract["flags"]["signature_stripped"] is True

    def test_normalize_html_only_converts_to_text(self) -> None:
        raw_eml = (
            b"From: html_sender@example.com\r\n"
            b"To: receiver@example.com\r\n"
            b"Subject: HTML Only Newsletter\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b"<h1>Special Offer</h1><p>Check <a href='https://deal.com'>Deals</a></p>"
        )

        normalizer = EmailNormalizer()
        context = NormalizationContext(
            organization_id=uuid4(),
            mailbox_id=uuid4(),
            message_id=uuid4(),
            provider="graph",
            provider_message_id="graph_99",
        )

        result = normalizer.normalize(raw_eml, context)
        norm_msg = result.message

        assert norm_msg.normalization_failed is False
        assert "Special Offer" in norm_msg.body_text
        assert "Deals (https://deal.com)" in norm_msg.body_text
        assert norm_msg.body_text_clean == norm_msg.body_text

    def test_normalize_corrupted_payload_records_failed_flag(self) -> None:
        corrupted_bytes = b"\xff\xfe\x00\x00\x12\x34\x00Malformed Non-Email Garbage\x00\x00"

        normalizer = EmailNormalizer()
        context = NormalizationContext(
            organization_id=uuid4(),
            mailbox_id=uuid4(),
            message_id=uuid4(),
            provider="imap",
            provider_message_id="imap_fail",
        )

        result = normalizer.normalize(corrupted_bytes, context)
        norm_msg = result.message

        assert norm_msg.message_id == context.message_id
        assert norm_msg.flags["normalization_failed"] == norm_msg.normalization_failed
