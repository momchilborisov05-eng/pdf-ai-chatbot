"""
pdfqa: layout-aware PDF question answering with verified page citations, offline.

    qa = DocumentQA.from_pdf(pdf_bytes)
    result = qa.ask("What is the notice period?")
    result.answer, result.page, result.quote, result.verified

Pipeline: layout parsing (layout.py) -> structure-aware chunks (chunks.py) ->
BM25F retrieval (index.py) -> extractive answer (answer.py) -> citation check
against the page text (verify.py).
"""

from __future__ import annotations

import io

from pypdf import PdfReader

from pdfqa.answer import Answer, answer, classify
from pdfqa.chunks import Chunk, build_chunks
from pdfqa.index import BM25Index, Hit
from pdfqa.layout import ParsedDocument, from_page_texts, parse_pdf
from pdfqa.verify import PageIndex

__all__ = ["Answer", "BM25Index", "Chunk", "DocumentQA", "Hit", "PageIndex", "ParsedDocument",
           "build_chunks", "classify", "from_page_texts", "parse_pdf", "raw_page_texts"]


def raw_page_texts(data: bytes) -> list[str]:
    """pypdf's plain extraction (content-stream order), one string per page."""
    try:
        return [(p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages]
    except Exception:
        return []


class DocumentQA:
    def __init__(self, parsed: ParsedDocument, raw_pages: list[str] | None = None):
        self.parsed = parsed
        self.chunks = build_chunks(parsed)
        self.index = BM25Index(self.chunks)
        raw = raw_pages or []
        self.pages = PageIndex([
            [p.text, raw[i] if i < len(raw) else ""] for i, p in enumerate(parsed.pages)])

    @classmethod
    def from_pdf(cls, data: bytes) -> DocumentQA:
        return cls(parse_pdf(data), raw_page_texts(data))

    @classmethod
    def from_dict(cls, data: dict, raw_pages: list[str] | None = None) -> DocumentQA:
        return cls(ParsedDocument.from_dict(data), raw_pages)

    def search(self, question: str, k: int = 5) -> list[Hit]:
        return self.index.search(question, k)

    def ask(self, question: str, k: int = 6) -> Answer:
        result = answer(question, self.index, k=k)
        if not result.abstained:
            result.verified, page = self.pages.check(result.quote, result.page)
            result.page = page
        return result

    def outline(self) -> list[dict]:
        return [{"level": b.level, "title": b.text, "page": p.number}
                for p in self.parsed.pages for b in p.blocks if b.kind == "heading"]

    def structure(self) -> dict:
        """What the parser found, for the Inspect view: outline, tables, columns, furniture."""
        pages = []
        for p in self.parsed.pages:
            pages.append({
                "page": p.number,
                "columns": p.columns,
                "positioned": p.positioned,
                "furniture": p.furniture,
                "blocks": [{"kind": b.kind, "level": b.level, "column": b.column,
                            "text": b.text[:400], "rows": b.rows[:40]} for b in p.blocks],
            })
        return {"stats": {**self.parsed.stats(), "chunks": len(self.chunks),
                          "table_row_chunks": sum(c.kind == "table_row" for c in self.chunks)},
                "outline": self.outline(), "pages": pages}
