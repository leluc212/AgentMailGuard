"""PDF document parser for knowledge RAG ingestion.

Requirements:
- R9.2: PDF source support (.pdf, application/pdf).
- specs/tasks.md Task 3.1: Text plus document structure (headings, sections, lists).
- rag-implementation: Page-aware extraction with rich metadata attachment.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from packages.domain.knowledge import (
    DocumentElement,
    ElementType,
    ParsedDocument,
)
from packages.knowledge.parsers.base import (
    DocumentParser,
    DocumentParsingError,
    HeadingStack,
)

RE_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\s+([A-Z][^\n]+)$")
RE_BULLET_LIST = re.compile(r"^(\s*)([-*•+])\s+(.+)$")
RE_NUMBERED_LIST = re.compile(r"^(\s*)(\d+[\.\)])\s+(.+)$")
RE_ALL_CAPS_HEADING = re.compile(r"^[A-Z0-9\s\-_:]{3,60}$")


class PDFParser(DocumentParser):
    """Parser for PDF documents (.pdf, application/pdf).

    Extracts page-by-page text with page_number metadata, derives structural
    elements (headings, lists, paragraphs) and integrates outline bookmarks.
    """

    supported_mime_types = {"application/pdf"}
    supported_extensions = {".pdf"}

    def parse(
        self,
        content: bytes | str,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> ParsedDocument:
        """Parse PDF byte content into structured elements."""
        pdf_bytes = content.encode("latin-1") if isinstance(content, str) else content

        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
        except (PdfReadError, Exception) as err:
            raise DocumentParsingError(
                f"Failed to read PDF document: {err}",
                details={"filename": filename, "error": str(err)},
            ) from err

        # Extract PDF metadata
        doc_metadata: dict[str, Any] = {
            "page_count": len(reader.pages),
        }
        if filename:
            doc_metadata["filename"] = filename

        title: str | None = None
        if reader.metadata:
            if reader.metadata.title:
                title = str(reader.metadata.title).strip()
            if reader.metadata.author:
                doc_metadata["author"] = str(reader.metadata.author)
            if reader.metadata.creation_date:
                doc_metadata["creation_date"] = str(reader.metadata.creation_date)

        # Extract bookmark outlines if present
        bookmarks: dict[str, int] = {}
        try:

            def extract_outlines(outline_list: list[Any], current_level: int = 1) -> None:
                for item in outline_list:
                    if isinstance(item, list):
                        extract_outlines(item, current_level + 1)
                    elif hasattr(item, "title"):
                        b_title = str(item.title).strip()
                        if b_title:
                            bookmarks[b_title.lower()] = current_level

            if reader.outline:
                extract_outlines(reader.outline)
        except Exception:
            # Resilient to outline parsing bugs in malformed PDFs
            pass

        elements: list[DocumentElement] = []
        heading_stack = HeadingStack()
        full_page_texts: list[str] = []

        def flush_paragraph_buf(buf: list[str], page_num: int) -> None:
            if buf:
                p_content = " ".join(line_item.strip() for line_item in buf).strip()
                if p_content:
                    elements.append(
                        self.make_element(
                            element_type=ElementType.PARAGRAPH,
                            content=p_content,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                            metadata={"page_number": page_num},
                        )
                    )
                buf.clear()

        for page_idx, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""

            full_page_texts.append(page_text)
            norm_page_text = page_text.replace("\r\n", "\n").replace("\r", "\n")
            lines = norm_page_text.split("\n")

            current_paragraph: list[str] = []

            for line in lines:
                stripped = line.strip()
                if not stripped:
                    flush_paragraph_buf(current_paragraph, page_idx)
                    continue

                lower_stripped = stripped.lower()

                # Check if line matches an outline bookmark
                if lower_stripped in bookmarks:
                    flush_paragraph_buf(current_paragraph, page_idx)
                    b_level = bookmarks[lower_stripped]
                    h_path, section = heading_stack.push(b_level, stripped)
                    if b_level == 1 and title is None:
                        title = stripped
                    elements.append(
                        self.make_element(
                            element_type=ElementType.HEADING,
                            content=stripped,
                            level=b_level,
                            heading_path=h_path,
                            section=section,
                            metadata={"page_number": page_idx, "is_bookmark": True},
                        )
                    )
                    continue

                # Check for numbered heading (e.g. "1.1 Architecture")
                num_match = RE_NUMBERED_HEADING.match(stripped)
                if num_match and len(stripped) < 80 and not stripped.endswith((".", ":", ";")):
                    flush_paragraph_buf(current_paragraph, page_idx)
                    parts = num_match.group(1).split(".")
                    level = min(len(parts), 6)
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
                            metadata={"page_number": page_idx},
                        )
                    )
                    continue

                # Check for all-caps short heading
                if (
                    RE_ALL_CAPS_HEADING.match(stripped)
                    and len(stripped.split()) <= 6
                    and not stripped.endswith((".", ",", ";"))
                ):
                    flush_paragraph_buf(current_paragraph, page_idx)
                    h_path, section = heading_stack.push(2, stripped)
                    elements.append(
                        self.make_element(
                            element_type=ElementType.HEADING,
                            content=stripped,
                            level=2,
                            heading_path=h_path,
                            section=section,
                            metadata={"page_number": page_idx},
                        )
                    )
                    continue

                # Check for bullet list item
                bullet_match = RE_BULLET_LIST.match(line)
                if bullet_match:
                    flush_paragraph_buf(current_paragraph, page_idx)
                    indent = len(bullet_match.group(1))
                    item_text = bullet_match.group(3).strip()
                    elements.append(
                        self.make_element(
                            element_type=ElementType.LIST_ITEM,
                            content=item_text,
                            level=(indent // 2) + 1,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                            metadata={"page_number": page_idx, "list_type": "bullet"},
                        )
                    )
                    continue

                # Check for numbered list item
                num_list_match = RE_NUMBERED_LIST.match(line)
                if num_list_match:
                    flush_paragraph_buf(current_paragraph, page_idx)
                    indent = len(num_list_match.group(1))
                    item_text = num_list_match.group(3).strip()
                    elements.append(
                        self.make_element(
                            element_type=ElementType.LIST_ITEM,
                            content=item_text,
                            level=(indent // 2) + 1,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                            metadata={"page_number": page_idx, "list_type": "ordered"},
                        )
                    )
                    continue

                current_paragraph.append(line)

            flush_paragraph_buf(current_paragraph, page_idx)

        if title is None and filename:
            title = Path(filename).stem.replace("_", " ").replace("-", " ").title()

        full_text = "\n\n".join(t for t in full_page_texts if t.strip())

        return ParsedDocument(
            title=title,
            elements=elements,
            text=full_text,
            mime_type="application/pdf",
            metadata=doc_metadata,
        )
