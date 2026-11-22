"""
Offline mode end to end: the parsed layout is stored at upload, the structure and
evidence-search endpoints answer from it, offline chat cites a verified quote,
and the benchmark runs on the bundled corpus. No network, no API calls.
"""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from pdfqa import parse_pdf  # noqa: E402
from storage import Storage  # noqa: E402
from test_api import ALICE, BOB, events, upload  # noqa: E402
from web.app import Settings, create_app  # noqa: E402
from web.demo import DemoClient, offline_answer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "eval" / "corpus"


def corpus_copy(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"test-{name}"
    shutil.copy(CORPUS / name, path)
    return path


# ---------- storage ----------

def test_layout_is_stored_and_page_variants_include_both_renderings(tmp_path):
    data = (CORPUS / "meridian-annual-report.pdf").read_bytes()
    store = Storage(f"sqlite:///{tmp_path / 's.db'}")
    doc_id = store.upsert_document("hash", "r.pdf", "r.pdf", len(data), 4)
    store.save_pages(doc_id, ["raw 1", "raw 2", "raw 3", "raw 4"])
    assert store.get_layout(doc_id) is None
    assert store.get_page_variants(doc_id) == [["raw 1", ""], ["raw 2", ""], ["raw 3", ""], ["raw 4", ""]]

    layout = parse_pdf(data).to_dict()
    store.save_layout(doc_id, layout)
    store.save_layout(doc_id, layout)  # idempotent
    assert store.get_layout(doc_id)["pages"][0]["number"] == 1
    variants = store.get_page_variants(doc_id)
    assert len(variants) == 4 and variants[0][0] == "raw 1" and "Meridian" in " ".join(variants[0][1:])
    store.close()


# ---------- API ----------

def test_structure_endpoint_reports_columns_tables_and_furniture(make_client, tmp_path):
    http, _ = make_client()
    doc = upload(http, corpus_copy(tmp_path, "meridian-annual-report.pdf")).json()

    structure = http.get(f"/api/documents/{doc['id']}/structure").json()

    assert structure["stats"]["pages"] == 4
    assert structure["stats"]["two_column_pages"] >= 1
    assert structure["stats"]["tables"] >= 1
    assert structure["stats"]["table_row_chunks"] >= 3
    assert any(p["furniture"] for p in structure["pages"])
    assert structure["outline"][0]["level"] == 1


def test_ask_endpoint_returns_a_verified_answer_and_passages(make_client, tmp_path):
    http, fake = make_client()
    doc = upload(http, corpus_copy(tmp_path, "hx200-pump-manual.pdf")).json()

    body = http.post(f"/api/documents/{doc['id']}/ask",
                     json={"question": "What is the rated power of the HX200?", "k": 3}).json()

    assert not body["abstained"]
    assert body["verified"] == "verified"
    assert body["page"] >= 1 and body["quote"]
    assert len(body["passages"]) == 3
    assert [p["rank"] for p in body["passages"]] == [1, 2, 3]
    assert body["latency_ms"] >= 0
    assert fake.requests == []  # answered offline, no model call


def test_ask_abstains_on_questions_the_document_does_not_answer(make_client, tmp_path):
    http, _ = make_client()
    doc = upload(http, corpus_copy(tmp_path, "hx200-pump-manual.pdf")).json()

    body = http.post(f"/api/documents/{doc['id']}/ask",
                     json={"question": "Who won the football championship in 1998?"}).json()

    assert body["abstained"] and body["answer"] == "" and body["page"] is None


def test_offline_endpoints_are_private_to_the_owner(make_client, tmp_path):
    http, _ = make_client(auth_mode="easyauth")
    doc = upload(http, corpus_copy(tmp_path, "travel-policy.pdf"), ALICE).json()

    assert http.get(f"/api/documents/{doc['id']}/structure", headers=BOB).status_code == 404
    assert http.post(f"/api/documents/{doc['id']}/ask", json={"question": "hotel"},
                     headers=BOB).status_code == 404
    assert http.post(f"/api/documents/{doc['id']}/ask", json={"question": "hotel"},
                     headers=ALICE).status_code == 200


def test_ask_works_for_documents_stored_before_layouts_existed(make_client, tmp_path):
    http, _ = make_client()
    doc = upload(http, corpus_copy(tmp_path, "travel-policy.pdf")).json()
    with http.app.state.pool.connection() as store:
        cur = store.conn.cursor()
        cur.execute("DELETE FROM layouts")
        store.conn.commit()
    http.delete(f"/api/documents/{doc['id']}")  # also drops the cached parse
    doc = upload(http, corpus_copy(tmp_path, "travel-policy.pdf")).json()
    with http.app.state.pool.connection() as store:
        cur = store.conn.cursor()
        cur.execute("DELETE FROM layouts")
        store.conn.commit()

    structure = http.get(f"/api/documents/{doc['id']}/structure").json()
    assert structure["stats"]["unpositioned_pages"] == structure["stats"]["pages"]
    assert http.post(f"/api/documents/{doc['id']}/ask", json={"question": "hotel"}).status_code == 200


# ---------- offline chat ----------

def test_offline_chat_cites_a_verified_quote_on_the_right_page(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'demo.db'}", demo=True))
    http = TestClient(app)
    doc = upload(http, corpus_copy(tmp_path, "meridian-annual-report.pdf")).json()

    response = http.post("/api/chat", json={"document_id": doc["id"],
                                            "question": "Which firm audited the financial statements?"})

    done = [data for name, data in events(response) if name == "done"][0]
    assert "Calder & Wynn LLP" in done["text"]
    assert done["citations"][0]["start_page"] == 4
    assert "Calder" in done["citations"][0]["cited_text"]
    app.state.pool.close()


def test_offline_answer_says_so_when_nothing_matches():
    text, citations = offline_answer(None, ["The pump is rated at 2.2 kW."], "Who founded the company?")
    assert "could not find" in text and citations == []
    text, citations = offline_answer(None, ["", ""], "anything")
    assert "no extractable text" in text and citations == []


def test_demo_client_forgets_deleted_files():
    client = DemoClient()
    meta = client.files.upload(("a.pdf", (CORPUS / "travel-policy.pdf").read_bytes(), "application/pdf"))
    assert meta.id in client.files.qa
    client.files.delete(meta.id)
    assert meta.id not in client.files.qa and meta.id not in client.files.pages


def test_settings_fall_back_to_offline_mode_without_an_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("OFFLINE_FALLBACK", raising=False)
    assert Settings.from_env().demo
    monkeypatch.setenv("OFFLINE_FALLBACK", "0")
    assert not Settings.from_env().demo
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    monkeypatch.delenv("OFFLINE_FALLBACK")
    assert not Settings.from_env().demo


# ---------- evaluation ----------

def test_corpus_questions_point_at_bundled_pdfs():
    questions = json.loads((ROOT / "eval" / "questions.json").read_text(encoding="utf-8"))
    assert len(questions) >= 60
    assert {q["kind"] for q in questions} >= {"two-column", "table", "text", "unanswerable"}
    for q in questions:
        assert (CORPUS / q["doc"]).exists()


def test_benchmark_quick_run_writes_both_reports(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import benchmark

    report = benchmark.main(["--quick", "--out", str(tmp_path)])

    assert (tmp_path / "benchmark.json").exists() and (tmp_path / "benchmark.md").exists()
    saved = json.loads((tmp_path / "benchmark.json").read_text(encoding="utf-8"))
    assert saved["layout"]["recall"]["@5"] >= saved["naive"]["recall"]["@5"]
    assert report["layout"]["answer_accuracy"] > report["naive"]["answer_accuracy"]
    assert report["verifier"] and report["speed"]["query"]
    assert "not measured" in (tmp_path / "benchmark.md").read_text(encoding="utf-8")
