"""
Layout-aware PDF parsing: positioned text runs -> lines -> columns -> blocks.

A PDF stores text as drawing operations, not as paragraphs. The order of those
operations is up to the program that wrote the file, so a plain extractor can
interleave the lines of two columns, glue table cells into one string, repeat
the running header on every page and leave words broken by end-of-line hyphens.

This module rebuilds the reading order from geometry:

1. Runs: every text-show operation with its x start, x end, baseline and font
   (pypdf decodes the font encodings; positions come from its text-state machine).
2. Lines: runs on the same baseline, split into segments wherever the gap is too
   wide to be a word space (column gutters, table cell gaps).
3. Page furniture: lines in the top and bottom margins that repeat across pages
   once digits are masked ("Page 3 of 9" == "Page 7 of 9") are headers/footers.
4. Columns: a gutter is an x position that many prose lines end before and many
   others start after, with nothing crossing it. Between full-width lines, the
   left column is read before the right one.
5. Tables: runs of lines with 3+ aligned segments (or "a | b | c" text rows).
6. Blocks: headings (larger or bold short lines, or numbered "2.1 Title"),
   paragraphs (lines joined by spacing; end-of-line hyphens removed), tables.

The result keeps, per page, both the structured blocks and a reading-order text
that is used to verify quotes.
"""

from __future__ import annotations

import io
import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field

from pypdf import PdfReader

# ---------- data model ----------


@dataclass
class Run:
    text: str
    x0: float
    x1: float
    y: float
    size: float
    bold: bool = False


@dataclass
class Line:
    segments: list[list[Run]]
    y: float
    size: float

    @property
    def x0(self) -> float:
        return self.segments[0][0].x0

    @property
    def x1(self) -> float:
        return self.segments[-1][-1].x1

    @property
    def bold(self) -> bool:
        runs = [r for s in self.segments for r in s if r.text.strip()]
        return bool(runs) and all(r.bold for r in runs)

    def segment_texts(self) -> list[str]:
        return [_join_runs(s) for s in self.segments]

    @property
    def text(self) -> str:
        return " ".join(self.segment_texts())


@dataclass
class Block:
    kind: str                     # heading | paragraph | table
    text: str                     # reading-order text (tables: rows joined by " | ")
    page: int
    level: int = 0                # headings: 1 = top level
    rows: list[list[str]] = field(default_factory=list)   # tables only
    column: int = 0               # 0 = full width, 1 = left, 2 = right
    header: bool = False          # tables: the first row is set in bold


@dataclass
class ParsedPage:
    number: int
    width: float
    height: float
    blocks: list[Block]
    furniture: list[str]          # running headers / footers / page numbers removed from blocks
    columns: int                  # 1 or 2
    text: str                     # all text on the page in reading order (for quote checks)
    positioned: bool = True       # False when only plain text could be extracted


@dataclass
class ParsedDocument:
    pages: list[ParsedPage]
    body_size: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ParsedDocument:
        pages = [ParsedPage(**{**p, "blocks": [Block(**b) for b in p["blocks"]]}) for p in data["pages"]]
        return cls(pages=pages, body_size=data["body_size"])

    def stats(self) -> dict:
        blocks = [b for p in self.pages for b in p.blocks]
        return {
            "pages": len(self.pages),
            "headings": sum(b.kind == "heading" for b in blocks),
            "paragraphs": sum(b.kind == "paragraph" for b in blocks),
            "tables": sum(b.kind == "table" for b in blocks),
            "two_column_pages": sum(p.columns == 2 for p in self.pages),
            "furniture_lines": sum(len(p.furniture) for p in self.pages),
            "unpositioned_pages": sum(not p.positioned for p in self.pages),
        }


# ---------- step 1: positioned runs (via pypdf's text-state machine) ----------

