"""
PDF AI Assistant - ask questions about any PDF or get a quick summary.

Uses the Claude API's native PDF support: the whole document (text, tables,
charts) is sent to the model, so no manual text extraction is needed.
The PDF is cached with prompt caching, so follow-up questions are faster
and much cheaper than the first one.

Every conversation is saved to a database (SQLite locally, or Azure SQL):
answers with their page citations, token usage and cost. Conversations can be
resumed later, searched, and summaries are reused instead of paid for twice.

Usage:
    python pdf_chat.py chat document.pdf            # new conversation
    python pdf_chat.py chat document.pdf --continue # continue the latest one
    python pdf_chat.py summary document.pdf --lang Bulgarian
    python pdf_chat.py resume 12                    # continue session 12
    python pdf_chat.py docs | sessions | show 12 | search "revenue" | stats

The same conversation engine (PdfChat) powers the web app in web/app.py.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import anthropic
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from storage import DEFAULT_URL, Citation, Storage, Usage, to_datetime

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

# Models that support server-side refusal fallbacks ("default" routing).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

MAX_PDF_BYTES = 32 * 1024 * 1024  # API request limit
MAX_PDF_PAGES = 600

# USD per million tokens: (input, output, cache read). Cache writes cost 1.25x input.
# Anthropic list prices as of 2026-10; models not listed are saved without a cost.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_MULTIPLIER = 1.25

SYSTEM_PROMPT = (
    "You are a precise document assistant. Answer only from the attached PDF. "
    "If the answer is not in the document, say so clearly instead of guessing. "
    "Answer in the same language as the user's question."
)


class PdfError(ValueError):
    """The file can't be used as a PDF document (the message is safe to show users)."""


class PdfFile:
    """A PDF's bytes, validated and ready to send to the API."""

    def __init__(self, data: bytes, name: str, path: Path | None = None):
        # The PDF header may follow a little junk, but must be near the start.
        if b"%PDF-" not in data[:1024]:
            raise PdfError(f"'{name}' is not a PDF file.")
        if len(data) > MAX_PDF_BYTES:
            raise PdfError(f"PDF is too large ({len(data) / 1_000_000:.1f} MB). The limit is 32 MB.")
        self.data = data
        self.name = name
        self.path = path
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.page_count = count_pages(data)
        if self.page_count and self.page_count > MAX_PDF_PAGES:
            raise PdfError(f"PDF has {self.page_count} pages. The limit is {MAX_PDF_PAGES}.")

    @classmethod
    def from_path(cls, path: Path) -> "PdfFile":
        if not path.exists():
            raise PdfError(f"File not found: {path}")
        if path.suffix.lower() != ".pdf":
            raise PdfError(f"Not a PDF file: {path}")
        return cls(path.read_bytes(), path.name, path.resolve())

    def page_texts(self) -> list[str]:
        """The text layer of each page ("" for scanned pages), used to verify quoted evidence."""
        try:
            return [page.extract_text() or "" for page in PdfReader(io.BytesIO(self.data)).pages]
        except (PdfReadError, ValueError, OSError):
            return []

    def document_block(self, title: str, citations: bool = True) -> dict:
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(self.data).decode("ascii"),
            },
            "title": title,
            # Cache the PDF, so every follow-up question reuses it.
            "cache_control": {"type": "ephemeral"},
            # Exact page-level sources for every claim in the answer (not allowed with a JSON schema).
            **({"citations": {"enabled": True}} if citations else {}),
        }


class UploadedPdf:
    """A PDF already stored with the Claude Files API: requests send its id, not its bytes."""

    def __init__(self, file_id: str):
        self.file_id = file_id

    def document_block(self, title: str, citations: bool = True) -> dict:
        return {
            "type": "document",
            "source": {"type": "file", "file_id": self.file_id},
            "title": title,
            "cache_control": {"type": "ephemeral"},
            **({"citations": {"enabled": True}} if citations else {}),
        }


def count_pages(data: bytes) -> int | None:
    """Page count for the library, or None for PDFs pypdf can't open (e.g. encrypted)."""
    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except (PdfReadError, ValueError, OSError):
        return None


def estimate_cost(model: str, usage) -> float | None:
    prices = PRICES.get(model)
    if not prices:
        return None
    input_price, output_price, cache_read_price = prices
    cache_write = usage.cache_creation_input_tokens or 0
    cache_read = usage.cache_read_input_tokens or 0
    return (
        usage.input_tokens * input_price
        + cache_write * input_price * CACHE_WRITE_MULTIPLIER
        + cache_read * cache_read_price
        + usage.output_tokens * output_price
    ) / 1_000_000


