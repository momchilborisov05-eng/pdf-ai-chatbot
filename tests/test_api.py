"""
Tests for the web service. The Claude client is a fake (no API calls, no cost);
the database is a temporary SQLite file, or Azure SQL when PDF_CHAT_TEST_DB is set
(the make_client fixture is in conftest.py).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_pdf_chat import fake_message, make_pdf  # noqa: E402

ALICE = {"x-ms-client-principal-id": "alice-id", "x-ms-client-principal-name": "alice@example.com"}
BOB = {"x-ms-client-principal-id": "bob-id", "x-ms-client-principal-name": "bob@example.com"}


def upload(http, path: Path, headers=None):
    with path.open("rb") as f:
        return http.post("/api/documents", files={"file": (path.name, f, "application/pdf")},
                         headers=headers or {})


def events(response) -> list[tuple[str, dict]]:
    out = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


# ---------- documents ----------

def test_upload_sends_the_pdf_to_claude_once(make_client, tmp_path):
    http, fake = make_client()
    path = make_pdf(tmp_path, pages=4)

    first = upload(http, path)
    second = upload(http, path)

    assert first.status_code == 201
    assert first.json()["page_count"] == 4
    assert second.json()["id"] == first.json()["id"]
    assert fake.files.uploaded == [path.name]  # the second upload reused the stored file


def test_upload_rejects_files_that_are_not_pdfs(make_client, tmp_path):
    http, fake = make_client()
    fake_pdf = tmp_path / "test-notes.pdf"
    fake_pdf.write_text("just text, renamed to .pdf")

    response = upload(http, fake_pdf)

    assert response.status_code == 422
    assert "not a PDF" in response.json()["detail"]
    assert fake.files.uploaded == []


def test_delete_removes_history_and_the_stored_file(make_client, tmp_path):
    http, fake = make_client(fake_message("Answer."))
    doc = upload(http, make_pdf(tmp_path)).json()
    http.post("/api/chat", json={"document_id": doc["id"], "question": "Q?"})

    assert http.delete(f"/api/documents/{doc['id']}").status_code == 204
    assert http.get("/api/documents").json() == []
    assert len(fake.files.deleted) == 1
    assert http.delete(f"/api/documents/{doc['id']}").status_code == 404


# ---------- chat ----------

def test_chat_streams_the_answer_and_continues_the_session(make_client, tmp_path):
    http, fake = make_client(fake_message("Revenue was 4.2M.", pages=(7, 9)),
                             fake_message("It grew 12%."))
    doc = upload(http, make_pdf(tmp_path)).json()

    first = events(http.post("/api/chat", json={"document_id": doc["id"], "question": "Revenue?"}))
    assert first[0] == ("delta", {"text": "Revenue was 4.2M."})
    name, done = first[-1]
    assert name == "done"
    assert done["citations"] == [{"start_page": 7, "end_page": 8, "cited_text": "quoted passage"}]
    assert done["usage"]["cost_usd"] > 0

    second = events(http.post("/api/chat", json={
        "document_id": doc["id"], "session_id": done["session_id"], "question": "Growth?"}))
    assert second[-1][1]["session_id"] == done["session_id"]

    # The PDF travels as a Files API reference, not as base64 bytes.
    first_block = fake.requests[1]["messages"][0]["content"][0]
    assert first_block["source"]["type"] == "file"
    session = http.get(f"/api/sessions/{done['session_id']}").json()
    assert [m["role"] for m in session["messages"]] == ["user", "assistant", "user", "assistant"]


def test_failed_answer_reports_an_error_and_saves_nothing(make_client, tmp_path):
    http, _ = make_client(RuntimeError("connection dropped"))
    doc = upload(http, make_pdf(tmp_path)).json()

    result = events(http.post("/api/chat", json={"document_id": doc["id"], "question": "Q?"}))

    assert result[-1][0] == "error"
    assert http.get(f"/api/documents/{doc['id']}/sessions").json() == []


def test_summary_is_generated_once_then_served_from_the_database(make_client, tmp_path):
    http, fake = make_client(fake_message("Short summary."))
    doc = upload(http, make_pdf(tmp_path)).json()
    url = f"/api/documents/{doc['id']}/summary"

    streamed = events(http.post(url, json={"language": "Bulgarian"}))
    cached = http.post(url, json={"language": "Bulgarian"}).json()

    assert streamed[-1][0] == "done"
    assert cached["cached"] is True and cached["text"] == "Short summary."
    assert len(fake.requests) == 1


def test_daily_budget_blocks_further_questions(make_client, tmp_path):
    http, fake = make_client(fake_message("Answer."), daily_budget_per_user=0.001)
    doc = upload(http, make_pdf(tmp_path)).json()

    http.post("/api/chat", json={"document_id": doc["id"], "question": "Q1?"})  # costs ~$0.005
    blocked = http.post("/api/chat", json={"document_id": doc["id"], "question": "Q2?"})

    assert blocked.status_code == 429
    assert "limit" in blocked.json()["detail"]
    assert len(fake.requests) == 1


# ---------- users ----------

def test_sign_in_is_required_and_the_allowlist_is_enforced(make_client):
    http, _ = make_client(auth_mode="easyauth", allowed_users={"alice@example.com"})

    assert http.get("/api/documents").status_code == 401
    assert http.get("/api/documents", headers=BOB).status_code == 403
    assert http.get("/api/documents", headers=ALICE).status_code == 200


def test_users_cannot_see_or_touch_each_others_documents(make_client, tmp_path):
    http, _ = make_client(fake_message("Alice's answer about profit."), auth_mode="easyauth")
    doc = upload(http, make_pdf(tmp_path), ALICE).json()
    done = events(http.post("/api/chat", headers=ALICE,
                            json={"document_id": doc["id"], "question": "Profit?"}))[-1][1]
    session_id = done["session_id"]

    assert http.get("/api/documents", headers=BOB).json() == []
    assert http.get(f"/api/sessions/{session_id}", headers=BOB).status_code == 404
    assert http.get(f"/api/documents/{doc['id']}/sessions", headers=BOB).status_code == 404
    assert http.post("/api/chat", headers=BOB,
                     json={"document_id": doc["id"], "question": "Profit?"}).status_code == 404
    assert http.delete(f"/api/documents/{doc['id']}", headers=BOB).status_code == 404
    assert http.get("/api/search?q=profit", headers=BOB).json() == []
    assert http.get("/api/stats", headers=BOB).json()["per_document"] == []
    # ...while Alice still has everything.
    assert len(http.get("/api/search?q=profit", headers=ALICE).json()) == 2


def test_the_same_pdf_uploaded_by_two_users_is_two_documents(make_client, tmp_path):
    http, fake = make_client(auth_mode="easyauth")
    path = make_pdf(tmp_path)

    alice_doc = upload(http, path, ALICE).json()
    bob_doc = upload(http, path, BOB).json()

    assert alice_doc["id"] != bob_doc["id"]
    assert len(fake.files.uploaded) == 2


def test_a_session_cannot_be_continued_on_another_document(make_client, tmp_path):
    http, _ = make_client(fake_message("A."))
    doc_a = upload(http, make_pdf(tmp_path)).json()
    doc_b = upload(http, make_pdf(tmp_path)).json()
    session_id = events(http.post("/api/chat", json={"document_id": doc_a["id"], "question": "Q?"}))[-1][1][
        "session_id"]

    response = http.post("/api/chat", json={
        "document_id": doc_b["id"], "session_id": session_id, "question": "Q?"})

    assert response.status_code == 404


# ---------- search, health, headers ----------

def test_search_treats_wildcards_literally(make_client, tmp_path):
    http, _ = make_client(fake_message("Margin rose 100% in Q4."), fake_message("Nothing here."))
    doc = upload(http, make_pdf(tmp_path)).json()
    http.post("/api/chat", json={"document_id": doc["id"], "question": "Margin?"})
    http.post("/api/chat", json={"document_id": doc["id"], "question": "Other?"})

    hits = http.get("/api/search", params={"q": "100%"}).json()
    assert [h["text"] for h in hits] == ["Margin rose 100% in Q4."]
    assert http.get("/api/search", params={"q": "_"}).status_code == 422  # too short
    assert http.get("/api/search", params={"q": "%%"}).json() == []


def test_health_and_security_headers(make_client):
    http, _ = make_client()

    assert http.get("/healthz").json() == {"status": "ok"}
    ready = http.get("/readyz").json()
    assert ready["status"] == "ready"
    page = http.get("/")
    assert page.status_code == 200
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-request-id"]
