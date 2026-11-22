"""
Citation verification: is the quoted text really on the cited page?

A citation is a (page, quote) pair, from Claude, from a review report or from
the offline answerer. The quote is looked up in that page's text after
normalization (case, punctuation, line breaks and end-of-line hyphens ignored;
"..." marks an allowed gap). Possible outcomes:

- verified:  found on the cited page
- moved:     not on the cited page but on another one (the page is corrected)
- not_found: on no page (invented, paraphrased or altered: flag it)
- unchecked: the PDF has no text layer (a scan), so nothing can be checked

Each page can have several renderings (layout reading order and the raw
content-stream text); a quote matches a page if it matches any of them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pdfqa.text import normalize_quote


def _in_order(words: list[str], page_words: list[str], slack: float = 2.0) -> bool:
    """Every quote word appears on the page in order, within a window of slack x the quote length.

    This accepts text that extraction interleaved with other text (two columns read
    across, a footnote marker in the middle) but never a changed or missing word.
    """
    n = len(words)
    limit = int(slack * n) + 4
    for start, w in enumerate(page_words):
        if w != words[0]:
            continue
        i, j = 1, start + 1
        while i < n and j < len(page_words) and j - start < limit:
            if page_words[j] == words[i]:
                i += 1
            j += 1
        if i == n:
            return True
    return False


def found(quote: str, page_text: str) -> bool:
    """The quote is on the (already normalized) page; "..." marks an allowed gap.

    Accepted, in order of strictness:
    1. word for word;
    2. the same characters with spaces ignored (extraction split or merged words:
       "con figuration", "EUR4,800");
    3. for quotes of 6+ words, all words in order within a short window (columns
       or margin notes interleaved by extraction).
    A changed number, a substituted word or a paraphrase matches none of them.
    """
    parts = [normalize_quote(p) for p in re.split(r"\.\.\.|…", quote)]
    parts = [p for p in parts if p]
    if not parts:
        return False
    padded = f" {page_text} "
    if all(f" {p} " in padded for p in parts):
        return True
    squashed = page_text.replace(" ", "")
    if all(len(p) >= 12 and p.replace(" ", "") in squashed for p in parts):
        return True
    page_words = page_text.split()
    return all(len(p.split()) >= 6 and _in_order(p.split(), page_words) for p in parts)


class PageIndex:
    """Normalized page texts, built once per document and reused for every check."""

    def __init__(self, pages: Sequence[str | Sequence[str]]):
        self.pages = [[normalize_quote(t) for t in ([p] if isinstance(p, str) else p) if t]
                      for p in pages]
        self.has_text = any(any(v for v in variants) for variants in self.pages)

    def on_page(self, quote: str, page: int) -> bool:
        return 1 <= page <= len(self.pages) and any(found(quote, v) for v in self.pages[page - 1])

    def locate(self, quote: str) -> int | None:
        return next((n for n in range(1, len(self.pages) + 1) if self.on_page(quote, n)), None)

    def check(self, quote: str, page: int | None) -> tuple[str, int | None]:
        """(status, page): page is the corrected page for "moved"."""
        if not self.has_text or not quote or not quote.strip():
            return "unchecked", page
        if page and self.on_page(quote, page):
            return "verified", page
        actual = self.locate(quote)
        if actual:
            return "moved", actual
        return "not_found", page


def verify_items(items: list[dict], index: PageIndex, field: str = "quote") -> dict:
    """Set item["verified"] (and fix item["page"] when moved) for each item; return the counts."""
    counts = {"verified": 0, "moved": 0, "not_found": 0, "unchecked": 0}
    for item in items:
        status, page = index.check(item.get(field, ""), item.get("page") or 0)
        if status == "moved":
            item["page"] = page
        item["verified"] = status
        counts[status] += 1
    return counts