def extract_citations(content) -> list[Citation]:
    """Page citations from the answer. The API's end page is exclusive; ours is inclusive."""
    found = []
    for block in content:
        if block.type != "text" or not block.citations:
            continue
        for c in block.citations:
            if c.type == "page_location":
                start = c.start_page_number
                found.append(Citation(start, max(start, c.end_page_number - 1), c.cited_text))
    return found


def format_pages(citations: list[Citation]) -> str:
    ranges = sorted({(c.start_page, c.end_page) for c in citations})
    return ", ".join(f"p. {a}" if a == b else f"p. {a}-{b}" for a, b in ranges)


@dataclass
class Turn:
    """The outcome of one question."""

    text: str
    citations: list[Citation]
    usage: Usage | None
    session_id: int | None
    answer_id: int | None = None
    refused: bool = False
    truncated: bool = False


class PdfChat:
    """A multi-turn conversation about one PDF, saved to the database after every answer."""

    def __init__(self, client: anthropic.Anthropic, store: Storage, pdf: PdfFile | UploadedPdf,
                 document_id: int, kind: str = "chat", language: str | None = None,
                 session_id: int | None = None):
        self.client = client
        self.store = store
        self.document_id = document_id
        self.kind = kind
        self.language = language
        # The title is the name the document was first saved under, so replayed
        # history is identical to what the model saw (and the PDF cache still hits).
        self.document = pdf.document_block(store.get_document(document_id)["filename"])
        self.session_id = session_id
        self.messages: list[dict] = self._load_history() if session_id else []

    def _load_history(self) -> list[dict]:
        """Rebuild the API message list from the saved session."""
        messages = []
        for row in self.store.get_messages(self.session_id):
            if row["role"] == "user":
                # The PDF goes into the first user message only, exactly as when it was asked.
                content = row["text"] if messages else [self.document, {"type": "text", "text": row["text"]}]
            else:
                content = json.loads(row["content_json"])
            messages.append({"role": row["role"], "content": content})
        return messages

    def ask(self, question: str, on_text: Callable[[str], None] | None = None) -> Turn:
        """Ask a question; streamed text goes to on_text as it arrives."""
        for kind, value in self.stream(question):
            if kind == "text" and on_text:
                on_text(value)
            elif kind == "done":
                return value
        raise RuntimeError("stream ended without a result")

    def stream(self, question: str) -> Iterator[tuple[str, object]]:
        """Yield ("text", chunk) while the answer streams, then ("done", Turn).

        Nothing is saved unless the answer completes: a failed request, a refusal
        or a client that disconnects mid-answer leaves the history unchanged.
        """
        content = question if self.messages else [self.document, {"type": "text", "text": question}]
        self.messages.append({"role": "user", "content": content})

        extra = {}
        if MODEL in FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

        started = time.monotonic()
        answer_parts = []
        try:
            with self.client.beta.messages.stream(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                output_config={"effort": "medium"},
                messages=self.messages,
                **extra,
            ) as stream:
                for text in stream.text_stream:
                    answer_parts.append(text)
                    yield "text", text
                response = stream.get_final_message()
        except BaseException:  # includes GeneratorExit when a web client disconnects
            self.messages.pop()  # keep history consistent if the request failed
            raise
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.stop_reason == "refusal":
            self.messages.pop()  # drop the question so the chat can continue
            yield "done", Turn("The model declined to answer this question.", [], None,
                               self.session_id, refused=True)
            return

        # Keep the full content (including thinking blocks) in the history, exactly as returned.
        answer_content = [block.to_dict() for block in response.content]
        self.messages.append({"role": "assistant", "content": answer_content})

        answer = "".join(answer_parts)
        citations = extract_citations(response.content)
        usage = Usage(
            model=response.model,
            input_tokens=response.usage.input_tokens,
            cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
            cache_read_tokens=response.usage.cache_read_input_tokens or 0,
            output_tokens=response.usage.output_tokens,
            cost_usd=estimate_cost(response.model, response.usage),
            latency_ms=latency_ms,
        )

        # Sessions are created on the first answer, so opening a PDF and leaving saves nothing.
        if self.session_id is None:
            self.session_id = self.store.create_session(self.document_id, self.kind, self.language)
        answer_id = self.store.save_turn(self.session_id, question, answer, answer_content,
                                         usage, citations)
        yield "done", Turn(answer, citations, usage, self.session_id, answer_id,
                           truncated=response.stop_reason == "max_tokens")


def summary_prompt(language: str) -> str:
    return (
        f"Write a summary of this document in {language}. Use this structure:\n"
        "1. One-sentence overview\n"
        "2. Key points (5-8 bullet points, with page numbers)\n"
        "3. Important numbers, dates or names\n"
        "4. Conclusion / what the reader should do next"
    )


# ---------- commands ----------