def _page_runs(page) -> list[Run] | None:
    """Every text-show operation on the page with its geometry, or None if unavailable.

    pypdf's public visitor API merges all operations on a line into one string, which
    loses the gaps between table cells and columns; its layout-mode internals keep one
    record per operation, so those are used (and guarded, since they are private).
    """
    try:
        from pypdf._text_extraction import _layout_mode as lm
        from pypdf._text_extraction._layout_mode._fixed_width_page import recurse_to_target_op
        from pypdf._text_extraction._layout_mode._text_state_manager import TextStateManager
        from pypdf.generic import ContentStream
    except ImportError:  # pragma: no cover - a future pypdf moved them
        return None
    try:
        contents = page.get("/Contents")
        if contents is None:
            return []
        fonts = page._layout_mode_fonts()
        ops = iter(ContentStream(contents.get_object(), page.pdf, "bytes").operations)
        state = TextStateManager()
        tj_ops = []
        for operands, op in ops:
            if op in (b"BT", b"q"):
                _, tjs = recurse_to_target_op(ops, state, b"ET" if op == b"BT" else b"Q", fonts, True)
                tj_ops.extend(tjs)
            elif op == b"Tf":
                lm.set_font_operation(state, fonts, operands)
            else:
                state.set_state_param(op, operands)
    except Exception:
        return None
    runs = []
    for tj in tj_ops:
        if tj.rotated or not tj.font.interpretable or not tj.text.strip():
            continue
        name = getattr(tj.font, "name", "") or ""
        runs.append(Run(text=tj.text.replace("\x00", ""), x0=float(tj.tx), x1=float(tj.displaced_tx),
                        y=float(tj.ty), size=round(float(tj.font_height), 2),
                        bold=bool(re.search(r"bold|black|heavy|semibold|demi", name, re.I))))
    return runs


# ---------- step 2: lines and segments ----------

def _join_runs(runs: list[Run]) -> str:
    out = ""
    prev: Run | None = None
    for run in runs:
        text = run.text
        if prev is not None:
            gap = run.x0 - prev.x1
            if gap > 0.15 * run.size and not out.endswith(" ") and not text.startswith(" "):
                out += " "
        out += text
        prev = run
    return re.sub(r"\s+", " ", out).strip()


def _lines(runs: list[Run]) -> list[Line]:
    runs = sorted(runs, key=lambda r: (-r.y, r.x0))
    rows: list[list[Run]] = []
    for run in runs:
        if rows and abs(rows[-1][0].y - run.y) <= 0.45 * min(run.size, rows[-1][0].size):
            rows[-1].append(run)
        else:
            rows.append([run])
    lines = []
    for row in rows:
        row.sort(key=lambda r: r.x0)
        segments: list[list[Run]] = [[row[0]]]
        for run in row[1:]:
            prev = segments[-1][-1]
            gap = run.x0 - prev.x1
            # A word space is ~0.28 em; justified text can stretch it. 1.6 em is a cell/column gap.
            if gap > 1.6 * max(run.size, prev.size):
                segments.append([run])
            else:
                segments[-1].append(run)
        sizes = [r.size for r in row for _ in r.text]
        lines.append(Line(segments=segments, y=statistics.mean(r.y for r in row),
                          size=statistics.median(sizes) if sizes else row[0].size))
    return lines


# ---------- step 3: running headers and footers ----------

_PAGE_NUMBER = re.compile(r"^\W*(page|p\.|seite|стр\.?|страница)?\s*\d+\s*((of|/|от)\s*\d+)?\W*$", re.I)


def _furniture_key(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower()).strip()


def _find_furniture(pages: list[tuple[float, list[Line]]]) -> list[set[int]]:
    """Indexes of header/footer lines per page."""
    margin_keys = Counter()
    candidates = []
    for height, lines in pages:
        cands = {}
        for i, line in enumerate(lines):
            if line.y > height * 0.92 or line.y < height * 0.08:
                cands[i] = _furniture_key(line.text)
        candidates.append(cands)
        margin_keys.update(set(cands.values()))
    need = max(2, int(0.4 * len(pages) + 0.5))
    out = []
    for (_, lines), cands in zip(pages, candidates):
        out.append({i for i, key in cands.items()
                    if (len(pages) > 1 and margin_keys[key] >= need) or _PAGE_NUMBER.match(lines[i].text)})
    return out


# ---------- step 4: columns ----------

def _find_gutter(lines: list[Line], width: float) -> float | None:
    """The x position of a two-column gutter, or None for a single-column page."""
    prose = [ln for ln in lines if len(ln.segments) <= 2]
    if len(prose) < 6:
        return None
    best, best_score = None, 0
    x = width * 0.3
    while x <= width * 0.7:
        left = right = both = crossing = 0
        for ln in prose:
            crosses = any(s[0].x0 < x - 1 and s[-1].x1 > x + 1 for s in ln.segments)
            if crosses:
                crossing += 1
                continue
            has_left = any(s[-1].x1 <= x for s in ln.segments)
            has_right = any(s[0].x0 >= x for s in ln.segments)
            left += has_left
            right += has_right
            both += has_left and has_right
        # Both columns need real prose; few lines may cross (full-width headings, titles).
        score = min(left, right)
        if score > best_score and both >= 3 and crossing <= 0.25 * len(prose):
            best, best_score = x, score
        x += 3
    if best is None or best_score < 0.25 * len(prose):
        return None
    # Columns of prose are filled to their width; a two-column table ("Task | Interval") is not.
    x0 = min(ln.x0 for ln in prose)
    x1 = max(ln.x1 for ln in prose)
    fills = []
    for ln in prose:
        lsegs = [s for s in ln.segments if s[-1].x1 <= best]
        rsegs = [s for s in ln.segments if s[0].x0 >= best]
        if lsegs and rsegs:
            left_fill = (lsegs[-1][-1].x1 - x0) / max(best - x0, 1)
            right_fill = (rsegs[-1][-1].x1 - rsegs[0][0].x0) / max(x1 - rsegs[0][0].x0, 1)
            fills.append(min(left_fill, right_fill))
    if fills and statistics.median(fills) < 0.6:
        return None
    return best


