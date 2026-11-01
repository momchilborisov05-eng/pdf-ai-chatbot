"""
PDF AI Assistant - web service.

A FastAPI app around the same conversation engine as the command line:
upload PDFs, chat with them with streamed answers and page citations, run a
document review (contract, invoice, tender) with verified evidence, fill in
questionnaires from a library of approved answers, reuse summaries, search
history and see what it cost. Built to run in a container
on Azure Container Apps:

- Sign-in is handled by the platform (Container Apps / App Service "Easy Auth"
  with Microsoft Entra ID); the app reads the signed-in user from the headers
  the platform sets. Every document, session and search is scoped to that user.
- PDFs are uploaded once to the Claude Files API; later requests send only the
  file id, so a question about a 30 MB PDF doesn't re-upload 30 MB.
- Each user has a daily spending limit, and there is one for the whole app,
  so a public URL can't run up the API bill.
- History lives in Azure SQL (Managed Identity, no password) or SQLite.

Run locally:
    uvicorn web.app:create_app --factory --reload
"""

import base64
import csv
import io
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import questionnaire
from pdf_chat import (
    MAX_PDF_BYTES,
    MODEL,
    PdfChat,
    PdfError,
    PdfFile,
    UploadedPdf,
    summary_prompt,
)
from questionnaire import LibraryError, parse_library, run_autofill
from review import PLAYBOOKS, ReviewError, run_review, to_csv_rows, to_ics
from storage import DEFAULT_URL, Storage, StoragePool, to_datetime

STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger("pdf_chat.web")


@dataclass
class Settings:
    database_url: str = DEFAULT_URL
    # "none": single local user (development). "easyauth": the platform signs users
    # in and passes the identity in X-MS-CLIENT-PRINCIPAL-* headers.
    auth_mode: str = "none"
    # Optional allowlist of sign-in names (emails); empty means any signed-in user.
    allowed_users: set[str] = field(default_factory=set)
    daily_budget_per_user: float = 1.00
    daily_budget_total: float = 5.00
    pool_size: int = 4
    # Canned answers with real citations, for trying the app without an API key.
    demo: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        allowed = os.environ.get("ALLOWED_USERS", "")
        return cls(
            database_url=os.environ.get("PDF_CHAT_DB", DEFAULT_URL),
            auth_mode=os.environ.get("AUTH_MODE", "none").lower(),
            allowed_users={u.strip().lower() for u in allowed.split(",") if u.strip()},
            daily_budget_per_user=float(os.environ.get("DAILY_BUDGET_USD", "1.00")),
            daily_budget_total=float(os.environ.get("DAILY_BUDGET_TOTAL_USD", "5.00")),
            pool_size=int(os.environ.get("DB_POOL_SIZE", "4")),
            demo=os.environ.get("DEMO_MODE") == "1",
        )


@dataclass
class User:
    id: str  # stable owner key stored with every document
    name: str


# ---------- request / response models ----------

class ChatRequest(BaseModel):
    document_id: int
    question: str = Field(min_length=1, max_length=4000)
    session_id: int | None = None


class SummaryRequest(BaseModel):
    language: str = Field(default="English", min_length=2, max_length=40)
    refresh: bool = False


class ReviewRequest(BaseModel):
    playbook: str = Field(default="general", pattern="^(" + "|".join(PLAYBOOKS) + ")$")
    language: str = Field(default="English", min_length=2, max_length=40)
    refresh: bool = False


class AutofillRequest(BaseModel):
    language: str = Field(default="English", min_length=2, max_length=40)
    refresh: bool = False


# ---------- app factory ----------

