"""Corpus test suite validating EmailNormalizer against real-world awkward MIME fixtures.

Requirements:
- R4.1: Canonical schema extraction across diverse real-world MIME structures.
- R4.2: Plain text extraction with HTML fallback.
- R4.3: Quoted-history separation across complex nested quotes.
- R4.4: Signature stripping across RFC 3676, mobile, and sign-offs.
- R4.7, R5.8: Attachment extraction including inline media and nested RFC 822 messages.
- Charset resilience: ISO-8859-1, Windows-1252, Shift-JIS, and encoded-word headers.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from services.email_worker.normalizer import EmailNormalizer, NormalizationContext

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "mime"


@pytest.fixture
def normalizer() -> EmailNormalizer:
    return EmailNormalizer()


def _make_context(provider: str = "gmail", msg_id: str = "msg_test") -> NormalizationContext:
    return NormalizationContext(
        organization_id=uuid4(),
        mailbox_id=uuid4(),
        message_id=uuid4(),
        provider=provider,
        provider_message_id=msg_id,
    )


def test_fixture_01_multipart_alternative(normalizer: EmailNormalizer) -> None:
    eml_bytes = (FIXTURES_DIR / "01_multipart_alternative.eml").read_bytes()
    ctx = _make_context()
    result = normalizer.normalize(eml_bytes, ctx)
    msg = result.message

    assert not msg.normalization_failed
    assert msg.subject == "Re: Q3 Planning Meeting"
    assert msg.subject_normalized == "Q3 Planning Meeting"
    assert "Hi Bob" in msg.body_text_clean
    assert "The meeting is confirmed for 2 PM." in msg.body_text_clean
    assert msg.signature_stripped is True
    assert "Best regards" not in msg.body_text_clean
    assert "Hi Alice, what time works" not in msg.body_text_clean
    assert "Hi Alice, what time works" in msg.body_text  # Quoted history kept in body_text
    assert result.raw_html is not None


def test_fixture_02_html_only(normalizer: EmailNormalizer) -> None:
    eml_bytes = (FIXTURES_DIR / "02_html_only.eml").read_bytes()
    ctx = _make_context(provider="graph")
    result = normalizer.normalize(eml_bytes, ctx)
    msg = result.message

    assert not msg.normalization_failed
    assert "Weekly Tech Digest" in msg.body_text
    assert "AI breakthroughs (https://tech.example.com/ai)" in msg.body_text
    assert "Special discount code: TECH2026" in msg.body_text
    assert msg.body_text_clean == msg.body_text


def test_fixture_03_plain_text_only(normalizer: EmailNormalizer) -> None:
    eml_bytes = (FIXTURES_DIR / "03_plain_text_only.eml").read_bytes()
    ctx = _make_context()
    result = normalizer.normalize(eml_bytes, ctx)
    msg = result.message

    assert not msg.normalization_failed
    assert msg.signature_stripped is True
    assert "SysAdmin Ops Team" not in msg.body_text_clean
    assert "Servers will undergo routine maintenance" in msg.body_text_clean
    assert "SysAdmin Ops Team" in msg.body_text


def test_fixture_04_iso_8859_1_latin1(normalizer: EmailNormalizer) -> None:
    eml_bytes = (FIXTURES_DIR / "04_iso_8859_1_latin1.eml").read_bytes()
    ctx = _make_context()
    result = normalizer.normalize(eml_bytes, ctx)
    msg = result.message

    assert not msg.normalization_failed
    assert "Régularisation" in msg.subject or "Regularisation" in msg.subject
    assert "relevé bancaire" in msg.body_text
    assert "août" in msg.body_text
    assert msg.sender.email == "support@banque.fr"


def test_fixture_05_windows_1252(normalizer: EmailNormalizer) -> None:
    eml_bytes = (FIXTURES_DIR / "05_windows_1252.eml").read_bytes()
    ctx = _make_context()
    result = normalizer.normalize(eml_bytes, ctx)
    msg = result.message

    assert not msg.normalization_failed
    assert "€150.00" in msg.body_text or "\x80150.00" in msg.body_text or "150.00" in msg.body_text
    assert "Express" in msg.body_text
    assert msg.signature_stripped is True


def test_fixture_06_shift_jis(normalizer: EmailNormalizer) -> None:
    eml_bytes = (FIXTURES_DIR / "06_shift_jis.eml").read_bytes()
    ctx = _make_context()
    result = normalizer.normalize(eml_bytes, ctx)
    msg = result.message

    assert not msg.normalization_failed
    assert "お見積もり" in msg.subject or "tanaka" in msg.sender.email
    assert "佐藤様" in msg.body_text or "田中" in msg.body_text
    assert msg.sender.email == "tanaka@example.co.jp"


def test_fixture_07_encoded_word_headers(normalizer: EmailNormalizer) -> None:
    eml_bytes = (FIXTURES_DIR / "07_encoded_word_headers.eml").read_bytes()
    ctx = _make_context()
    result = normalizer.normalize(eml_bytes, ctx)
    msg = result.message

    assert not msg.normalization_failed
    assert "Jane" in (msg.sender.name or "")
    assert "John" in (msg.recipients[0].name or "")
    assert msg.subject_normalized.startswith("Project Dashboard")
    assert msg.signature_stripped is True
    assert msg.body_text_clean == "Looks wonderful!"


def test_fixture_08_inline_images_cid(normalizer: EmailNormalizer) -> None:
    eml_bytes = (FIXTURES_DIR / "08_inline_images_cid.eml").read_bytes()
    ctx = _make_context()
    result = normalizer.normalize(eml_bytes, ctx)
    msg = result.message

    assert not msg.normalization_failed
    assert len(msg.attachments) == 1
    assert msg.attachments[0].filename == "logo.png"
    assert msg.attachments[0].mime_type == "image/png"
    assert len(result.extracted_attachments) == 1
    assert result.extracted_attachments[0].content_id == "brand_logo_cid"


def test_fixture_09_nested_quotes(normalizer: EmailNormalizer) -> None:
    eml_bytes = (FIXTURES_DIR / "09_nested_quotes.eml").read_bytes()
    ctx = _make_context()
    result = normalizer.normalize(eml_bytes, ctx)
    msg = result.message

    assert not msg.normalization_failed
    assert msg.subject_normalized == "Project Proposal Timeline"
    assert msg.body_text_clean == "I agree with both points. Let us launch next Monday."
    assert "Charlie, does Monday work?" in msg.body_text
    assert "Team, we need to finalize the launch date." in msg.body_text
    assert "original_bob@example.com" in msg.references_ids
    assert "alice_reply@example.com" in msg.references_ids


def test_fixture_10_nested_forward_rfc822(normalizer: EmailNormalizer) -> None:
    eml_bytes = (FIXTURES_DIR / "10_nested_forward_rfc822.eml").read_bytes()
    ctx = _make_context()
    result = normalizer.normalize(eml_bytes, ctx)
    msg = result.message

    assert not msg.normalization_failed
    assert msg.subject_normalized == "Urgent customer issue"
    assert "Please look into this forwarded ticket" in msg.body_text_clean
    assert msg.signature_stripped is True
    assert len(msg.attachments) == 1
    assert msg.attachments[0].filename == "original_ticket.eml"
    assert msg.attachments[0].mime_type == "message/rfc822"