def _order_columns(lines: list[Line], gutter: float | None) -> list[tuple[Line, int]]:
    """Reading order: full-width lines in place; between them, the left column then the right."""
    if gutter is None:
        return [(ln, 0) for ln in lines]
    out: list[tuple[Line, int]] = []
    left: list[Line] = []
    right: list[Line] = []

    def flush():
        out.extend((ln, 1) for ln in left)
        out.extend((ln, 2) for ln in right)
        left.clear()
        right.clear()

    for ln in lines:
        crosses = any(s[0].x0 < gutter - 1 and s[-1].x1 > gutter + 1 for s in ln.segments)
        if crosses or len(ln.segments) >= 3:
            flush()
            out.append((ln, 0))
            continue
        lsegs = [s for s in ln.segments if s[-1].x1 <= gutter]
        rsegs = [s for s in ln.segments if s[0].x0 >= gutter]
        if lsegs:
            left.append(Line(lsegs, ln.y, ln.size))
        if rsegs:
            right.append(Line(rsegs, ln.y, ln.size))
    flush()
    return out


# ---------- steps 5 and 6: tables, headings, paragraphs ----------

_NUMBERED = re.compile(r"^(\d+(?:\.\d+){0,3})\.?\s+[A-ZЀ-Я][^.!?]{2,90}$")
_BULLET = re.compile(r"^([•●▪\-\*]|\(?[a-z0-9]{1,2}[.)]|\d+(?:\.\d+)+\.?)\s+")


def _is_table_row(line: Line) -> bool:
    return len(line.segments) >= 3 or line.text.count(" | ") >= 2


def _two_cell_run(ordered: list[tuple[Line, int]], i: int) -> int:
    """Length of a run of 3+ two-cell lines whose second cells start at the same x (a 2-column table)."""
    first = ordered[i][0]
    if len(first.segments) != 2:
        return 0
    x = first.segments[1][0].x0
    j = i
    while (j < len(ordered) and len(ordered[j][0].segments) == 2 and ordered[j][1] == ordered[i][1]
           and abs(ordered[j][0].segments[1][0].x0 - x) <= 3
           and (j == i or ordered[j - 1][0].y - ordered[j][0].y <= 2.6 * ordered[j][0].size)):
        j += 1
    return j - i if j - i >= 3 else 0


def _cells(line: Line) -> list[str]:
    if len(line.segments) == 1 and " | " in line.text:
        return [c.strip() for c in line.text.split(" | ")]
    return line.segment_texts()


def _join_lines(parts: list[str]) -> str:
    text = ""
    for part in parts:
        if not text:
            text = part
        elif re.search(r"[A-Za-zЀ-ӿ]-$", text) and re.match(r"[a-zа-я]", part):
            text = text[:-1] + part          # "con-" + "figuration" -> "configuration"
        else:
            text += " " + part
    return text


def _heading_level(line: Line, body: float, heading_sizes: list[float]) -> int:
    """0 if the line is not a heading, else its level (1 = largest)."""
    text = line.text.strip()
    words = len(text.split())
    if not text or words > 16 or text.endswith((".", ",", ";", ":")) and not _NUMBERED.match(text):
        return 0
    numbered = _NUMBERED.match(text)
    if line.size >= body * 1.15:
        level = 1 + next((i for i, s in enumerate(heading_sizes) if line.size >= s - 0.3), len(heading_sizes))
        return level
    if line.bold and words <= 12 and not text.endswith("."):
        return len(heading_sizes) + 1
    if numbered and words <= 10 and not text.endswith("."):
        return numbered[1].count(".") + 1
    return 0


