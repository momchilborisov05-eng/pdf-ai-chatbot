"""
Offline extractive answering: no model, no network, deterministic.

1. Classify the question by the answer it expects: an amount of money, a
   percentage, a date, a year, a duration, a count or measurement, a name, or
   free text. Free text is answered with the whole supporting sentence.
2. Retrieve the top chunks (BM25F) and split them into candidate sentences;
   table rows are candidates as whole rows.
3. Score each candidate by how much of the question's IDF mass it covers
   (section headings count half, near-identical word forms count 0.8), plus the
   retrieval rank, plus a bonus when it contains an entity of the expected type.
   Question words that never occur in the document count with the median IDF:
   a question about "buses" in a rail report should not be answered from a
   sentence that only shares "operate".
4. Extract the short answer: the typed entity whose own clause best matches the
   question ("EUR 210 million is reserved for rolling stock and EUR 95 million
   for signalling" -> EUR 210 million for "rolling stock"), or, in a table row,
   the cell under the column the question names.
5. Abstain ("not in the document") when the best candidate covers too little
   of the question.

The quote returned is a literal span of the page, so it is checked against the
page text like any other citation before it is shown.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from pdfqa.index import BM25Index, Hit
from pdfqa.text import sentences, tokens

_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
          r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_CUR = r"(?:EUR|USD|BGN|GBP|CHF|\$|€|£)"
_SCALE = r"(?:\s?(?:million|billion|thousand|bn|m|k)\b)?"
_UNITS = (r"%|°C|kW|kWh|MW|W|V|A|Hz|bar|mm|cm|m|km|kg|g|t|l/min|m3/h|L|l|rpm|dB|percent|per cent|Nm|"
          r"seconds?|hours?|h|days?|stations?|units?|staff|employees|trains?|buses|seats?|people|times")
PATTERNS = {
    "money": re.compile(
        rf"{_CUR}\s?\d[\d,]*(?:\.\d+)?{_SCALE}|\d[\d,]*(?:\.\d+)?{_SCALE}\s?(?:{_CUR}|euros?|dollars?|leva)",
        re.I),
    "percent": re.compile(r"\d+(?:\.\d+)?\s?(?:%|percent\b|per cent\b)", re.I),
    "date": re.compile(
        rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}}\s{_MONTH}\.?\s\d{{4}}\b|\b{_MONTH}\.?\s\d{{1,2}},?\s\d{{4}}\b|"
        rf"\b\d{{1,2}}[./]\d{{1,2}}[./]\d{{4}}\b|\b{_MONTH}\s\d{{4}}\b", re.I),
    "year": re.compile(r"\b(?:19|20)\d{2}\b"),
    "duration": re.compile(
        r"\b\d[\d,]*(?:\.\d+)?\s?(?:working\s|business\s|calendar\s|operating\s)?"
        r"(?:days?|weeks?|months?|years?|hours?|minutes?|seconds?)\b", re.I),
    "number": re.compile(rf"(?<![\w.])\d[\d,]*(?:\.\d+)?(?:\s?(?:million|billion))?(?:\s?(?:{_UNITS})\b)?", re.I),
    # Proper names: two or more capitalized words, not at the start of a sentence
    # ("Northstar Travel", "Calder & Wynn LLP").
    "entity": re.compile(r"(?<![.!?:]\s)(?<!^)\b[A-Z][\w'-]*(?:\s(?:&\s|of\s)?[A-Z][\w'-]*)+"),
}

_RULES = [
    ("percent", r"\b(what|which) (percentage|percent|share|proportion)\b|\bpercentage\b|\bhow many percent|"
                r"\b(margin|turnover rate|growth rate|vat rate|tax rate)\b"),
    ("date", r"\bwhen\b|\b(what|which) (date|day)\b|\bdeadline\b|\bby when\b|\bdue date\b|\bdate of\b"),
    ("year", r"\b(what|which|in what) year\b"),
    ("duration", r"\bhow long\b|\bhow many (days|weeks|months|years|hours|minutes)\b|\bwithin how\b|"
                 r"\b(notice|warranty|retention|grace) period\b|\bhow often\b|\binterval\b"),
    ("money", r"\bhow much\b|\b(cost|price|fee|amount|budget|revenue|allowance|salary|spend|paid|value|"
              r"rate|capex|profit|ebitda|turnover|deposit|fine|penalty)\b"),
    ("number", r"\bhow many\b|\bnumber of\b|\b(maximum|minimum|max|min|rated|capacity|weight|pressure|"
               r"temperature|voltage|power|torque|flow|speed|length|size|height|depth|level|limit)\b"),
    ("entity", r"^\s*who\b|\b(which|what) (\w+ )?(company|supplier|firm|bank|authority|body|department|team|"
               r"auditor|agency|insurer|provider|vendor|organi[sz]ation)\b|\bname of\b"),
]

# When the expected type isn't in the best sentence, try a looser one ("by when" -> "in 2028").
_FALLBACK = {"money": ("number",), "date": ("year",), "duration": ("number",), "percent": ("number",)}

# Where one clause ends and the next begins, for attributing a value to its words.
_WINDOW = 8
_CLAUSE = re.compile(r",\s|;\s|\s(?:and|but|while|whereas)\s|\s(?=[A-Z][a-z]+(?:\s[a-z]+)?:\s)")


def classify(question: str) -> str:
    q = question.lower()
    for kind, pattern in _RULES:
        if re.search(pattern, q):
            return kind
    return "span"


@dataclass
class Answer:
    question: str
    answer: str                   # short answer, or "" when abstaining
    answer_type: str
    page: int | None
    quote: str                    # literal page text supporting the answer
    section: str
    confidence: float
    abstained: bool
    verified: str = "unchecked"   # set by the caller's citation check
    hits: list[Hit] = field(default_factory=list)


def _unseen_weight(index: BM25Index) -> float:
    cached = getattr(index, "_unseen_idf", None)
    if cached is None:
        cached = statistics.median(index.idf.values()) if index.idf else 1.0
        index._unseen_idf = cached  # type: ignore[attr-defined]
    return cached


def _weight(term: str, index: BM25Index) -> float:
    idf = index.term_idf(term)
    return idf if term in index.idf else _unseen_weight(index)


def _match(term: str, words: set[str]) -> float:
    if term in words:
        return 1.0
    # "approve" / "approval", "replace" / "replacement": same 6-letter prefix.
    if len(term) >= 6 and any(len(w) >= 6 and w[:6] == term[:6] for w in words):
        return 0.8
    return 0.0


def _coverage(q_terms: set[str], sentence: str, context: str, index: BM25Index) -> float:
    total = sum(_weight(t, index) for t in q_terms)
    if not total:
        return 0.0
    s = set(tokens(sentence))
    c = set(tokens(context))
    got = sum(_weight(t, index) * max(_match(t, s), 0.5 * _match(t, c)) for t in q_terms)
    return got / total


def _clause_bounds(sentence: str, start: int, end: int) -> tuple[str, str]:
    """The words of the clause before and after a match, cut at clause boundaries."""
    left = sentence[:start]
    cuts = list(_CLAUSE.finditer(left))
    if cuts:
        left = left[cuts[-1].end():]
    right = sentence[end:]
    cut = _CLAUSE.search(right)
    if cut:
        right = right[:cut.start()]
    return left, right


def _pick(matches: list[re.Match], sentence: str, q_terms: set[str], question: str,
          index: BM25Index | None) -> re.Match | None:
    """The match whose clause shares the most question words; ties go to the nearest one."""
    q_lower = question.lower()
    matches = [m for m in matches if m.group(0).strip().lower() not in q_lower]
    if not matches:
        return None
    anchors = [m.start() for m in re.finditer(r"[^\W_]+", sentence)
               if tokens(m.group(0)) and tokens(m.group(0))[0] in q_terms]

    def weight(t: str) -> float:
        return _weight(t, index) if index is not None else 1.0

    def score(m: re.Match) -> tuple[float, float]:
        left, right = _clause_bounds(sentence, m.start(), m.end())
        # The words next to the value: at most 8 on each side, within its clause.
        words = set(tokens(" ".join(left.split()[-_WINDOW:]))) | set(tokens(" ".join(right.split()[:_WINDOW])))
        clause = sum(weight(t) * _match(t, words) for t in q_terms)
        distance = min((abs(m.start() - a) for a in anchors), default=0)
        return (-clause, distance)

    return min(matches, key=score)


def _typed(answer_type: str, text: str) -> list[re.Match]:
    pattern = PATTERNS.get(answer_type)
    if not pattern:
        return []
    return [m for m in pattern.finditer(text) if m.group(0).strip()]


def _table_cell(hit: Hit, q_terms: set[str], question: str = "") -> tuple[str, float]:
    """The cell under the column the question names (best header-word overlap), else "".

    On a tie the column named earlier in the question wins: "the remedy for noise
    caused by cavitation" asks for Remedy, not Cause.
    """
    header, cells = hit.chunk.header, hit.chunk.cells
    if not header or len(header) != len(cells):
        return "", 0.0
    q_order = tokens(question)
    best, best_key, best_overlap = "", (0.0, 0.0), 0.0
    for i, (head, cell) in enumerate(zip(header, cells)):
        if i == 0:
            continue  # the row key column names the row, not the answer
        head_terms = set(tokens(head))
        overlap = sum(_match(t, q_terms) for t in head_terms) / max(len(head_terms), 1)
        if not overlap:
            continue
        first = min((q_order.index(t) for t in head_terms if t in q_order), default=len(q_order))
        key = (overlap, -first)
        if not best or key > best_key:
            best, best_key, best_overlap = cell, key, overlap
    return best, best_overlap


def answer(question: str, index: BM25Index, k: int = 6, min_coverage: float = 0.38) -> Answer:
    hits = index.search(question, k=k)
    answer_type = classify(question)
    q_terms = set(tokens(question))
    if not hits or not q_terms:
        return Answer(question, "", answer_type, None, "", "", 0.0, True, hits=hits)

    top = hits[0].score or 1.0
    best = None
    for hit in hits:
        units = [hit.chunk.text] if hit.chunk.kind == "table_row" else sentences(hit.chunk.text)
        for unit in units:
            cov = _coverage(q_terms, unit, hit.chunk.context, index)
            typed = bool(_typed(answer_type, unit)) if answer_type != "span" else False
            score = cov + 0.15 * (hit.score / top) + (0.25 if typed else 0.0)
            if hit.chunk.kind == "table_row":
                cell, head_overlap = _table_cell(hit, q_terms, question)
                if cell:
                    score += 0.1 * head_overlap
            if best is None or score > best[0]:
                best = (score, cov, unit, hit)

    score, cov, unit, hit = best
    if cov < min_coverage:
        return Answer(question, "", answer_type, None, "", "", round(cov, 3), True, hits=hits)

    short = ""
    if hit.chunk.kind == "table_row":
        short, _ = _table_cell(hit, q_terms, question)
        cells = hit.chunk.cells
        if not short and len(cells) >= 2 and set(tokens(cells[0])) & q_terms:
            # "Rated power | 2.2 | kW": the question names the row; the answer is the rest of it.
            short = " ".join(c for c in cells[1:] if c)
    if not short and answer_type != "span":
        m = None
        for kind in (answer_type, *_FALLBACK.get(answer_type, ())):
            m = _pick(_typed(kind, unit), unit, q_terms, question, index)
            if m is not None:
                break
        if m is not None:
            short = m.group(0).strip()
    if not short:
        short = unit
    return Answer(question, short, answer_type, hit.chunk.page, unit, hit.chunk.section,
                  round(min(1.0, cov), 3), False, hits=hits)
