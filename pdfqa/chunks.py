"""
Structure-aware chunking.

Chunks follow the document, not a character count:

- A chunk never crosses a page, so every retrieved passage has one exact page
  number to cite.
- A chunk never crosses a heading. It carries its section path ("2 Finance >
  2.1 Revenue"), which is indexed as a separate, lower-weighted field: a
  question about "maintenance intervals" finds the paragraph under that
  heading even when the paragraph never repeats those words.
- Each table row is its own chunk, with the table's header as context, so a
  question naming a row and a column finds that exact row.
- Paragraphs are packed up to ~90 words and long paragraphs are split on
  sentence boundaries, so a chunk is always a literal span of the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pdfqa.layout import ParsedDocument
from pdfqa.text import sentences

MAX_WORDS = 90


@dataclass
class Chunk:
    id: int
    page: int
    kind: str                    # text | table_row
    text: str                    # literal text from the page
    section: str = ""            # heading path, " > " separated
    header: list[str] = field(default_factory=list)   # table header cells (table rows only)
    cells: list[str] = field(default_factory=list)    # table row cells (table rows only)

    @property
    def context(self) -> str:
        return " ".join([self.section, *self.header])


def _has_header(rows: list[list[str]], bold_first: bool = False) -> bool:
    """The first row is a header if it is set in bold, or it has no digits and later rows do."""
    if len(rows) < 2:
        return False
    if bold_first:
        return True
    first_has_digits = any(re.search(r"\d", c) for c in rows[0])
    rest_has_digits = any(re.search(r"\d", c) for row in rows[1:] for c in row)
    return not first_has_digits and rest_has_digits


def build_chunks(doc: ParsedDocument, max_words: int = MAX_WORDS) -> list[Chunk]:
    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []

    def section() -> str:
        return " > ".join(title for _, title in stack)

    def add(page: int, kind: str, text: str, **extra):
        chunks.append(Chunk(len(chunks), page, kind, text, section(), **extra))

    for page in doc.pages:
        buffer: list[str] = []
        words = 0

        def flush(page_no=page.number):
            nonlocal buffer, words
            if buffer:
                add(page_no, "text", " ".join(buffer))
            buffer, words = [], 0

        for block in page.blocks:
            if block.kind == "heading":
                flush()
                while stack and stack[-1][0] >= block.level:
                    stack.pop()
                stack.append((block.level, block.text))
            elif block.kind == "table":
                flush()
                rows = block.rows
                header = rows[0] if _has_header(rows, block.header) else []
                for row in rows[1:] if header else rows:
                    add(page.number, "table_row", " | ".join(row), header=list(header), cells=list(row))
            else:
                for sentence in sentences(block.text) or [block.text]:
                    n = len(sentence.split())
                    if buffer and words + n > max_words:
                        flush()
                    buffer.append(sentence)
                    words += n
                # Paragraph boundaries are good chunk boundaries once a chunk has some substance.
                if words >= max_words * 0.5:
                    flush()
        flush()
    return chunks
