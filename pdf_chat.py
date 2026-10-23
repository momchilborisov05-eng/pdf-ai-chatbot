"""
PDF AI Assistant - ask questions about any PDF or get a quick summary.

Uses the Claude API's native PDF support: the whole document (text, tables,
charts) is sent to the model, so no manual text extraction is needed.
The PDF is cached with prompt caching, so follow-up questions are faster
and much cheaper than the first one.

Usage:
    python pdf_chat.py document.pdf              # interactive chat
    python pdf_chat.py document.pdf --summary    # summary only
    python pdf_chat.py document.pdf --summary --lang Bulgarian
"""

import argparse
import base64
import os
import sys
from pathlib import Path

import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

# Models that support server-side refusal fallbacks ("default" routing).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

MAX_PDF_BYTES = 32 * 1024 * 1024  # API request limit

SYSTEM_PROMPT = (
    "You are a precise document assistant. Answer only from the attached PDF. "
    "When you use information from the document, mention the page number, like (p. 3). "
    "If the answer is not in the document, say so clearly instead of guessing. "
    "Answer in the same language as the user's question."
)


def load_pdf(path: Path) -> dict:
    """Read a PDF file and return it as a document content block."""
    if not path.exists():
        sys.exit(f"File not found: {path}")
    if path.suffix.lower() != ".pdf":
        sys.exit(f"Not a PDF file: {path}")
    data = path.read_bytes()
    if len(data) > MAX_PDF_BYTES:
        sys.exit(f"PDF is too large ({len(data) / 1_000_000:.1f} MB). The limit is 32 MB.")

    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
        "title": path.name,
        # Cache the PDF, so every follow-up question reuses it.
        "cache_control": {"type": "ephemeral"},
    }


class PdfChat:
    """A multi-turn conversation about one PDF document."""

    def __init__(self, client: anthropic.Anthropic, document: dict):
        self.client = client
        self.document = document
        self.messages: list[dict] = []

    def ask(self, question: str) -> str:
        # The PDF goes into the first user message only; later turns reuse it from history.
        if not self.messages:
            content = [self.document, {"type": "text", "text": question}]
        else:
            content = question
        self.messages.append({"role": "user", "content": content})

        extra = {}
        if MODEL in FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

        answer_parts = []
        with self.client.beta.messages.stream(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "medium"},
            messages=self.messages,
            **extra,
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
                answer_parts.append(text)
            response = stream.get_final_message()
        print()

        if response.stop_reason == "refusal":
            self.messages.pop()  # drop the question so the chat can continue
            return "[The model declined to answer this question.]"

        # Keep the full content (including thinking blocks) in the history.
        self.messages.append({"role": "assistant", "content": response.content})

        usage = response.usage
        print(
            f"\n[tokens - input: {usage.input_tokens}, "
            f"cached: {usage.cache_read_input_tokens or 0}, "
            f"output: {usage.output_tokens}]"
        )
        return "".join(answer_parts)


def summary_prompt(language: str) -> str:
    return (
        f"Write a summary of this document in {language}. Use this structure:\n"
        "1. One-sentence overview\n"
        "2. Key points (5-8 bullet points, with page numbers)\n"
        "3. Important numbers, dates or names\n"
        "4. Conclusion / what the reader should do next"
    )


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
    parser = argparse.ArgumentParser(description="Chat with a PDF using Claude.")
    parser.add_argument("pdf", type=Path, help="path to the PDF file")
    parser.add_argument("--summary", action="store_true", help="print a summary and exit")
    parser.add_argument("--lang", default="English", help="summary language (default: English)")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Missing API key. Set ANTHROPIC_API_KEY first (see README).")

    document = load_pdf(args.pdf)
    chat = PdfChat(anthropic.Anthropic(), document)

    try:
        if args.summary:
            chat.ask(summary_prompt(args.lang))
            return

        print(f"Loaded '{args.pdf.name}'. Ask a question (type 'summary' or 'exit').\n")
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
                question = summary_prompt(args.lang)
            print("\nAI: ", end="")
            chat.ask(question)
            print()

    except anthropic.AuthenticationError:
        sys.exit("Invalid API key. Set the ANTHROPIC_API_KEY environment variable.")
    except anthropic.RateLimitError:
        sys.exit("Rate limit reached. Wait a minute and try again.")
    except anthropic.APIStatusError as e:
        sys.exit(f"API error ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Network error. Check your internet connection.")


if __name__ == "__main__":
    main()
