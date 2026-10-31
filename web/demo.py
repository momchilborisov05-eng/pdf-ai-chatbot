"""
Demo mode: a stand-in for the Claude client, so the web app can be tried
without an API key (DEMO_MODE=1). Answers are canned, but they quote real text
from the uploaded PDF with real page numbers, so citations, history, search
and cost tracking all work exactly as with the real API.

Reviews are built with simple keyword rules instead of a model: they quote
real sentences, so quote verification, invoice arithmetic checks and the
exports can be tried too. Claude's real reviews are far more thorough.
"""

import base64
import html
import io
import json
import re
import time
import uuid

from anthropic.types.beta import BetaMessage
from pypdf import PdfReader

DEMO_MODEL = "demo"


class _Files:
    def __init__(self):
        self.pages: dict[str, list[str]] = {}

    def upload(self, file):
        _, data, _ = file
        file_id = f"file_demo_{uuid.uuid4().hex[:12]}"
        self.pages[file_id] = _page_texts(data)
        return type("FileMetadata", (), {"id": file_id, "size_bytes": len(data)})()

    def delete(self, file_id):
        self.pages.pop(file_id, None)


class _Stream:
    def __init__(self, message: BetaMessage, text: str):
        self.message = message
        # Word by word, so the browser shows a real streaming answer.
        self.text_stream = self._words(text)

    @staticmethod
    def _words(text):
        for i, word in enumerate(text.split(" ")):
            time.sleep(0.02)
            yield word if i == 0 else " " + word

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class DemoClient:
    def __init__(self):
        self.files = _Files()
        self.beta = self
        self.messages = self

    def stream(self, *, messages, output_config=None, **_):
        content = messages[0]["content"]
        document = next(b for b in content if b["type"] == "document")["source"]
        if document["type"] == "file":
            pages = self.files.pages.get(document["file_id"]) or []
        else:  # base64, from the command line
            pages = _page_texts(base64.b64decode(document["data"]))
        if output_config and output_config.get("format"):
            if "questions" in output_config["format"]["schema"]["properties"]:
                report = demo_questionnaire(pages, _library(content[0]["text"]))
            else:
                report = demo_review(pages)
            return _Stream(_message(json.dumps(report, ensure_ascii=False), []), "")

        question = messages[-1]["content"]
        if isinstance(question, list):
            question = question[-1]["text"]

        page_no, passage = _best_passage(pages, question)
        if passage:
            text = (f"Demo mode (no API key): this is a canned answer. The most relevant passage "
                    f"for your question is on page {page_no}.")
            citations = [{
                "type": "page_location", "cited_text": passage, "document_index": 0,
                "document_title": "demo", "start_page_number": page_no, "end_page_number": page_no + 1,
            }]
        else:
            text = "Demo mode (no API key): this PDF has no extractable text, so there is nothing to quote."
            citations = []

        return _Stream(_message(text, citations), text)


def _page_texts(data: bytes) -> list[str]:
    try:
        return [(p.extract_text() or "").strip() for p in PdfReader(io.BytesIO(data)).pages]
    except Exception:
        return []


def _message(text: str, citations: list) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": f"msg_demo_{uuid.uuid4().hex[:8]}", "type": "message", "role": "assistant",
        "model": DEMO_MODEL,
        "content": [{"type": "text", "text": text, "citations": citations or None}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": len(text.split()),
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    })


# (keyword, risk title, severity, recommendation)
RISK_RULES = [
    ("automatically renew", "Automatic renewal", "high",
     "Diarise the notice date or negotiate opt-in renewal."),
    ("without limitation", "Uncapped liability", "high", "Ask for a mutual liability cap."),
    ("penalty", "Contractual penalty", "high", "Negotiate the amount down or remove it."),
    ("bank details have changed", "Changed bank details", "high",
     "Confirm the new account by phone with a known contact before paying."),
    ("remain the exclusive property", "IP stays with the other party", "medium",
     "Ask for ownership of, or a licence to, the work you pay for."),
    ("indemnify", "Indemnity", "medium", "Limit the indemnity to your own fault."),
    ("shall not engage any other", "Exclusivity", "medium", "Remove exclusivity or shorten it."),
    ("interest of", "High late-payment interest", "medium", "Align interest with the statutory rate."),
    ("at its own discretion", "Unilateral changes", "medium", "Require written consent for changes."),
    ("may increase the fees", "Unilateral price increase", "medium", "Cap increases, e.g. at inflation."),
    ("only upon payment", "Costly early termination", "medium", "Negotiate termination for convenience."),
]
MONTHS = {m: n for n, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                       "august", "september", "october", "november", "december"], 1)}
DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{1,2}) (" + "|".join(MONTHS) + r") (\d{4})", re.I)
MONEY = re.compile(r"(EUR|USD|BGN|\$|€) ?[\d,.]+|[\d,.]+ ?(EUR|USD|BGN|€)")
LINE = re.compile(r"^(.+?) \| ([\d.,]+) \| ([\d.,]+) \| ([\d.,]+)$")
TOTALS = {"subtotal": re.compile(r"subtotal:? ([\d.,]+)", re.I),
          "tax": re.compile(r"(?:vat|tax)[^:]*: ([\d.,]+)", re.I),
          "total": re.compile(r"\btotal(?: due)?:? ([\d.,]+)", re.I)}


