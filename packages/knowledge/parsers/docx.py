"""DOCX document parser for knowledge RAG ingestion.

Requirements:
- R9.2: DOCX source support (.docx, application/vnd.openxmlformats-officedocument...).
- specs/tasks.md Task 3.1: Text plus document structure (headings, sections, lists).
- rag-implementation: Structured table handling and metadata attachment.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph

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

RE_HEADING_STYLE = re.compile(r"^Heading\s*(\d+)$", re.IGNORECASE)


class DOCXParser(DocumentParser):
    """Parser for DOCX documents (.docx).

    Traverses document body elements in document order, mapping Word styles
    (Heading 1..9, Title, List Bullet, List Number) and tables to structured elements.
    """

    supported_mime_types = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    }
    supported_extensions = {".docx"}

    def parse(
        self,
        content: bytes | str,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> ParsedDocument:
        """Parse DOCX byte content into structured elements."""
        docx_bytes = content.encode("latin-1") if isinstance(content, str) else content

        try:
            doc = Document(io.BytesIO(docx_bytes))
        except (PackageNotFoundError, BadZipFile, Exception) as err:
            raise DocumentParsingError(
                f"Failed to read DOCX document: {err}",
                details={"filename": filename, "error": str(err)},
            ) from err

        title: str | None = None
        doc_metadata: dict[str, Any] = {}
        if filename:
            doc_metadata["filename"] = filename

        # Read core properties
        if doc.core_properties:
            if doc.core_properties.title:
                title = str(doc.core_properties.title).strip()
            if doc.core_properties.author:
                doc_metadata["author"] = str(doc.core_properties.author)
            if doc.core_properties.created:
                doc_metadata["created_at"] = str(doc.core_properties.created)

        elements: list[DocumentElement] = []
        heading_stack = HeadingStack()

        def process_paragraph(p: Paragraph) -> None:
            nonlocal title
            text = p.text.strip()
            if not text:
                return

            style_name = p.style.name if p.style and p.style.name else ""

            # Check if style is a Heading
            h_match = RE_HEADING_STYLE.match(style_name)
            if h_match:
                level = min(int(h_match.group(1)), 6)
                h_path, section = heading_stack.push(level, text)
                if level == 1 and title is None:
                    title = text
                elements.append(
                    self.make_element(
                        element_type=ElementType.HEADING,
                        content=text,
                        level=level,
                        heading_path=h_path,
                        section=section,
                        metadata={"style": style_name},
                    )
                )
                return

            # Check if style is Title or Subtitle
            if style_name.lower() == "title":
                h_path, section = heading_stack.push(1, text)
                if title is None:
                    title = text
                elements.append(
                    self.make_element(
                        element_type=ElementType.HEADING,
                        content=text,
                        level=1,
                        heading_path=h_path,
                        section=section,
                        metadata={"style": style_name},
                    )
                )
                return

            if style_name.lower() == "subtitle":
                h_path, section = heading_stack.push(2, text)
                elements.append(
                    self.make_element(
                        element_type=ElementType.HEADING,
                        content=text,
                        level=2,
                        heading_path=h_path,
                        section=section,
                        metadata={"style": style_name},
                    )
                )
                return

            # Check if style is a List
            if "list" in style_name.lower():
                is_ordered = "number" in style_name.lower()
                elements.append(
                    self.make_element(
                        element_type=ElementType.LIST_ITEM,
                        content=text,
                        level=1,
                        heading_path=heading_stack.current_heading_path,
                        section=heading_stack.current_section,
                        metadata={
                            "list_type": "ordered" if is_ordered else "bullet",
                            "style": style_name,
                        },
                    )
                )
                return

            # Default paragraph
            elements.append(
                self.make_element(
                    element_type=ElementType.PARAGRAPH,
                    content=text,
                    heading_path=heading_stack.current_heading_path,
                    section=heading_stack.current_section,
                    metadata={"style": style_name} if style_name else {},
                )
            )

        def process_table(table: Table) -> None:
            if not table.rows:
                return

            table_matrix: list[list[str]] = []
            for row in table.rows:
                row_cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                table_matrix.append(row_cells)

            if not table_matrix:
                return

            headers = table_matrix[0]
            rows = table_matrix[1:]

            table_lines: list[str] = [
                "| " + " | ".join(headers) + " |",
                "| " + " | ".join(["---"] * len(headers)) + " |",
            ]
            for r in rows:
                table_lines.append("| " + " | ".join(r) + " |")

            formatted_table = "\n".join(table_lines)
            elements.append(
                self.make_element(
                    element_type=ElementType.TABLE,
                    content=formatted_table,
                    heading_path=heading_stack.current_heading_path,
                    section=heading_stack.current_section,
                    metadata={"table_data": {"headers": headers, "rows": rows}},
                )
            )

        # Traverse body in document order
        for child in doc.element.body:
            if child.tag.endswith("p"):
                process_paragraph(Paragraph(child, doc))
            elif child.tag.endswith("tbl"):
                process_table(Table(child, doc))

        if title is None and filename:
            title = Path(filename).stem.replace("_", " ").replace("-", " ").title()

        full_text = "\n\n".join(e.content for e in elements if e.content)

        return ParsedDocument(
            title=title,
            elements=elements,
            text=full_text,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            metadata=doc_metadata,
        )