def load_pdf(path: Path) -> PdfFile:
    try:
        return PdfFile.from_path(path)
    except PdfError as e:
        sys.exit(str(e))


def open_pdf(store: Storage, path: Path) -> tuple[PdfFile, int]:
    pdf = load_pdf(path)
    doc_id = store.upsert_document(pdf.sha256, path.name, str(pdf.path), len(pdf.data), pdf.page_count)
    return pdf, doc_id


def print_answer(chat: PdfChat, question: str) -> Turn:
    """Stream an answer to the terminal, then its sources and cost."""
    turn = chat.ask(question, on_text=lambda text: print(text, end="", flush=True))
    if turn.refused:
        print(f"[{turn.text}]")
        return turn
    print()
    if turn.truncated:
        print("[The answer was cut off at the output limit.]")
    if turn.citations:
        print(f"\nSources: {format_pages(turn.citations)}")
    usage = turn.usage
    cost = f"${usage.cost_usd:.4f}" if usage.cost_usd is not None else "n/a"
    print(
        f"[tokens - input: {usage.input_tokens}, cached: {usage.cache_read_tokens}, "
        f"output: {usage.output_tokens} | cost: {cost} | session {turn.session_id}]"
    )
    return turn


def chat_loop(chat: PdfChat, language: str):
    name = chat.document["title"]
    if chat.messages:
        print(f"Continuing session {chat.session_id} on '{name}' "
              f"({len(chat.messages) // 2} earlier questions).")
    else:
        print(f"Loaded '{name}'.")
    print("Ask a question (type 'summary' or 'exit').\n")
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            break
        if question.lower() == "summary":
            question = summary_prompt(language)
        print("\nAI: ", end="")
        print_answer(chat, question)
        print()


def cmd_chat(args, client, store):
    pdf, doc_id = open_pdf(store, args.pdf)
    session_id = None
    if args.continue_:
        latest = store.latest_session(doc_id, "chat")
        session_id = latest["id"] if latest else None
    chat_loop(PdfChat(client, store, pdf, doc_id, session_id=session_id), args.lang)


def cmd_resume(args, client, store):
    session = store.get_session(args.session)
    if not session:
        sys.exit(f"No session {args.session}. See: python pdf_chat.py sessions")
    pdf = load_pdf(args.pdf or Path(session["path"]))
    if pdf.sha256 != session["sha256"]:
        sys.exit(f"'{pdf.name}' is not the PDF this session was about "
                 f"('{session['filename']}'). Pass the original file with --pdf.")
    store.upsert_document(pdf.sha256, pdf.name, str(pdf.path), len(pdf.data), pdf.page_count)
    chat = PdfChat(client, store, pdf, session["document_id"], session["kind"],
                   session["language"], session_id=session["id"])
    chat_loop(chat, args.lang)


def cmd_summary(args, client, store):
    pdf, doc_id = open_pdf(store, args.pdf)
    if not args.refresh:
        saved = store.latest_session(doc_id, "summary", args.lang)
        if saved:
            answer = [m for m in store.get_messages(saved["id"]) if m["role"] == "assistant"][-1]
            print(answer["text"])
            when = to_datetime(answer["created_at"]).strftime("%Y-%m-%d %H:%M")
            print(f"\n[saved summary from {when} UTC, no API cost - use --refresh for a new one]")
            return
    print_answer(PdfChat(client, store, pdf, doc_id, "summary", args.lang), summary_prompt(args.lang))


def cmd_docs(args, store):
    rows = store.list_documents()
    if not rows:
        print("No documents yet. Start with: python pdf_chat.py chat file.pdf")
        return
    print(f"{'ID':>4}  {'Pages':>5}  {'Size':>8}  {'Sessions':>8}  {'Questions':>9}  File")
    for r in rows:
        pages = r["page_count"] if r["page_count"] is not None else "?"
        print(f"{r['id']:>4}  {pages:>5}  {r['size_bytes'] / 1_000_000:>6.1f}MB  "
              f"{r['sessions']:>8}  {r['questions']:>9}  {r['filename']}")


def cmd_sessions(args, store):
    rows = store.list_sessions(args.doc, args.limit)
    if not rows:
        print("No sessions yet.")
        return
    print(f"{'ID':>4}  {'Updated (UTC)':<16}  {'Kind':<8}  {'Q':>3}  {'Cost':>8}  File")
    for r in rows:
        kind = r["kind"] + (f" ({r['language']})" if r["language"] else "")
        print(f"{r['id']:>4}  {to_datetime(r['updated_at']):%Y-%m-%d %H:%M}  {kind:<8}  "
              f"{r['questions']:>3}  ${float(r['cost_usd']):>7.4f}  {r['filename']}")