def demo_review(pages: list[str]) -> dict:
    sentences = [(n, s.strip()) for n, page in enumerate(pages, 1)
                 for chunk in _unwrap(page)
                 for s in re.split(r"(?<=\.) (?=[A-Z0-9])", chunk) if len(s.strip()) > 15]
    risks, facts, deadlines, lines = [], [], [], []
    for page, sentence in sentences:
        lower = sentence.lower()
        for keyword, title, severity, advice in RISK_RULES:
            if keyword in lower and not any(r["title"] == title for r in risks):
                risks.append({"title": title, "severity": severity,
                              "explanation": f"Demo rule: the text mentions \"{keyword}\".",
                              "recommendation": advice, "page": page, "quote": sentence[:200]})
        date = DATE.search(sentence)
        if date and len(deadlines) < 6:
            y, m, d = (date[1], date[2], date[3]) if date[1] else (date[6], MONTHS[date[5].lower()], date[4])
            deadlines.append({"description": sentence[:90], "date": f"{int(y):04d}-{int(m):02d}-{int(d):02d}",
                              "page": page, "quote": sentence[:200]})
        if MONEY.search(sentence) and len(facts) < 6 and not LINE.match(sentence):
            facts.append({"label": "Amount", "value": MONEY.search(sentence)[0], "page": page,
                          "quote": sentence[:200]})
        line = LINE.match(sentence)
        if line and line[2][0].isdigit():
            lines.append({"description": line[1], "quantity": _number(line[2]),
                          "unit_price": _number(line[3]), "amount": _number(line[4]), "page": page})
    text = "\n".join(pages)
    totals = {key: _number(m[1]) if (m := rx.search(text)) else None for key, rx in TOTALS.items()}
    is_invoice = bool(lines) or "invoice" in text[:300].lower()
    high = sum(r["severity"] == "high" for r in risks)
    return {
        "document_type": "invoice" if is_invoice else "contract" if "agreement" in text.lower() else "other",
        "title": next((s for _, s in sentences), "Untitled")[:120],
        "summary": "Demo mode (no API key): this report was made with simple keyword rules, not by Claude. "
                   "The quotes, quote checks, arithmetic checks and exports are real.",
        "overall_risk": "high" if high else "medium" if risks else "low",
        "parties": [],
        "key_facts": facts,
        "risks": risks,
        "deadlines": deadlines,
        "missing": [{"item": "A full review",
                     "why_it_matters": "Set ANTHROPIC_API_KEY to let Claude review it."}],
        "line_items": lines,
        "totals": {"currency": "EUR" if "EUR" in text else None, **totals},
    }


ENTRY = re.compile(r'<entry id="(\d+)"[^>]*>\s*<question>(.*?)</question>\s*<answer>(.*?)</answer>', re.S)
QUESTION = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+(.{10,})$")


def _library(text: str) -> list[tuple[int, str, str]]:
    return [(int(i), html.unescape(q), html.unescape(a)) for i, q, a in ENTRY.findall(text)]


def demo_questionnaire(pages: list[str], library: list[tuple[int, str, str]]) -> dict:
    """Questions are numbered lines or lines ending in "?"; each gets the library entry whose
    question shares the most words with it."""
    questions = []
    for page_no, page in enumerate(pages, 1):
        for chunk in _unwrap(page):
            numbered = QUESTION.match(chunk)
            if not (numbered or chunk.endswith("?")):
                continue
            number, text = (numbered[1], numbered[2]) if numbered else ("", chunk)
            words = set(_keywords(text))
            score, best = max(((len(words & set(_keywords(q))) / max(len(words), 1), (i, a))
                               for i, q, a in library), default=(0, None))
            status = "answered" if score >= 0.5 else "partial" if score >= 0.3 else "needs_input"
            questions.append({
                "number": number, "question": text, "page": page_no, "status": status,
                "answer": best[1] if status != "needs_input" else "",
                "sources": [best[0]] if status != "needs_input" else [],
                "note": "" if status == "answered" else
                "Demo rule: no close match in the library." if status == "needs_input" else
                "Demo rule: only a loose match in the library, check it.",
            })
    return {"title": next((c for p in pages for c in _unwrap(p) if c), "Questionnaire")[:120],
            "questions": questions}


def _keywords(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) > 3]


def _unwrap(page: str) -> list[str]:
    """Rejoin lines wrapped mid-sentence; table rows and numbered clauses stay separate."""
    chunks: list[str] = []
    for line in page.splitlines():
        line = line.strip()
        starts_new = (not chunks or "|" in line or "|" in chunks[-1] or chunks[-1].endswith((".", ":", "?"))
                      or re.match(r"\d+\.\d|[\w %]{2,30}:", line))
        if starts_new:
            chunks.append(line)
        else:
            chunks[-1] += " " + line
    return chunks


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def _best_passage(pages: list[str], question: str) -> tuple[int, str]:
    """The sentence sharing the most words with the question (simple keyword overlap)."""
    words = {w.lower().strip(".,?!:;") for w in question.split() if len(w) > 3}
    best = (0, 0, "")
    for number, page in enumerate(pages, start=1):
        for sentence in page.replace("\n", " ").split(". "):
            sentence = sentence.strip()
            if len(sentence) < 20:
                continue
            score = len(words & {w.lower().strip(".,?!:;") for w in sentence.split()})
            if score > best[0] or not best[2]:
                best = (score, number, sentence[:300])
    return best[1], best[2]
