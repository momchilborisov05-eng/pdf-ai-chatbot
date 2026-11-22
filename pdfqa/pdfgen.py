"""
A tiny PDF writer with positioned text (standard library only).

Used to build the evaluation corpus and the test fixtures: two-column pages,
tables drawn cell by cell, running headers and footers, headings in a larger
bold font, and words hyphenated at line ends. Those are exactly the layout
features a plain text extractor gets wrong, so the parser can be measured on them.

Text uses the standard Helvetica / Helvetica-Bold fonts (WinAnsi), whose widths
are built into every PDF reader, so nothing is embedded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Helvetica advance widths (1/1000 em) for ASCII 32..126, from the standard AFM metrics.
_HELVETICA = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]

PAGE_W, PAGE_H = 595.0, 842.0


def text_width(text: str, size: float, bold: bool = False) -> float:
    """Rendered width in points (bold is approximated as 5% wider)."""
    units = sum(_HELVETICA[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in text)
    return units * size / 1000 * (1.05 if bold else 1.0)


def wrap(text: str, width: float, size: float, bold: bool = False, hyphenate: bool = True) -> list[str]:
    """Greedy line breaking; long words that don't fit are hyphenated (like a typesetter would)."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if text_width(candidate, size, bold) <= width:
            line = candidate
            continue
        if hyphenate and len(word) >= 8 and word.isalpha() and line:
            # Put as much of the word as fits on this line, keeping at least 3 letters on each side.
            for cut in range(len(word) - 3, 2, -1):
                head = f"{line} {word[:cut]}-"
                if text_width(head, size, bold) <= width:
                    lines.append(head)
                    line = word[cut:]
                    break
            else:
                lines.append(line)
                line = word
            continue
        if line:
            lines.append(line)
        line = word
    if line:
        lines.append(line)
    return lines


@dataclass
class _Op:
    x: float
    y: float
    text: str
    size: float
    bold: bool


@dataclass
class Page:
    ops: list[_Op] = field(default_factory=list)

    def text(self, x: float, y: float, text: str, size: float = 10, bold: bool = False):
        self.ops.append(_Op(x, y, text, size, bold))


class Canvas:
    """Collects pages of positioned text and writes a PDF.

    order="stream" writes text in the order it was drawn. order="rows" sorts it top to
    bottom, left to right, which interleaves the lines of side-by-side columns, as many
    real PDF producers do, and which defeats extractors that trust the content stream.
    """

    def __init__(self, order: str = "stream"):
        self.pages: list[Page] = []
        self.order = order

    def new_page(self) -> Page:
        self.pages.append(Page())
        return self.pages[-1]

    def to_bytes(self) -> bytes:
        objects: list[bytes | str | None] = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        ]
        kids = []
        for page in self.pages:
            ops = page.ops
            if self.order == "rows":
                ops = sorted(ops, key=lambda o: (-round(o.y, 1), o.x))
            body = "".join(
                f"BT /{'F2' if o.bold else 'F1'} {o.size:g} Tf 1 0 0 1 {o.x:.2f} {o.y:.2f} Tm "
                f"({_escape(o.text)}) Tj ET\n" for o in ops)
            stream = body.encode("cp1252", errors="replace")
            objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
            kids.append(len(objects) + 1)
            objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:g} {PAGE_H:g}] "
                           f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {len(objects)} 0 R >>")
        objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"

        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for number, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            data = obj if isinstance(obj, bytes) else str(obj).encode("latin-1")
            out += b"%d 0 obj\n" % number + data + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
        return bytes(out)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


# ---------- a small document layout engine on top of the canvas ----------

@dataclass
class Style:
    body: float = 10.0
    leading: float = 1.3
    h1: float = 16.0
    h2: float = 12.5
    margin: float = 56.0
    top: float = 770.0
    bottom: float = 70.0
    gutter: float = 22.0


def render(pages: list[list[tuple]], *, columns: int = 1, header: str | None = None,
           footer: str | None = "Page {n} of {total}", order: str = "stream",
           style: Style | None = None) -> bytes:
    """Lay out a document: one list of elements per page, never flowing between pages.

    Elements: ("h1", text), ("h2", text), ("p", text), ("table", [[cells], ...]) and
    ("full", element) to put an element across all columns (tables always are), and
    ("colbreak",) to continue in the next column. In a
    multi-column layout, text fills the first column, then the next; a full-width element
    starts below all columns. Raises if a page overflows, so ground-truth
    page numbers in the evaluation set can never drift.
    """
    st = style or Style()
    canvas = Canvas(order=order)
    total = len(pages)
    usable = PAGE_W - 2 * st.margin
    col_w = (usable - st.gutter * (columns - 1)) / columns

    for n, elements in enumerate(pages, start=1):
        page = canvas.new_page()
        if header:
            page.text(st.margin, PAGE_H - 36, header, 8.5)
        if footer:
            label = footer.format(n=n, total=total)
            page.text(PAGE_W - st.margin - text_width(label, 8.5), 34, label, 8.5)

        col = 0
        ys = [st.top] * columns

        for element in elements:
            if element[0] == "colbreak":
                col = min(col + 1, columns - 1)
                continue
            full = element[0] == "full" or columns == 1
            kind, payload = element[1] if element[0] == "full" else element
            if element[0] == "full" or kind == "table":
                full = True
            if full:
                y = min(ys)
                col = 0
            else:
                y = ys[col]
            width = usable if full else col_w
            if kind in ("h1", "h2"):
                size = st.h1 if kind == "h1" else st.h2
                lines = wrap(payload, width, size, bold=True, hyphenate=False)
                need = len(lines) * size * 1.25 + size * 0.6
            elif kind == "p":
                size = st.body
                lines = wrap(payload, width, size)
                need = len(lines) * size * st.leading + size * 0.7
            elif kind == "table":
                size = st.body - 0.5
                need = len(payload) * size * 1.55 + size
            else:
                raise ValueError(f"unknown element {kind}")

            if y - need < st.bottom:
                if not full and col < columns - 1:
                    col += 1
                    y = ys[col]
                if y - need < st.bottom:
                    raise ValueError(f"page {n} overflows at: {str(payload)[:60]}")
            x = st.margin if full else st.margin + col * (col_w + st.gutter)

            if kind in ("h1", "h2"):
                y -= size * 0.3
                for line in lines:
                    y -= size * 1.25
                    page.text(x, y, line, size, bold=True)
                y -= size * 0.3
            elif kind == "p":
                for line in lines:
                    y -= size * st.leading
                    page.text(x, y, line, size)
                y -= size * 0.7
            else:
                _table(page, payload, x, y, width, size)
                y -= need
            if full:
                ys = [y] * columns
            else:
                ys[col] = y
    return canvas.to_bytes()


def _table(page: Page, rows: list[list[str]], x: float, y: float, width: float, size: float):
    """Cells drawn one by one at fixed column positions; the first row is bold (the header)."""
    ncols = max(len(r) for r in rows)
    widths = [max(text_width(str(r[i]) if i < len(r) else "", size, True) for r in rows) for i in range(ncols)]
    spare = max(0.0, width - sum(widths))
    gap = spare / max(ncols - 1, 1) if ncols > 1 else 0
    gap = min(gap, 60.0)
    starts, cx = [], x
    for w in widths:
        starts.append(cx)
        cx += w + gap
    for r, row in enumerate(rows):
        y -= size * 1.55
        for i, cell in enumerate(row):
            page.text(starts[i], y, str(cell), size, bold=(r == 0))