def _blocks(ordered: list[tuple[Line, int]], page_no: int, body: float,
            heading_sizes: list[float]) -> list[Block]:
    blocks: list[Block] = []
    i = 0
    while i < len(ordered):
        line, col = ordered[i]
        # Tables: 2+ consecutive rows with 3+ cells (or pipe-delimited rows), or 3+ aligned 2-cell rows.
        run = _two_cell_run(ordered, i)
        if run or _is_table_row(line):
            if run:
                j = i + run
                rows = [_cells(ordered[k][0]) for k in range(i, j)]
            else:
                j = i
                rows = []
                while j < len(ordered) and _is_table_row(ordered[j][0]) and (
                        j == i or ordered[j - 1][0].y - ordered[j][0].y <= 2.6 * ordered[j][0].size):
                    rows.append(_cells(ordered[j][0]))
                    j += 1
            if len(rows) >= 2:
                bold_header = ordered[i][0].bold and not ordered[i + 1][0].bold
                blocks.append(Block("table", "\n".join(" | ".join(r) for r in rows), page_no,
                                    rows=rows, column=col, header=bold_header))
                i = j
                continue
        level = _heading_level(line, body, heading_sizes)
        if level:
            parts = [line.text]
            # Headings that wrap: following lines in the same size and weight.
            while (i + 1 < len(ordered) and ordered[i + 1][1] == col
                   and abs(ordered[i + 1][0].size - line.size) < 0.3 and ordered[i + 1][0].bold == line.bold
                   and line.size >= body * 1.15 and line.y - ordered[i + 1][0].y <= 1.6 * line.size):
                i += 1
                parts.append(ordered[i][0].text)
            blocks.append(Block("heading", " ".join(parts), page_no, level=level, column=col))
            i += 1
            continue
        parts = [line.text]
        prev = line
        i += 1
        while i < len(ordered):
            nxt, ncol = ordered[i]
            gap = prev.y - nxt.y
            if (ncol != col or _is_table_row(nxt) or _heading_level(nxt, body, heading_sizes)
                    or abs(nxt.size - prev.size) > 0.6 or gap > 1.75 * prev.size or gap < 0
                    or _BULLET.match(nxt.text)):
                break
            parts.append(nxt.text)
            prev = nxt
            i += 1
        blocks.append(Block("paragraph", _join_lines(parts), page_no, column=col))
    return blocks


# ---------- entry points ----------

def parse_pdf(data: bytes) -> ParsedDocument:
    """Parse a PDF into pages of blocks. Raises pypdf errors for unreadable files."""
    reader = PdfReader(io.BytesIO(data))
    raw: list[tuple[float, float, list[Line] | None, str]] = []
    for page in reader.pages:
        box = page.mediabox
        width, height = float(box.width), float(box.height)
        runs = _page_runs(page)
        if runs is None:
            raw.append((width, height, None, page.extract_text() or ""))
        else:
            raw.append((width, height, _lines(runs), ""))

    sizes = Counter()
    for _, _, lines, _ in raw:
        for ln in lines or []:
            sizes[round(ln.size, 1)] += len(ln.text)
    body = sizes.most_common(1)[0][0] if sizes else 10.0
    heading_sizes = sorted({s for s in sizes if s >= body * 1.15}, reverse=True)

    positioned = [(h, lines) for _, h, lines, _ in raw if lines is not None]
    furniture = iter(_find_furniture(positioned))
    pages = []
    for number, (width, height, lines, plain) in enumerate(raw, start=1):
        if lines is None:
            blocks = [Block("paragraph", p.strip(), number) for p in re.split(r"\n\s*\n", plain) if p.strip()]
            pages.append(ParsedPage(number, width, height, blocks, [], 1, plain, positioned=False))
            continue
        skip = next(furniture)
        content = [ln for i, ln in enumerate(lines) if i not in skip]
        gutter = _find_gutter(content, width)
        ordered = _order_columns(content, gutter)
        blocks = _blocks(ordered, number, body, heading_sizes)
        top = [lines[i].text for i in sorted(skip) if lines[i].y > height / 2]
        bottom = [lines[i].text for i in sorted(skip) if lines[i].y <= height / 2]
        text = "\n".join([*top, *(b.text for b in blocks), *bottom])
        pages.append(ParsedPage(number, width, height, blocks, top + bottom,
                                2 if gutter is not None else 1, text))
    return ParsedDocument(pages=pages, body_size=body)


def from_page_texts(pages: list[str]) -> ParsedDocument:
    """A layout-less document from plain page texts (documents stored before layouts were kept).

    No positions, so no columns, tables or furniture: each blank-line or line-separated
    paragraph becomes a block. Retrieval and answers still work, with less structure.
    """
    parsed = []
    for number, plain in enumerate(pages, start=1):
        parts = re.split(r"\n\s*\n", plain) if "\n\n" in plain else plain.splitlines()
        blocks = [Block("paragraph", p.strip(), number) for p in parts if p.strip()]
        parsed.append(ParsedPage(number, 0.0, 0.0, blocks, [], 1, plain, positioned=False))
    return ParsedDocument(pages=parsed, body_size=10.0)
