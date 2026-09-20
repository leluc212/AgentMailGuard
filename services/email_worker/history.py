"""Quoted-history separation and signature stripping for normalized emails.

Requirements:
- R4.3: Separate quoted reply history from the new content and persist
        both body_text (full text) and body_text_clean (new content only).
- R4.4: Detect and strip signature blocks, recording whether detection succeeded.
"""

from __future__ import annotations

import re

# Quoted history boundary header patterns
QUOTE_HEADERS: list[re.Pattern[str]] = [
    # On <date/time>, <person> wrote:
    re.compile(r"^\s*On\s+.+?wrote:\s*$", re.IGNORECASE | re.MULTILINE),
    # At <date/time>, <person> wrote:
    re.compile(r"^\s*At\s+.+?wrote:\s*$", re.IGNORECASE | re.MULTILINE),
    # Outlook style dividers
    re.compile(r"^\s*-+\s*Original Message\s*-+\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*_{10,}\s*$", re.MULTILINE),
    # Forwarded message dividers
    re.compile(r"^\s*-+\s*Forwarded message\s*-+\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Begin forwarded message:\s*$", re.IGNORECASE | re.MULTILINE),
    # Outlook headers: From: ... \n Sent/Date: ...
    re.compile(r"^\s*From:\s*.+?\n\s*(?:Sent|Date):\s*.+?$", re.IGNORECASE | re.MULTILINE),
    # International quote headers
    re.compile(r"^\s*Le\s+.+?a écrit\s*:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Am\s+.+?schrieb\s*.+?:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*El\s+.+?escribió\s*:\s*$", re.IGNORECASE | re.MULTILINE),
]

# Mobile signatures
MOBILE_SIGNATURE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"^\s*Sent from my (?:iPhone|iPad|Galaxy|Android|mobile|phone).*?$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(r"^\s*Sent from Mail for Windows.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Get Outlook for (?:iOS|Android).*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Sent from Samsung Mobile.*$", re.IGNORECASE | re.MULTILINE),
]

# Standard sign-offs
SIGN_OFF_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"^\s*(?:Best regards|Kind regards|Warm regards|With regards|Regards|Best),?\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:Thanks|Thank you|Many thanks),?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:Sincerely|Yours sincerely|Yours faithfully),?\s*$", re.IGNORECASE),
    re.compile(r"^\s*Cheers,?\s*$", re.IGNORECASE),
]

# RFC 3676 signature delimiter: "-- " or "--" on its own line
RFC_SIGNATURE_PATTERN = re.compile(r"^-- ?$", re.MULTILINE)


def separate_quoted_history(text: str) -> tuple[str, str]:
    """Separate quoted reply history from the new content (R4.3).

    Returns:
        tuple[str, str]: (new_content, quoted_history)
        If no quoted history is detected, returns (text.strip(), "").
    """
    if not text or not text.strip():
        return ("", "")

    earliest_pos: int | None = None

    # Check for known quote boundary headers
    for pattern in QUOTE_HEADERS:
        match = pattern.search(text)
        if match:
            pos = match.start()
            if earliest_pos is None or pos < earliest_pos:
                earliest_pos = pos

    # If no header pattern matched, check for a trailing block of quote lines (> or |)
    if earliest_pos is None:
        lines = text.splitlines(keepends=True)
        quote_start_idx: int | None = None
        for idx, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith((">", "|")):
                if quote_start_idx is None:
                    quote_start_idx = idx
            else:
                # Blank line inside quotes can be allowed if followed by quotes, but
                # if regular text follows, reset
                if stripped:
                    quote_start_idx = None

        # Only treat as quote section if it reaches the end of the message
        # and has at least 1 quoted line
        if quote_start_idx is not None and quote_start_idx > 0:
            earliest_pos = sum(len(line) for line in lines[:quote_start_idx])

    if earliest_pos is not None:
        new_content = text[:earliest_pos].strip()
        quoted_history = text[earliest_pos:].strip()
        return (new_content, quoted_history)

    return (text.strip(), "")


def detect_and_strip_signature(text: str) -> tuple[str, bool]:
    """Detect and strip email signature blocks (R4.4).

    Returns:
        tuple[str, bool]: (cleaned_text, signature_stripped)
    """
    if not text or not text.strip():
        return ("", False)

    # 1. RFC 3676 delimiter ("-- " or "--")
    match = RFC_SIGNATURE_PATTERN.search(text)
    if match:
        cut_pos = match.start()
        cleaned = text[:cut_pos].strip()
        return (cleaned, True)

    # 2. Mobile signatures
    for pattern in MOBILE_SIGNATURE_PATTERNS:
        match = pattern.search(text)
        if match:
            cut_pos = match.start()
            cleaned = text[:cut_pos].strip()
            return (cleaned, True)

    # 3. Common sign-offs near the end of message (within last 10 lines)
    lines = text.splitlines()
    search_window = min(len(lines), 10)
    start_index = len(lines) - search_window

    for i in range(start_index, len(lines)):
        line = lines[i].strip()
        if not line:
            continue
        for sign_off in SIGN_OFF_PATTERNS:
            if sign_off.match(line):
                # Ensure the sign-off is not the ONLY line in the entire email
                cleaned_lines = lines[:i]
                if any(cl.strip() for cl in cleaned_lines):
                    cleaned = "\n".join(cleaned_lines).strip()
                    return (cleaned, True)

    return (text.strip(), False)


def clean_email_body(full_text: str) -> tuple[str, str, bool]:
    """Process email text into full body and clean body (R4.3, R4.4).

    Returns:
        tuple[str, str, bool]: (body_text, body_text_clean, signature_stripped)
    """
    body_text = full_text.strip()
    new_content, _ = separate_quoted_history(body_text)
    body_text_clean, signature_stripped = detect_and_strip_signature(new_content)

    return (body_text, body_text_clean, signature_stripped)
