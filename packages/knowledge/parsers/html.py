"""HTML document parser for knowledge RAG ingestion.

Requirements:
- R9.2: HTML source support (.html, text/html).
- specs/tasks.md Task 3.1: Text plus document structure (headings, sections, lists).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from packages.domain.knowledge import (
    DocumentElement,
    ElementType,
    ParsedDocument,
)
from packages.knowledge.parsers.base import (
    DocumentParser,
    HeadingStack,
)

RE_HEADING_TAG = re.compile(r"^h([1-6])$")
NOISE_TAGS = {"script", "style", "noscript", "svg", "iframe", "link", "meta", "nav", "footer"}


class HTMLParser(DocumentParser):
    """Parser for HTML documents (.html, text/html).

    Strips noise elements (scripts, styles, navigation, footer) per rag-eval guidelines,
    and walks semantic tags (h1-h6, p, ul/ol/li, table, pre, blockquote) building
    structured elements with accurate heading_path breadcrumbs.
    """

    supported_mime_types = {"text/html", "application/xhtml+xml"}
    supported_extensions = {".html", ".htm", ".xhtml"}

    def parse(
        self,
        content: bytes | str,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> ParsedDocument:
        """Parse HTML content into structured elements."""
        raw_html = self.decode_content(content)
        soup = BeautifulSoup(raw_html, "html.parser")

        # Extract title from <title> if present
        title: str | None = None
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        # Remove noise elements
        for noise in soup.find_all(list(NOISE_TAGS)):
            noise.decompose()

        root = soup.body if soup.body else soup
        elements: list[DocumentElement] = []
        heading_stack = HeadingStack()

        def process_table(table_tag: Tag) -> DocumentElement | None:
            headers: list[str] = []
            rows: list[list[str]] = []

            for tr in table_tag.find_all("tr", recursive=False) or table_tag.find_all("tr"):
                # Header row
                th_cells = tr.find_all("th")
                if th_cells:
                    headers = [c.get_text(separator=" ", strip=True) for c in th_cells]
                    continue
                td_cells = tr.find_all("td")
                if td_cells:
                    rows.append([c.get_text(separator=" ", strip=True) for c in td_cells])

            if not headers and not rows:
                return None

            table_lines: list[str] = []
            if headers:
                table_lines.append("| " + " | ".join(headers) + " |")
                table_lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
            for row in rows:
                table_lines.append("| " + " | ".join(row) + " |")

            formatted_table = "\n".join(table_lines)
            return self.make_element(
                element_type=ElementType.TABLE,
                content=formatted_table,
                heading_path=heading_stack.current_heading_path,
                section=heading_stack.current_section,
                metadata={"table_data": {"headers": headers, "rows": rows}},
            )

        def walk_node(node: Tag | NavigableString, list_depth: int = 0) -> None:
            nonlocal title
            if isinstance(node, NavigableString):
                text = str(node).strip()
                if text:
                    elements.append(
                        self.make_element(
                            element_type=ElementType.PARAGRAPH,
                            content=text,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                        )
                    )
                return

            tag_name = node.name.lower()

            # Heading tags h1..h6
            h_match = RE_HEADING_TAG.match(tag_name)
            if h_match:
                level = int(h_match.group(1))
                h_text = node.get_text(separator=" ", strip=True)
                if h_text:
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
                return

            # Tables
            if tag_name == "table":
                tbl_elem = process_table(node)
                if tbl_elem:
                    elements.append(tbl_elem)
                return

            # Lists
            if tag_name in ("ul", "ol"):
                for child in node.children:
                    if isinstance(child, Tag) and child.name.lower() == "li":
                        li_text = child.get_text(separator=" ", strip=True)
                        if li_text:
                            elements.append(
                                self.make_element(
                                    element_type=ElementType.LIST_ITEM,
                                    content=li_text,
                                    level=list_depth + 1,
                                    heading_path=heading_stack.current_heading_path,
                                    section=heading_stack.current_section,
                                    metadata={
                                        "list_type": "ordered" if tag_name == "ol" else "bullet",
                                        "depth": list_depth + 1,
                                    },
                                )
                            )
                return

            # Code pre / code blocks
            if tag_name in ("pre", "code"):
                code_text = node.get_text(strip=True)
                if code_text:
                    elements.append(
                        self.make_element(
                            element_type=ElementType.CODE_BLOCK,
                            content=code_text,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                        )
                    )
                return

            # Blockquotes
            if tag_name == "blockquote":
                quote_text = node.get_text(separator=" ", strip=True)
                if quote_text:
                    elements.append(
                        self.make_element(
                            element_type=ElementType.BLOCKQUOTE,
                            content=quote_text,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                        )
                    )
                return

            # Paragraphs
            if tag_name == "p":
                p_text = node.get_text(separator=" ", strip=True)
                if p_text:
                    elements.append(
                        self.make_element(
                            element_type=ElementType.PARAGRAPH,
                            content=p_text,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                        )
                    )
                return

            # Container block tags: recurse into children
            for child in node.children:
                if isinstance(child, (Tag, NavigableString)):
                    walk_node(child, list_depth=list_depth)

        walk_node(root)

        if title is None and filename:
            title = Path(filename).stem.replace("_", " ").replace("-", " ").title()

        doc_metadata: dict[str, Any] = {}
        if filename:
            doc_metadata["filename"] = filename

        # Compute full clean text from elements
        clean_text = "\n\n".join(elem.content for elem in elements if elem.content)

        return ParsedDocument(
            title=title,
            elements=elements,
            text=clean_text,
            mime_type="text/html",
            metadata=doc_metadata,
        )
