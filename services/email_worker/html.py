"""Pure HTML to plain text converter for email bodies.

Requirements:
- R4.2: Extract plain text, converting HTML bodies to text when no text part exists.
- Hyperlink preservation: format as 'anchor text (url)'.
- Tracking pixel removal: omit 1x1, 0x0, or hidden images.
- Script and style stripping: omit code, styles, and hidden tags.
- Zero third-party HTML dependencies: uses stdlib html.parser.HTMLParser.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import ClassVar


class HTMLToTextConverter(HTMLParser):
    """Converts HTML email bodies into clean, normalized plain text."""

    IGNORED_TAGS: ClassVar[set[str]] = {
        "canvas",
        "head",
        "meta",
        "noscript",
        "script",
        "style",
        "svg",
        "title",
    }

    BLOCK_TAGS: ClassVar[set[str]] = {
        "article",
        "blockquote",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._output_chunks: list[str] = []
        self._ignored_depth = 0
        self._current_href: str | None = None
        self._link_text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()

        if tag_lower in self.IGNORED_TAGS:
            self._ignored_depth += 1
            return

        if self._ignored_depth > 0:
            return

        attrs_dict = {k.lower(): (v or "") for k, v in attrs}

        # Check for tracking pixel / invisible images
        if tag_lower == "img":
            if self._is_tracking_pixel(attrs_dict):
                return
            alt = attrs_dict.get("alt", "").strip()
            if alt:
                self._append_text(f"[{alt}]")
            return

        # Hyperlinks
        if tag_lower == "a":
            href = attrs_dict.get("href", "").strip()
            if href and not href.startswith("javascript:"):
                self._current_href = href
                self._link_text_chunks = []
            return

        # List items
        if tag_lower == "li":
            self._ensure_newline()
            self._append_text("- ")
            return

        # Table cells
        if tag_lower in {"td", "th"}:
            if self._output_chunks and not self._output_chunks[-1].endswith(("\n", "\t", " ")):
                self._append_text("\t")
            return

        # Line breaks
        if tag_lower == "br":
            self._append_text("\n")
            return

        if tag_lower == "hr":
            self._ensure_newline()
            self._append_text("---\n")
            return

        # Block containers
        if tag_lower in self.BLOCK_TAGS:
            self._ensure_newline()

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()

        if tag_lower in self.IGNORED_TAGS:
            if self._ignored_depth > 0:
                self._ignored_depth -= 1
            return

        if self._ignored_depth > 0:
            return

        if tag_lower == "a" and self._current_href is not None:
            anchor_text = "".join(self._link_text_chunks).strip()
            href = self._current_href
            self._current_href = None
            self._link_text_chunks = []

            if anchor_text and anchor_text != href:
                self._append_text(f"{anchor_text} ({href})")
            elif anchor_text:
                self._append_text(anchor_text)
            else:
                self._append_text(href)
            return

        if tag_lower in self.BLOCK_TAGS:
            self._ensure_newline()

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0:
            return

        if self._current_href is not None:
            self._link_text_chunks.append(data)
        else:
            self._append_text(data)

    def _append_text(self, text: str) -> None:
        self._output_chunks.append(text)

    def _ensure_newline(self) -> None:
        if self._output_chunks and not self._output_chunks[-1].endswith("\n"):
            self._output_chunks.append("\n")

    @staticmethod
    def _is_tracking_pixel(attrs: dict[str, str]) -> bool:
        """Detect tracking pixels (1x1, 0x0, or hidden style)."""
        width = attrs.get("width", "").strip().lower()
        height = attrs.get("height", "").strip().lower()

        # Dimension checks
        for dim in (width, height):
            if dim in {"0", "1", "0px", "1px"}:
                return True

        # Style checks
        style = attrs.get("style", "").lower()
        if "display:none" in style.replace(" ", ""):
            return True
        if "visibility:hidden" in style.replace(" ", ""):
            return True
        return bool(re.search(r"width:\s*[01]px", style) or re.search(r"height:\s*[01]px", style))

    def get_text(self) -> str:
        """Produce the cleaned plain text."""
        raw = "".join(self._output_chunks)
        # Normalize whitespace per line while preserving paragraphs
        lines = [line.strip() for line in raw.split("\n")]
        text = "\n".join(lines)
        # Collapse > 2 consecutive newlines to 2
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html_content: str) -> str:
    """Convert HTML string into clean normalized plain text.

    Preserves hyperlinks as 'text (url)', strips tracking pixels,
    and strips script/style elements.
    """
    if not html_content or not html_content.strip():
        return ""
    parser = HTMLToTextConverter()
    parser.feed(html_content)
    parser.close()
    return parser.get_text()