def create_app(settings: Settings | None = None,
               client_factory: Callable[[], anthropic.Anthropic] | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.demo and client_factory is None:
        from web.demo import DemoClient
        client_factory = DemoClient
    if settings.auth_mode not in {"none", "easyauth"}:
        raise ValueError("AUTH_MODE must be 'none' or 'easyauth'")
    pool = StoragePool(settings.database_url, settings.pool_size)
    _client: list[anthropic.Anthropic] = []

    def claude() -> anthropic.Anthropic:
        if not _client:
            if client_factory:
                _client.append(client_factory())
            elif not os.environ.get("ANTHROPIC_API_KEY"):
                raise HTTPException(503, "The server has no Claude API key configured.")
            else:
                _client.append(anthropic.Anthropic(max_retries=3))
        return _client[0]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        pool.close()

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    app = FastAPI(title="PDF AI Assistant", version="2.0.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.pool = pool
    app.state.settings = settings

    # ---------- middleware ----------

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.url.path not in {"/healthz", "/readyz"}:
            log.info(json.dumps({
                "request_id": request_id, "method": request.method,
                "path": request.url.path, "status": response.status_code,
                "ms": int((time.monotonic() - started) * 1000),
            }))
        return response

    # ---------- dependencies ----------

    def current_user(request: Request) -> User:
        if settings.auth_mode == "none":
            return User("local", "local")
        # Set by the platform after sign-in; it strips these headers from client requests.
        user_id = request.headers.get("x-ms-client-principal-id")
        name = request.headers.get("x-ms-client-principal-name", "")
        if not user_id:
            raise HTTPException(401, "Sign in required.")
        if settings.allowed_users and name.lower() not in settings.allowed_users:
            raise HTTPException(403, "Your account is not on this app's access list.")
        return User(user_id, name or _name_from_principal(request) or user_id)

    def db():
        with pool.connection() as store:
            yield store

    def check_budget(store: Storage, user: User):
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if store.spent_since(today, user.id) >= settings.daily_budget_per_user:
            raise HTTPException(429, f"You've reached today's limit of "
                                     f"${settings.daily_budget_per_user:.2f}. It resets at 00:00 UTC.")
        if store.spent_since(today) >= settings.daily_budget_total:
            raise HTTPException(429, "The app has reached today's overall spending limit. "
                                     "Try again tomorrow.")

    def owned_document(store: Storage, user: User, doc_id: int) -> dict:
        doc = store.get_document(doc_id, user.id)
        if not doc:  # someone else's document looks exactly like a missing one
            raise HTTPException(404, "Document not found.")
        return doc

    # ---------- health ----------

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(store: Storage = Depends(db)):
        if not store.ping():
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        return {"status": "ready", "database": store.dialect, "model": MODEL}

    # ---------- user ----------

    @app.get("/api/me")
    def me(user: User = Depends(current_user), store: Storage = Depends(db)):
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "name": user.name,
            "auth": settings.auth_mode,
            "demo": settings.demo,
            "model": MODEL,
            "spent_today_usd": round(store.spent_since(today, user.id), 4),
            "daily_budget_usd": settings.daily_budget_per_user,
        }

    # ---------- documents ----------

    @app.get("/api/documents")
    def list_documents(user: User = Depends(current_user), store: Storage = Depends(db)):
        return [_jsonable(d) for d in store.list_documents(user.id)]

    @app.post("/api/documents", status_code=201)
    def upload_document(file: UploadFile = File(...), user: User = Depends(current_user),
                        store: Storage = Depends(db)):
        data = file.file.read(MAX_PDF_BYTES + 1)
        name = Path(file.filename or "document.pdf").name[:200]
        if len(data) > MAX_PDF_BYTES:
            raise HTTPException(413, "PDF is too large. The limit is 32 MB.")
        try:
            pdf = PdfFile(data, name)
        except PdfError as e:
            raise HTTPException(422, str(e)) from None

        doc_id = store.upsert_document(pdf.sha256, name, f"upload:{name}", len(data),
                                       pdf.page_count, owner=user.id)
        doc = store.get_document(doc_id)
        if not doc["file_id"]:
            try:
                uploaded = claude().files.upload(file=(doc["filename"], data, "application/pdf"))
            except anthropic.APIError as e:
                log.warning("Files API upload failed: %s", e)
                raise HTTPException(502, "Could not store the PDF with Claude. Try again.") from None
            store.set_file_id(doc_id, uploaded.id)
        store.save_pages(doc_id, pdf.page_texts())
        return _jsonable(next(d for d in store.list_documents(user.id) if d["id"] == doc_id))

    @app.delete("/api/documents/{doc_id}", status_code=204)
    def delete_document(doc_id: int, user: User = Depends(current_user),
                        store: Storage = Depends(db)):
        doc = store.delete_document(doc_id, user.id)
        if not doc:
            raise HTTPException(404, "Document not found.")
        if doc["file_id"]:
            try:
                claude().files.delete(doc["file_id"])
            except (anthropic.NotFoundError, HTTPException):
                pass
            except anthropic.APIError as e:  # the history is gone either way; log the orphan
                log.warning("Could not delete file %s: %s", doc["file_id"], e)

    @app.get("/api/documents/{doc_id}/sessions")
    def document_sessions(doc_id: int, user: User = Depends(current_user),
                          store: Storage = Depends(db)):
        owned_document(store, user, doc_id)
        return [_jsonable(s) for s in store.list_sessions(doc_id, 100, owner=user.id)]

    # ---------- sessions ----------

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: int, user: User = Depends(current_user),
                    store: Storage = Depends(db)):
        session = store.get_session(session_id, user.id)
        if not session:
            raise HTTPException(404, "Session not found.")
        messages = []
        for m in store.get_messages(session_id):
            item = {"role": m["role"], "text": m["text"], "created_at": _iso(m["created_at"])}
            if m["role"] == "assistant":
                item["citations"] = [c.__dict__ for c in store.get_citations(m["id"])]
                item["cost_usd"] = float(m["cost_usd"]) if m["cost_usd"] is not None else None
                item["model"] = m["model"]
            messages.append(item)
        return {
            "id": session["id"], "document_id": session["document_id"],
            "kind": session["kind"], "language": session["language"],
            "filename": session["filename"], "messages": messages,
        }

    # ---------- chat (streamed) ----------

    @app.post("/api/chat")
    def chat(body: ChatRequest, user: User = Depends(current_user)):
        # The connection is held for the whole stream, so it's taken here, not via Depends.
        with pool.connection() as store:
            doc = owned_document(store, user, body.document_id)
            if body.session_id is not None:
                session = store.get_session(body.session_id, user.id)
                if not session or session["document_id"] != doc["id"]:
                    raise HTTPException(404, "Session not found.")
            check_budget(store, user)
            if not doc["file_id"]:
                raise HTTPException(409, "This document needs to be uploaded again.")
            client = claude()
        return _sse(pool, lambda store: PdfChat(
            client, store, UploadedPdf(doc["file_id"]), doc["id"],
            session_id=body.session_id,
        ).stream(body.question))

    @app.post("/api/documents/{doc_id}/summary")
    def summary(doc_id: int, body: SummaryRequest, user: User = Depends(current_user)):
        with pool.connection() as store:
            doc = owned_document(store, user, doc_id)
            saved = None if body.refresh else store.latest_session(doc_id, "summary", body.language)
            if saved:
                answer = [m for m in store.get_messages(saved["id"]) if m["role"] == "assistant"][-1]
                return {"cached": True, "session_id": saved["id"], "text": answer["text"],
                        "citations": [c.__dict__ for c in store.get_citations(answer["id"])],
                        "created_at": _iso(answer["created_at"])}
            check_budget(store, user)
            if not doc["file_id"]:
                raise HTTPException(409, "This document needs to be uploaded again.")
            client = claude()
        return _sse(pool, lambda store: PdfChat(
            client, store, UploadedPdf(doc["file_id"]), doc["id"], "summary", body.language,
        ).stream(summary_prompt(body.language)))

    # ---------- document review ----------

    @app.get("/api/playbooks")
    def playbooks():
        return [{"id": key, "label": p["label"]} for key, p in PLAYBOOKS.items()] + [
            {"id": questionnaire.PLAYBOOK, "label": "Questionnaire (answer from library)"}]

    @app.get("/api/documents/{doc_id}/reviews")
    def document_reviews(doc_id: int, user: User = Depends(current_user),
                         store: Storage = Depends(db)):
        owned_document(store, user, doc_id)
        return [_jsonable(r) for r in store.list_reviews(doc_id)]

    @app.post("/api/documents/{doc_id}/review")
    def review(doc_id: int, body: ReviewRequest, user: User = Depends(current_user)):
        """A saved review (JSON) or a new one (SSE: progress*, then done or error)."""
        with pool.connection() as store:
            doc = owned_document(store, user, doc_id)
            saved = None if body.refresh else store.latest_review(doc_id, body.playbook, body.language)
            if saved:
                return {"cached": True, **_review_json(saved)}
            check_budget(store, user)
            if not doc["file_id"]:
                raise HTTPException(409, "This document needs to be uploaded again.")
            client = claude()

        return _report_stream(pool, lambda store: run_review(
            client, store, UploadedPdf(doc["file_id"]), doc_id, body.playbook, body.language))

    # ---------- questionnaires and the answer library ----------

    @app.get("/api/library")
    def library(user: User = Depends(current_user), store: Storage = Depends(db)):
        entries = store.list_library(user.id)
        return {"count": len(entries), "entries": [_jsonable(e) for e in entries[:1000]]}

    @app.post("/api/library")
    def import_library(file: UploadFile = File(...), user: User = Depends(current_user),
                       store: Storage = Depends(db)):
        """Replace the user's approved answers with the rows of an Excel or CSV file."""
        data = file.file.read(questionnaire.MAX_LIBRARY_FILE_BYTES + 1)
        try:
            entries = parse_library(file.filename or "", data)
        except LibraryError as e:
            raise HTTPException(422, str(e)) from None
        return {"count": store.replace_library(user.id, entries)}

    @app.get("/api/library/template.csv")
    def library_template():
        return Response("\ufeff" + questionnaire.LIBRARY_TEMPLATE, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="answer-library.csv"'})

    @app.post("/api/documents/{doc_id}/autofill")
    def autofill(doc_id: int, body: AutofillRequest, user: User = Depends(current_user)):
        """Saved answers (JSON) if the library hasn't changed since, else a new run (SSE)."""
        with pool.connection() as store:
            doc = owned_document(store, user, doc_id)
            library = store.list_library(user.id)
            if not library:
                raise HTTPException(409, "The answer library is empty. Import your approved answers first.")
            saved = None if body.refresh else store.latest_review(doc_id, questionnaire.PLAYBOOK,
                                                                  body.language)
            if saved and saved["report"]["library"]["fingerprint"] == questionnaire.fingerprint(library):
                return {"cached": True, **_review_json(saved)}
            check_budget(store, user)
            if not doc["file_id"]:
                raise HTTPException(409, "This document needs to be uploaded again.")
            client = claude()
        return _report_stream(pool, lambda store: run_autofill(
            client, store, UploadedPdf(doc["file_id"]), doc_id, user.id, body.language))

    @app.get("/api/reviews/{review_id}")
    def get_review(review_id: int, user: User = Depends(current_user), store: Storage = Depends(db)):
        return _review_json(owned_review(store, user, review_id))

    @app.get("/api/reviews/{review_id}/export.csv")
    def export_review(review_id: int, user: User = Depends(current_user),
                      store: Storage = Depends(db)):
        saved = owned_review(store, user, review_id)
        rows = (questionnaire.to_rows if saved["playbook"] == questionnaire.PLAYBOOK else to_csv_rows)
        out = io.StringIO()
        writer = csv.writer(out)
        for row in rows(saved["report"]):
            # A leading = + - @ would make Excel run the cell as a formula.
            writer.writerow(["'" + c if isinstance(c, str) and c.startswith(("=", "+", "-", "@")) else c
                             for c in row])
        # The byte order mark makes Excel read the file as UTF-8 (Cyrillic, accents).
        return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers=_download(saved, "csv"))

    @app.get("/api/reviews/{review_id}/answers.xlsx")
    def export_answers(review_id: int, user: User = Depends(current_user),
                       store: Storage = Depends(db)):
        saved = owned_review(store, user, review_id)
        if saved["playbook"] != questionnaire.PLAYBOOK:
            raise HTTPException(404, "Only questionnaires have an answer sheet.")
        return Response(questionnaire.to_xlsx(saved["report"]), headers=_download(saved, "xlsx"),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/reviews/{review_id}/deadlines.ics")
    def export_deadlines(review_id: int, user: User = Depends(current_user),
                         store: Storage = Depends(db)):
        saved = owned_review(store, user, review_id)
        return Response(to_ics(saved["report"], saved["filename"], review_id),
                        media_type="text/calendar; charset=utf-8", headers=_download(saved, "ics"))

    def owned_review(store: Storage, user: User, review_id: int) -> dict:
        saved = store.get_review(review_id, user.id)
        if not saved:
            raise HTTPException(404, "Review not found.")
        return saved

    # ---------- history ----------

    @app.get("/api/search")
    def search(q: str, user: User = Depends(current_user), store: Storage = Depends(db)):
        q = q.strip()
        if len(q) < 2:
            raise HTTPException(422, "Search for at least 2 characters.")
        return [_jsonable(r) for r in store.search(q[:200], 50, owner=user.id)]

    @app.get("/api/stats")
    def stats(user: User = Depends(current_user), store: Storage = Depends(db)):
        s = store.stats(user.id)
        return {
            "per_document": [_jsonable(r) for r in s["per_document"]],
            "per_model": [_jsonable(r) for r in s["per_model"]],
            "total_cost_usd": round(s["total_cost_usd"], 6),
            "cache_hit_rate": s["cache_hit_rate"],
        }

    # ---------- web page ----------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


# ---------- streaming ----------

def _sse(pool: StoragePool, start: Callable[[Storage], Iterator[tuple[str, object]]]):
    """Stream a PdfChat answer as server-sent events: delta*, then done or error."""

    def events():
        with pool.connection() as store:
            try:
                for kind, value in start(store):
                    if kind == "text":
                        yield _event("delta", {"text": value})
                    else:
                        yield _event("done", _turn_json(value))
            except anthropic.APIError as e:
                yield _event("error", {"message": _api_error_message(e)})
            except Exception:
                log.exception("chat failed")
                yield _event("error", {"message": "Something went wrong. Nothing was saved."})

    return _event_stream(events())


def _report_stream(pool: StoragePool, start: Callable[[Storage], Iterator[tuple[str, object]]]):
    """Stream a review or questionnaire run as server-sent events: progress*, then done or error."""

    def events():
        with pool.connection() as store:
            try:
                for kind, value in start(store):
                    if kind == "progress":
                        yield _event("progress", {"characters": value})
                    else:
                        yield _event("done", _review_json(store.get_review(value.review_id)))
            except (ReviewError, LibraryError) as e:
                yield _event("error", {"message": str(e)})
            except anthropic.APIError as e:
                yield _event("error", {"message": _api_error_message(e)})
            except Exception:
                log.exception("review failed")
                yield _event("error", {"message": "Something went wrong. Nothing was saved."})

    return _event_stream(events())


def _event_stream(events: Iterator[str]) -> StreamingResponse:
    return StreamingResponse(events, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _api_error_message(e: anthropic.APIError) -> str:
    if isinstance(e, anthropic.RateLimitError):
        return "Claude is busy right now. Try again in a minute."
    if isinstance(e, anthropic.NotFoundError):
        return "The stored PDF is missing. Delete it and upload again."
    if isinstance(e, anthropic.APIStatusError):
        log.warning("Claude API error %s: %s", e.status_code, e.message)
        return f"Claude API error ({e.status_code})."
    return "Could not reach the Claude API."


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _turn_json(turn) -> dict:
    usage = turn.usage
    return {
        "session_id": turn.session_id,
        "refused": turn.refused,
        "truncated": turn.truncated,
        "text": turn.text,
        "citations": [c.__dict__ for c in turn.citations],
        "usage": None if usage is None else {
            "model": usage.model,
            "input_tokens": usage.input_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "latency_ms": usage.latency_ms,
        },
    }


def _review_json(saved: dict) -> dict:
    return {
        "id": saved["id"], "document_id": saved["document_id"], "filename": saved["filename"],
        "playbook": saved["playbook"], "language": saved["language"],
        "created_at": _iso(saved["created_at"]), "model": saved["model"],
        "cost_usd": float(saved["cost_usd"]) if saved["cost_usd"] is not None else None,
        "latency_ms": saved["latency_ms"],
        "report": saved["report"],
    }


def _download(saved: dict, extension: str) -> dict:
    # Header values must be ASCII; non-Latin file names become dashes.
    stem = "".join(c if (c.isascii() and c.isalnum()) or c in "-_" else "-"
                   for c in Path(saved["filename"]).stem)[:60]
    return {"Content-Disposition": f'attachment; filename="review-{saved["id"]}-{stem}.{extension}"'}


# ---------- helpers ----------

def _iso(value) -> str:
    return to_datetime(value).replace(tzinfo=timezone.utc).isoformat()


def _jsonable(row: dict) -> dict:
    """Database row -> JSON: timestamps as ISO 8601, DECIMAL as float."""
    out = {}
    for key, value in row.items():
        if key.endswith("_at") and value is not None:
            out[key] = _iso(value)
        elif hasattr(value, "is_finite"):  # decimal.Decimal from SQL Server
            out[key] = float(value)
        else:
            out[key] = value
    return out


def _name_from_principal(request: Request) -> str | None:
    """The display name from X-MS-CLIENT-PRINCIPAL (base64 JSON), when the name header is empty."""
    raw = request.headers.get("x-ms-client-principal")
    if not raw:
        return None
    try:
        claims = json.loads(base64.b64decode(raw))["claims"]
    except (ValueError, KeyError, TypeError):
        return None
    return next((c["val"] for c in claims if c.get("typ") == "name"), None)

