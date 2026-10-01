"""Plain text document parser for knowledge RAG ingestion.

Requirements:
- R9.2: Plain text source support.
- specs/tasks.md Task 3.1: Text plus document structure (headings, sections, lists).
"""

from __future__ import annotations

import re
from pathlib import Path

from packages.domain.knowledge import (
    DocumentElement,
    ElementType,
    ParsedDocument,
)
from packages.knowledge.parsers.base import (
    DocumentParser,
    HeadingStack,
)

# Regex patterns for structural inference
RE_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
RE_UNDERLINE_H1 = re.compile(r"^={3,}\s*$")
RE_UNDERLINE_H2 = re.compile(r"^-{3,}\s*$")
RE_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\s+([A-Z][^\n]+)$")
RE_BULLET_LIST = re.compile(r"^(\s*)([-*•+])\s+(.+)$")
RE_NUMBERED_LIST = re.compile(r"^(\s*)(\d+[\.\)])\s+(.+)$")


class PlainTextParser(DocumentParser):
    """Parser for plain text documents (.txt, text/plain).

    Extracts paragraphs, lists, and inferred headings while building a
    breadcrumb heading_path and clean linear text representation.
    """

    supported_mime_types = {"text/plain"}
    supported_extensions = {".txt", ".text"}

    def parse(
        self,
        content: bytes | str,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> ParsedDocument:
        """Parse plain text into structured elements."""
        raw_text = self.decode_content(content)
        # Normalize newlines
        normalized_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized_text.split("\n")

        elements: list[DocumentElement] = []
        heading_stack = HeadingStack()
        title: str | None = None
        current_paragraph: list[str] = []

        def flush_paragraph() -> None:
            if current_paragraph:
                p_text = " ".join(line.strip() for line in current_paragraph).strip()
                if p_text:
                    elements.append(
                        self.make_element(
                            element_type=ElementType.PARAGRAPH,
                            content=p_text,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                        )
                    )
                current_paragraph.clear()

        idx = 0
        total_lines = len(lines)

        while idx < total_lines:
            line = lines[idx]
            stripped = line.strip()

            if not stripped:
                flush_paragraph()
                idx += 1
                continue

            # Check for underline heading (Setext style: text followed by === or ---)
            if idx + 1 < total_lines:
                next_line = lines[idx + 1]
                if RE_UNDERLINE_H1.match(next_line):
                    flush_paragraph()
                    h_path, section = heading_stack.push(1, stripped)
                    if title is None:
                        title = stripped
                    elements.append(
                        self.make_element(
                            element_type=ElementType.HEADING,
                            content=stripped,
                            level=1,
                            heading_path=h_path,
                            section=section,
                        )
                    )
                    idx += 2
                    continue
                if RE_UNDERLINE_H2.match(next_line):
                    flush_paragraph()
                    h_path, section = heading_stack.push(2, stripped)
                    elements.append(
                        self.make_element(
                            element_type=ElementType.HEADING,
                            content=stripped,
                            level=2,
                            heading_path=h_path,
                            section=section,
                        )
                    )
                    idx += 2
                    continue

            # Check for Markdown-style heading (# Heading)
            md_match = RE_MD_HEADING.match(stripped)
            if md_match:
                flush_paragraph()
                level = len(md_match.group(1))
                h_text = md_match.group(2).strip()
                h_path, section = heading_stack.push(level, h_text)
                if level == 1 and title is None:
                    title = h_text
                elements.append(
                    self.make_element(
                        element_type=ElementType.HEADING,
                        content=h_text,
                        level=level,
                        heading_path=h_path,
                        section=section,
                    )
                )
                idx += 1
                continue

            # Check for numbered heading (e.g. 1.0 Overview, 2.1 Configuration)
            num_match = RE_NUMBERED_HEADING.match(stripped)
            if num_match and len(stripped) < 80 and not stripped.endswith((".", ":", ";")):
                # Determine level by numbering depth: 1 -> 1, 1.2 -> 2, 1.2.3 -> 3
                num_parts = num_match.group(1).split(".")
                level = min(len(num_parts), 6)
                flush_paragraph()
                h_path, section = heading_stack.push(level, stripped)
                if level == 1 and title is None:
                    title = stripped
                elements.append(
                    self.make_element(
                        element_type=ElementType.HEADING,
                        content=stripped,
                        level=level,
                        heading_path=h_path,
                        section=section,
                    )
                )
                idx += 1
                continue

            # Check for bullet list item
            bullet_match = RE_BULLET_LIST.match(line)
            if bullet_match:
                flush_paragraph()
                indent = len(bullet_match.group(1))
                list_level = (indent // 2) + 1
                item_content = bullet_match.group(3).strip()
                elements.append(
                    self.make_element(
                        element_type=ElementType.LIST_ITEM,
                        content=item_content,
                        level=list_level,
                        heading_path=heading_stack.current_heading_path,
                        section=heading_stack.current_section,
                        metadata={"list_type": "bullet", "indent": indent},
                    )
                )
                idx += 1
                continue

            # Check for numbered list item
            num_list_match = RE_NUMBERED_LIST.match(line)
            if num_list_match:
                flush_paragraph()
                indent = len(num_list_match.group(1))
                list_level = (indent // 2) + 1
                item_content = num_list_match.group(3).strip()
                elements.append(
                    self.make_element(
                        element_type=ElementType.LIST_ITEM,
                        content=item_content,
                        level=list_level,
                        heading_path=heading_stack.current_heading_path,
                        section=heading_stack.current_section,
                        metadata={
                            "list_type": "ordered",
                            "prefix": num_list_match.group(2),
                            "indent": indent,
                        },
                    )
                )
                idx += 1
                continue

            # Default: accumulate paragraph lines
            current_paragraph.append(line)
            idx += 1

        flush_paragraph()

        # Fallback title if none found
        if title is None and filename:
            title = Path(filename).stem.replace("_", " ").replace("-", " ").title()

        return ParsedDocument(
            title=title,
            elements=elements,
            text=normalized_text.strip(),
            mime_type="text/plain",
            metadata={"filename": filename} if filename else {},
        )
