"""
Tests for storage and conversation logic. No API calls: the Claude client is
replaced by a fake that returns real SDK response objects.

The same suite runs against Azure SQL when PDF_CHAT_TEST_DB is set, e.g.
    PDF_CHAT_TEST_DB=mssql://<server>.database.windows.net/<db> pytest
"""

import os
import sys
import uuid
from pathlib import Path

import pytest
from anthropic.types.beta import BetaMessage
from pypdf import PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_chat  # noqa: E402
from pdf_chat import PdfChat, PdfFile, estimate_cost, format_pages, normalize_argv  # noqa: E402
from storage import Citation, Storage, Usage  # noqa: E402

TEST_DB = os.environ.get("PDF_CHAT_TEST_DB")


@pytest.fixture
def store(tmp_path):
    s = Storage(TEST_DB or f"sqlite:///{tmp_path / 'test.db'}")
    yield s
    if TEST_DB:  # shared database: remove only what this test created
        cur = s.conn.cursor()
        cur.execute("DELETE FROM documents WHERE filename LIKE 'test-%'")
        s.conn.commit()
    s.close()


def make_pdf(folder: Path, pages: int = 3) -> Path:
    """A real PDF with unique content, so every test gets its own document hash."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": uuid.uuid4().hex})
    path = folder / f"test-{uuid.uuid4().hex[:8]}.pdf"
    with path.open("wb") as f:
        writer.write(f)
    return path


def fake_message(text: str, pages=(7, 8), model="claude-opus-5-5", stop_reason="end_turn"):
    return BetaMessage.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": model,
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": text, "citations": [{
                "type": "page_location", "cited_text": "quoted passage",
                "document_index": 0, "document_title": "doc",
                "start_page_number": pages[0], "end_page_number": pages[1],
            }]},
        ],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 20, "output_tokens": 10,
                  "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0},
    })


class FakeStream:
    def __init__(self, message):
        self.message = message
        self.text_stream = [b.text for b in message.content if b.type == "text"]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    """Records every request and answers with queued messages."""

    def __init__(self, *messages):
        self.queue = list(messages)
        self.requests = []
        self.beta = self
        self.messages = self

    def stream(self, **kwargs):
        # Copy the list: the chat appends to it after the request.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        reply = self.queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeStream(reply)


# ---------- storage ----------

def test_same_pdf_is_one_document_and_keeps_its_first_name(store, tmp_path):
    path = make_pdf(tmp_path)
    pdf = PdfFile.from_path(path)
    first = store.upsert_document(pdf.sha256, path.name, str(path), len(pdf.data), 3)
    second = store.upsert_document(pdf.sha256, "renamed.pdf", "/elsewhere/renamed.pdf", len(pdf.data), 3)

    assert first == second
    doc = store.get_document(first)
    assert doc["filename"] == path.name  # the title sent to the API never changes
    assert doc["path"] == "/elsewhere/renamed.pdf"  # but the new location is remembered


def test_failed_turn_saves_nothing(store, tmp_path):
    path = make_pdf(tmp_path)
    pdf = PdfFile.from_path(path)
    doc_id = store.upsert_document(pdf.sha256, path.name, str(path), len(pdf.data), 3)
    session_id = store.create_session(doc_id)
    usage = Usage("claude-opus-5-5", 1, 0, 0, 1, 0.0, 5)
    bad_citation = Citation(1, 1, None)  # NOT NULL violation after both messages are inserted

    with pytest.raises(Exception):
        store.save_turn(session_id, "question", "answer", [], usage, [bad_citation])

    assert store.get_messages(session_id) == []


def test_turns_are_numbered_in_order(store, tmp_path):
    path = make_pdf(tmp_path)
    pdf = PdfFile.from_path(path)
    doc_id = store.upsert_document(pdf.sha256, path.name, str(path), len(pdf.data), 3)
    session_id = store.create_session(doc_id)
    usage = Usage("claude-opus-5-5", 1, 0, 0, 1, 0.001, 5)

    store.save_turn(session_id, "q1", "a1", [], usage, [])
    store.save_turn(session_id, "q2", "a2", [], usage, [Citation(2, 3, "text")])

    rows = store.get_messages(session_id)
    assert [(r["seq"], r["role"], r["text"]) for r in rows] == [
        (1, "user", "q1"), (2, "assistant", "a1"), (3, "user", "q2"), (4, "assistant", "a2"),
    ]
    assert store.get_citations(rows[3]["id"]) == [Citation(2, 3, "text")]


# ---------- conversation ----------

def test_answer_is_saved_with_usage_cost_and_inclusive_pages(store, tmp_path):
    path = make_pdf(tmp_path)
    pdf = PdfFile.from_path(path)
    doc_id = store.upsert_document(pdf.sha256, path.name, str(path), len(pdf.data), pdf.page_count)
    client = FakeClient(fake_message("Revenue was 4.2M.", pages=(7, 9)))

    chat = PdfChat(client, store, pdf, doc_id)
    chat.ask("What was the revenue?")

    answer = store.get_messages(chat.session_id)[1]
    assert answer["text"] == "Revenue was 4.2M."
    assert answer["cache_write_tokens"] == 1000
    expected = (20 * 4.00 + 1000 * 4.00 * 1.25 + 10 * 20.00) / 1_000_000
    assert float(answer["cost_usd"]) == pytest.approx(expected, abs=1e-6)
    # The API's end page (9) is exclusive, so the citation covers pages 7-8.
    assert store.get_citations(answer["id"]) == [Citation(7, 8, "quoted passage")]


def test_resumed_session_replays_identical_history(store, tmp_path):
    path = make_pdf(tmp_path)
    pdf = PdfFile.from_path(path)
    doc_id = store.upsert_document(pdf.sha256, path.name, str(path), len(pdf.data), pdf.page_count)
    first_client = FakeClient(fake_message("First answer."), fake_message("Second answer."))
    chat = PdfChat(first_client, store, pdf, doc_id)
    chat.ask("Question one?")
    chat.ask("Question two?")

    resumed_client = FakeClient(fake_message("Third answer."))
    resumed = PdfChat(resumed_client, store, PdfFile.from_path(path), doc_id, session_id=chat.session_id)
    resumed.ask("Question three?")

    live = first_client.requests[-1]["messages"] + chat.messages[-1:]
    replayed = resumed_client.requests[0]["messages"][:-1]
    # Byte-for-byte the same prefix: the PDF cache hits and thinking blocks stay valid.
    assert replayed == live


def test_refusal_and_errors_leave_no_trace(store, tmp_path):
    path = make_pdf(tmp_path)
    pdf = PdfFile.from_path(path)
    doc_id = store.upsert_document(pdf.sha256, path.name, str(path), len(pdf.data), pdf.page_count)
    client = FakeClient(fake_message("", stop_reason="refusal"), RuntimeError("network down"))
    chat = PdfChat(client, store, pdf, doc_id)

    chat.ask("Something declined")
    with pytest.raises(RuntimeError):
        chat.ask("Request that fails")

    assert chat.messages == []
    assert chat.session_id is None  # no empty session was created
    assert store.list_sessions(doc_id) == []


def test_saved_summary_is_reused_without_an_api_call(store, tmp_path, capsys):
    path = make_pdf(tmp_path)
    client = FakeClient(fake_message("A short summary."))
    args = pdf_chat.build_parser().parse_args(["summary", str(path), "--lang", "Bulgarian"])

    pdf_chat.cmd_summary(args, client, store)
    pdf_chat.cmd_summary(args, client, store)  # the queue is empty: an API call would fail

    assert len(client.requests) == 1
    assert "saved summary" in capsys.readouterr().out


def test_search_and_stats(store, tmp_path):
    path = make_pdf(tmp_path)
    pdf = PdfFile.from_path(path)
    doc_id = store.upsert_document(pdf.sha256, path.name, str(path), len(pdf.data), pdf.page_count)
    chat = PdfChat(FakeClient(fake_message("Net profit grew 12%.")), store, pdf, doc_id)
    chat.ask("How did profit change?")

    hits = store.search("profit")
    assert {h["role"] for h in hits if h["filename"] == path.name} == {"user", "assistant"}

    stats = store.stats()
    row = next(r for r in stats["per_document"] if r["filename"] == path.name)
    assert row["answers"] == 1
    assert int(row["prompt_tokens"]) == 1020


# ---------- helpers ----------

def test_estimate_cost_unknown_model_is_none():
    usage = fake_message("x").usage
    assert estimate_cost("some-future-model", usage) is None


def test_format_pages_merges_duplicates_and_ranges():
    cites = [Citation(3, 3, "a"), Citation(3, 3, "b"), Citation(7, 8, "c"), Citation(1, 1, "d")]
    assert format_pages(cites) == "p. 1, p. 3, p. 7-8"


@pytest.mark.parametrize("argv, expected", [
    (["doc.pdf"], ["chat", "doc.pdf"]),
    (["doc.pdf", "--summary", "--lang", "Bulgarian"], ["summary", "doc.pdf", "--lang", "Bulgarian"]),
    (["stats"], ["stats"]),
])
def test_original_command_line_still_works(argv, expected):
    assert normalize_argv(argv) == expected


def test_old_single_user_database_is_migrated_in_place(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
        path TEXT NOT NULL, size_bytes INTEGER NOT NULL, page_count INTEGER, created_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO documents (sha256, filename, path, size_bytes, created_at) "
                 "VALUES ('abc', 'old.pdf', '/old.pdf', 1, '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()

    store = Storage(f"sqlite:///{db}")

    assert [d["filename"] for d in store.list_documents()] == ["old.pdf"]  # now owned by "local"
    assert store.get_document(1)["file_id"] is None
    store.close()
