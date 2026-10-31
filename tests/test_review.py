"""
Tests for document review: quote verification, invoice arithmetic, the review
API with its exports, and that reviews count towards budgets. No API calls.
"""

import copy
import json
import sys
import uuid
from pathlib import Path

import pytest
from anthropic.types.beta import BetaMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from review import check_arithmetic, verify_quotes  # noqa: E402
from samples.build import text_pdf  # noqa: E402
from test_api import ALICE, BOB, events, upload  # noqa: E402

PAGES = [
    "MASTER SERVICES AGREEMENT\nThe Agreement shall automatically renew for successive periods of 24 months "
    "unless either party gives notice at least 120 days before the end of the term.",
    "5. Liability\nThe Client shall be liable without limita-\ntion for any damage, including lost profits.\n"
    "Invoices are payable within 10 days.",
]

REPORT = {
    "document_type": "contract",
    "title": "Master Services Agreement",
    "summary": "A two-year services agreement that renews automatically.",
    "overall_risk": "high",
    "parties": [{"name": "Northwind", "role": "Supplier"}],
    "key_facts": [{"label": "Payment term", "value": "10 days", "page": 2,
                   "quote": "Invoices are payable within 10 days."}],
    "risks": [
        {"title": "Penalty", "severity": "medium", "explanation": "x", "recommendation": "y",
         "page": 1, "quote": "The Client shall pay a penalty of EUR 1,000,000."},  # invented
        {"title": "Uncapped liability", "severity": "high", "explanation": "x", "recommendation": "y",
         "page": 2, "quote": "liable without limitation for any damage, including lost profits"},
        {"title": "Automatic renewal", "severity": "high", "explanation": "x", "recommendation": "y",
         "page": 2, "quote": "The Agreement shall automatically renew for successive periods of 24 months"},
    ],
    "deadlines": [{"description": "Last day to cancel renewal", "date": "2028-07-15", "page": 1,
                   "quote": "at least 120 days before the end of the term"}],
    "missing": [{"item": "Liability cap", "why_it_matters": "Exposure is unlimited."}],
    "line_items": [],
    "totals": {"currency": None, "subtotal": None, "tax": None, "total": None},
}


def review_message(report: dict) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": "msg_review", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": json.dumps(report)}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 3000, "output_tokens": 1500,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    })


def contract_pdf(folder: Path) -> Path:
    path = folder / f"test-{uuid.uuid4().hex[:8]}.pdf"
    path.write_bytes(text_pdf([PAGES[0] + f"\nRef {uuid.uuid4().hex}", PAGES[1]]))
    return path


# ---------- quote verification ----------

def test_quotes_are_checked_against_the_pdf_text():
    report = copy.deepcopy(REPORT)

    counts = verify_quotes(report, PAGES)

    invented, hyphenated, wrong_page = report["risks"]
    assert invented["verified"] == "not_found"
    assert hyphenated["verified"] == "verified"  # "limita-\ntion" across a line break still matches
    assert wrong_page["verified"] == "moved" and wrong_page["page"] == 1  # page corrected
    assert report["key_facts"][0]["verified"] == "verified"
    assert counts == {"verified": 3, "moved": 1, "not_found": 1, "unchecked": 0, "text_layer": True}


def test_scanned_pdfs_are_marked_unchecked_not_wrong():
    report = copy.deepcopy(REPORT)

    counts = verify_quotes(report, ["", ""])

    assert counts["unchecked"] == 5 and counts["not_found"] == 0
    assert counts["text_layer"] is False


def test_quotes_with_an_ellipsis_match_both_parts():
    quote = "MASTER SERVICES AGREEMENT ... successive periods of 24 months"
    report = {"risks": [{"page": 1, "quote": quote}]}
    verify_quotes(report, PAGES)
    assert report["risks"][0]["verified"] == "verified"


# ---------- invoice arithmetic ----------

def test_invoice_arithmetic_finds_a_wrong_total_and_a_wrong_line():
    report = {
        "line_items": [
            {"description": "Hosting", "quantity": 1, "unit_price": 4800, "amount": 4800, "page": 1},
            {"description": "Support hours", "quantity": 12, "unit_price": 85, "amount": 1200, "page": 1},
        ],
        "totals": {"currency": "EUR", "subtotal": 6000, "tax": 1200, "total": 7400},
    }

    checks = {c["check"]: c for c in check_arithmetic(report)}

    assert checks["Line: Support hours"]["expected"] == 1020
    assert checks["Sum of lines = subtotal"]["status"] == "ok"
    assert checks["Subtotal + tax = total"] == {
        "check": "Subtotal + tax = total", "status": "mismatch", "expected": 7200, "found": 7400}


def test_invoice_arithmetic_tolerates_rounding_to_cents():
    report = {"line_items": [{"description": "x", "quantity": 3, "unit_price": 33.333, "amount": 100.0,
                              "page": 1}],
              "totals": {"currency": None, "subtotal": 100.0, "tax": 20.0, "total": 120.0}}
    assert all(c["status"] == "ok" for c in check_arithmetic(report))


# ---------- API ----------