def cmd_show(args, store):
    session = store.get_session(args.session)
    if not session:
        sys.exit(f"No session {args.session}.")
    print(f"Session {session['id']} - {session['filename']} ({session['kind']})\n")
    for m in store.get_messages(args.session):
        if m["role"] == "user":
            print(f"You: {m['text']}\n")
            continue
        print(f"AI: {m['text']}")
        citations = store.get_citations(m["id"])
        if citations:
            print(f"Sources: {format_pages(citations)}")
        cost = f"${float(m['cost_usd']):.4f}" if m["cost_usd"] is not None else "n/a"
        print(f"[{m['model']} | {m['output_tokens']} output tokens | {cost}]\n")


def cmd_search(args, store):
    rows = store.search(args.term, args.limit)
    if not rows:
        print(f"Nothing found for '{args.term}'.")
        return
    for r in rows:
        snippet = " ".join(r["text"].split())
        snippet = snippet if len(snippet) <= 160 else snippet[:157] + "..."
        who = "You" if r["role"] == "user" else "AI"
        print(f"[session {r['session_id']} | {r['filename']}] {who}: {snippet}")


def cmd_stats(args, store):
    s = store.stats()
    if not s["per_document"]:
        print("No answers yet.")
        return
    print(f"{'Answers':>7}  {'Prompt tok':>10}  {'Output tok':>10}  {'Cost':>9}  File")
    for r in s["per_document"]:
        print(f"{r['answers']:>7}  {int(r['prompt_tokens']):>10}  {int(r['output_tokens']):>10}  "
              f"${float(r['cost_usd']):>8.4f}  {r['filename']}")
    print("\nBy model:")
    for r in s["per_model"]:
        latency = f"{float(r['avg_latency_ms']) / 1000:.1f}s" if r["avg_latency_ms"] else "-"
        print(f"  {r['model']}: {r['answers']} answers, ${float(r['cost_usd']):.4f}, avg {latency}")
    print(f"\nTotal: ${s['total_cost_usd']:.4f} | "
          f"{s['cache_hit_rate']:.0%} of prompt tokens served from cache")


# ---------- entry point ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chat with a PDF using Claude, with saved history.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("chat", help="ask questions about a PDF")
    p.add_argument("pdf", type=Path)
    p.add_argument("--continue", dest="continue_", action="store_true",
                   help="continue the latest conversation about this PDF")
    p.add_argument("--lang", default="English", help="language for the 'summary' shortcut")

    p = sub.add_parser("resume", help="continue a saved session")
    p.add_argument("session", type=int)
    p.add_argument("--pdf", type=Path, help="PDF location, if it moved since the session")
    p.add_argument("--lang", default="English")

    p = sub.add_parser("summary", help="print a summary (reused from the database when possible)")
    p.add_argument("pdf", type=Path)
    p.add_argument("--lang", default="English", help="summary language (default: English)")
    p.add_argument("--refresh", action="store_true", help="ignore the saved summary")

    sub.add_parser("docs", help="list documents in the library")

    p = sub.add_parser("sessions", help="list saved sessions")
    p.add_argument("--doc", type=int, help="only sessions for this document id")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("show", help="print a saved session with its sources")
    p.add_argument("session", type=int)

    p = sub.add_parser("search", help="search past questions and answers")
    p.add_argument("term")
    p.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats", help="token usage and cost report")
    return parser


def normalize_argv(argv: list[str]) -> list[str]:
    """Keep the original interface working: `pdf_chat.py file.pdf [--summary]`."""
    if argv and argv[0].lower().endswith(".pdf"):
        if "--summary" in argv:
            return ["summary"] + [a for a in argv if a != "--summary"]
        return ["chat"] + argv
    return argv


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
    args = build_parser().parse_args(normalize_argv(sys.argv[1:]))

    try:
        store = Storage(os.environ.get("PDF_CHAT_DB", DEFAULT_URL))
    except Exception as e:  # driver errors differ per backend
        sys.exit(f"Could not open the database: {e}")

    offline = {"docs": cmd_docs, "sessions": cmd_sessions, "show": cmd_show,
               "search": cmd_search, "stats": cmd_stats}
    try:
        if args.command in offline:
            offline[args.command](args, store)
            return

        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("Missing API key. Set ANTHROPIC_API_KEY first (see README).")
        client = anthropic.Anthropic()
        commands = {"chat": cmd_chat, "resume": cmd_resume, "summary": cmd_summary}
        commands[args.command](args, client, store)

    except anthropic.AuthenticationError:
        sys.exit("Invalid API key. Set the ANTHROPIC_API_KEY environment variable.")
    except anthropic.RateLimitError:
        sys.exit("Rate limit reached. Wait a minute and try again.")
    except anthropic.APIStatusError as e:
        sys.exit(f"API error ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Network error. Check your internet connection.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
