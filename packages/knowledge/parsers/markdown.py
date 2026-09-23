"""Markdown document parser for knowledge RAG ingestion.

Requirements:
- R9.2: Markdown source support (.md, text/markdown).
- specs/tasks.md Task 3.1: Text plus document structure (headings, sections, lists).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from packages.domain.knowledge import (
    DocumentElement,
    ElementType,
    ParsedDocument,
)
from packages.knowledge.parsers.base import (
    DocumentParser,
    HeadingStack,
)

RE_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
RE_YAML_TITLE = re.compile(r"^title:\s*['\"]?(.*?)['\"]?\s*$", re.MULTILINE | re.IGNORECASE)


class MarkdownParser(DocumentParser):
    """Parser for Markdown documents (.md, text/markdown).

    Uses a CommonMark token stream with tables enabled to extract headings (H1-H6),
    lists, code blocks, tables, and paragraphs with accurate breadcrumb heading_path.
    """

    supported_mime_types = {"text/markdown", "text/x-markdown"}
    supported_extensions = {".md", ".markdown"}

    def __init__(self) -> None:
        self._md = MarkdownIt("commonmark").enable("table")

    def parse(
        self,
        content: bytes | str,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> ParsedDocument:
        """Parse markdown content into structured elements."""
        raw_text = self.decode_content(content)
        normalized_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

        # Extract frontmatter if present
        title: str | None = None
        doc_metadata: dict[str, Any] = {}
        body_text = normalized_text

        fm_match = RE_FRONTMATTER.match(normalized_text)
        if fm_match:
            fm_content = fm_match.group(1)
            body_text = normalized_text[fm_match.end() :]
            doc_metadata["frontmatter"] = fm_content
            title_match = RE_YAML_TITLE.search(fm_content)
            if title_match:
                title = title_match.group(1).strip()

        tokens = self._md.parse(body_text)
        elements: list[DocumentElement] = []
        heading_stack = HeadingStack()

        i = 0
        num_tokens = len(tokens)
        list_depth = 0

        while i < num_tokens:
            token = tokens[i]

            # Headings
            if token.type == "heading_open":
                level = int(token.tag[1]) if len(token.tag) > 1 and token.tag[1].isdigit() else 1
                heading_content = ""
                i += 1
                while i < num_tokens and tokens[i].type != "heading_close":
                    if tokens[i].type == "inline":
                        heading_content += tokens[i].content
                    i += 1
                heading_content = heading_content.strip()
                h_path, section = heading_stack.push(level, heading_content)
                if level == 1 and title is None:
                    title = heading_content

                elements.append(
                    self.make_element(
                        element_type=ElementType.HEADING,
                        content=heading_content,
                        level=level,
                        heading_path=h_path,
                        section=section,
                    )
                )
                i += 1
                continue

            # Lists
            if token.type in ("bullet_list_open", "ordered_list_open"):
                list_depth += 1
                i += 1
                continue

            if token.type in ("bullet_list_close", "ordered_list_close"):
                list_depth = max(0, list_depth - 1)
                i += 1
                continue

            if token.type == "list_item_open":
                item_content = ""
                i += 1
                while i < num_tokens and tokens[i].type not in (
                    "list_item_close",
                    "bullet_list_open",
                    "ordered_list_open",
                ):
                    if tokens[i].type == "inline":
                        item_content += tokens[i].content + " "
                    i += 1
                item_content = item_content.strip()
                if item_content:
                    elements.append(
                        self.make_element(
                            element_type=ElementType.LIST_ITEM,
                            content=item_content,
                            level=list_depth,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                            metadata={"list_depth": list_depth},
                        )
                    )
                continue

            # Code fences / blocks
            if token.type in ("fence", "code_block"):
                code_content = token.content.strip()
                lang = token.info.strip() if token.info else None
                if code_content:
                    elements.append(
                        self.make_element(
                            element_type=ElementType.CODE_BLOCK,
                            content=code_content,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                            metadata={"language": lang} if lang else {},
                        )
                    )
                i += 1
                continue

            # Blockquotes
            if token.type == "blockquote_open":
                quote_content = ""
                i += 1
                while i < num_tokens and tokens[i].type != "blockquote_close":
                    if tokens[i].type == "inline":
                        quote_content += tokens[i].content + " "
                    i += 1
                quote_content = quote_content.strip()
                if quote_content:
                    elements.append(
                        self.make_element(
                            element_type=ElementType.BLOCKQUOTE,
                            content=quote_content,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                        )
                    )
                i += 1
                continue

            # Tables
            if token.type == "table_open":
                table_headers: list[str] = []
                table_rows: list[list[str]] = []
                current_row: list[str] = []
                in_thead = False

                i += 1
                while i < num_tokens and tokens[i].type != "table_close":
                    sub = tokens[i]
                    if sub.type == "thead_open":
                        in_thead = True
                    elif sub.type == "thead_close":
                        in_thead = False
                    elif sub.type == "tr_open":
                        current_row = []
                    elif sub.type == "tr_close":
                        if in_thead:
                            table_headers = current_row
                        else:
                            table_rows.append(current_row)
                    elif sub.type in ("th_open", "td_open"):
                        cell_text = ""
                        i += 1
                        while i < num_tokens and tokens[i].type not in ("th_close", "td_close"):
                            if tokens[i].type == "inline":
                                cell_text += tokens[i].content
                            i += 1
                        current_row.append(cell_text.strip())
                    i += 1

                # Format textual representation of table
                table_lines = []
                if table_headers:
                    table_lines.append("| " + " | ".join(table_headers) + " |")
                    table_lines.append("| " + " | ".join(["---"] * len(table_headers)) + " |")
                for row in table_rows:
                    table_lines.append("| " + " | ".join(row) + " |")
                formatted_table = "\n".join(table_lines)

                elements.append(
                    self.make_element(
                        element_type=ElementType.TABLE,
                        content=formatted_table,
                        heading_path=heading_stack.current_heading_path,
                        section=heading_stack.current_section,
                        metadata={
                            "table_data": {
                                "headers": table_headers,
                                "rows": table_rows,
                            }
                        },
                    )
                )
                i += 1
                continue

            # Paragraphs
            if token.type == "paragraph_open":
                if list_depth > 0:
                    i += 1
                    continue
                p_content = ""
                i += 1
                while i < num_tokens and tokens[i].type != "paragraph_close":
                    if tokens[i].type == "inline":
                        p_content += tokens[i].content
                    i += 1
                p_content = p_content.strip()
                if p_content:
                    elements.append(
                        self.make_element(
                            element_type=ElementType.PARAGRAPH,
                            content=p_content,
                            heading_path=heading_stack.current_heading_path,
                            section=heading_stack.current_section,
                        )
                    )
                i += 1
                continue

            i += 1

        if title is None and filename:
            title = Path(filename).stem.replace("_", " ").replace("-", " ").title()

        if filename:
            doc_metadata["filename"] = filename

        return ParsedDocument(
            title=title,
            elements=elements,
            text=normalized_text.strip(),
            mime_type="text/markdown",
            metadata=doc_metadata,
        )