def test_review_is_streamed_verified_saved_and_reused(make_client, tmp_path):
    http, fake = make_client(review_message(REPORT))
    doc = upload(http, contract_pdf(tmp_path)).json()

    response = http.post(f"/api/documents/{doc['id']}/review",
                         json={"playbook": "contract", "language": "Bulgarian"})
    name, review = events(response)[-1]

    assert name == "done"
    risks = review["report"]["risks"]
    assert [r["severity"] for r in risks] == ["high", "high", "medium"]  # most severe first
    assert {r["title"]: r["verified"] for r in risks}["Penalty"] == "not_found"
    assert review["report"]["verification"]["not_found"] == 1
    assert review["cost_usd"] > 0

    request = fake.requests[0]
    document = request["messages"][0]["content"][0]
    assert document["source"]["type"] == "file"
    assert "citations" not in document  # not allowed together with a JSON schema
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "Bulgarian" in request["messages"][0]["content"][1]["text"]

    again = http.post(f"/api/documents/{doc['id']}/review",
                      json={"playbook": "contract", "language": "Bulgarian"}).json()
    assert again["cached"] is True and again["id"] == review["id"]
    assert len(fake.requests) == 1  # served from the database
    assert [r["id"] for r in http.get(f"/api/documents/{doc['id']}/reviews").json()] == [review["id"]]


def test_an_unusable_review_reports_an_error_and_saves_nothing(make_client, tmp_path):
    truncated = review_message(REPORT)
    truncated.stop_reason = "max_tokens"
    http, _ = make_client(truncated)
    doc = upload(http, contract_pdf(tmp_path)).json()

    name, data = events(http.post(f"/api/documents/{doc['id']}/review", json={"playbook": "general"}))[-1]

    assert name == "error" and "too long" in data["message"]
    assert http.get(f"/api/documents/{doc['id']}/reviews").json() == []


def test_unknown_review_type_is_rejected(make_client, tmp_path):
    http, fake = make_client()
    doc = upload(http, contract_pdf(tmp_path)).json()
    response = http.post(f"/api/documents/{doc['id']}/review", json={"playbook": "anything"})
    assert response.status_code == 422
    assert fake.requests == []


def test_reviews_count_towards_the_daily_budget(make_client, tmp_path):
    http, _ = make_client(review_message(REPORT), daily_budget_per_user=0.01)
    doc = upload(http, contract_pdf(tmp_path)).json()

    http.post(f"/api/documents/{doc['id']}/review", json={"playbook": "contract"})
    assert http.get("/api/me").json()["spent_today_usd"] > 0.01

    blocked = http.post(f"/api/documents/{doc['id']}/review", json={"playbook": "tender"})
    assert blocked.status_code == 429


def test_exports_are_excel_safe_csv_and_a_calendar(make_client, tmp_path):
    report = copy.deepcopy(REPORT)
    report["missing"] = [{"item": "=HYPERLINK(\"http://evil\")", "why_it_matters": "formula injection"}]
    http, _ = make_client(review_message(report))
    doc = upload(http, contract_pdf(tmp_path)).json()
    review = events(http.post(f"/api/documents/{doc['id']}/review", json={"playbook": "contract"}))[-1][1]

    csv_response = http.get(f"/api/reviews/{review['id']}/export.csv")
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert csv_response.text.startswith("﻿Section,")
    assert "'=HYPERLINK" in csv_response.text  # not run as a formula by Excel
    assert "Uncapped liability" in csv_response.text

    ics = http.get(f"/api/reviews/{review['id']}/deadlines.ics").text
    assert "DTSTART;VALUE=DATE:20280715" in ics
    assert "SUMMARY:Last day to cancel renewal" in ics


def test_users_cannot_see_each_others_reviews(make_client, tmp_path):
    http, _ = make_client(review_message(REPORT), auth_mode="easyauth")
    doc = upload(http, contract_pdf(tmp_path), headers=ALICE).json()
    review = events(http.post(f"/api/documents/{doc['id']}/review", json={"playbook": "contract"},
                              headers=ALICE))[-1][1]

    for path in (f"/api/reviews/{review['id']}", f"/api/reviews/{review['id']}/export.csv",
                 f"/api/reviews/{review['id']}/deadlines.ics", f"/api/documents/{doc['id']}/reviews"):
        assert http.get(path, headers=BOB).status_code == 404
    assert http.post(f"/api/documents/{doc['id']}/review", json={"playbook": "contract"},
                     headers=BOB).status_code == 404


def test_deleting_a_document_deletes_its_reviews(make_client, tmp_path):
    http, _ = make_client(review_message(REPORT))
    doc = upload(http, contract_pdf(tmp_path)).json()
    review = events(http.post(f"/api/documents/{doc['id']}/review", json={"playbook": "contract"}))[-1][1]

    http.delete(f"/api/documents/{doc['id']}")

    assert http.get(f"/api/reviews/{review['id']}").status_code == 404


@pytest.mark.parametrize("sample", ["service-agreement.pdf", "invoice-2026-114.pdf"])
def test_sample_pdfs_are_up_to_date(sample):
    """samples/*.pdf are generated; rebuild them with `python samples/build.py` after editing."""
    from samples import build

    pages = build.SERVICE_AGREEMENT if sample.startswith("service") else build.INVOICE
    assert (build.HERE / sample).read_bytes() == text_pdf(pages)
