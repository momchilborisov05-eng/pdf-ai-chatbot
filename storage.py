"""
Persistent storage for PDF AI Assistant.

Keeps a library of documents with their page text, chat sessions, every
question and answer, document reviews, token usage with cost, the page
citations behind each answer, and each owner's library of approved answers
(used to fill in questionnaires).

Every document belongs to an owner (the signed-in user in the web app, "local"
for the command line), and every read is scoped to that owner.

Two backends share one interface:
    sqlite:///pdf_chat.db                         local file (default)
    mssql://<server>.database.windows.net/<db>    Azure SQL Database, signed in
                                                  with Microsoft Entra ID (no password)

Both drivers accept "?" placeholders, so queries are written once. Only the
DDL, the "return the new id" clause and row limits differ per dialect.
"""

import json
import queue
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

DEFAULT_URL = "sqlite:///pdf_chat.db"
LOCAL_OWNER = "local"  # owner of everything created from the command line

SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS documents (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        owner       TEXT    NOT NULL DEFAULT 'local',
        sha256      TEXT    NOT NULL,
        filename    TEXT    NOT NULL,
        path        TEXT    NOT NULL,
        size_bytes  INTEGER NOT NULL,
        page_count  INTEGER,
        file_id     TEXT,
        created_at  TEXT    NOT NULL,
        UNIQUE (owner, sha256)
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        kind         TEXT    NOT NULL CHECK (kind IN ('chat', 'summary')),
        language     TEXT,
        created_at   TEXT    NOT NULL,
        updated_at   TEXT    NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id          INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        seq                 INTEGER NOT NULL,
        role                TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
        text                TEXT    NOT NULL,
        content_json        TEXT,
        model               TEXT,
        input_tokens        INTEGER,
        cache_write_tokens  INTEGER,
        cache_read_tokens   INTEGER,
        output_tokens       INTEGER,
        cost_usd            REAL,
        latency_ms          INTEGER,
        created_at          TEXT    NOT NULL,
        UNIQUE (session_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS citations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        start_page  INTEGER NOT NULL,
        end_page    INTEGER NOT NULL,
        cited_text  TEXT    NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS pages (
        document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        page         INTEGER NOT NULL,
        text         TEXT    NOT NULL,
        PRIMARY KEY (document_id, page)
    )""",
    """CREATE TABLE IF NOT EXISTS reviews (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id         INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        playbook            TEXT    NOT NULL,
        language            TEXT    NOT NULL,
        report_json         TEXT    NOT NULL,
        overall_risk        TEXT,
        model               TEXT,
        input_tokens        INTEGER,
        cache_write_tokens  INTEGER,
        cache_read_tokens   INTEGER,
        output_tokens       INTEGER,
        cost_usd            REAL,
        latency_ms          INTEGER,
        created_at          TEXT    NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS library (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        owner       TEXT    NOT NULL,
        question    TEXT    NOT NULL,
        answer      TEXT    NOT NULL,
        source      TEXT,
        updated_at  TEXT    NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_library_owner ON library(owner)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_document ON sessions(document_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS ix_reviews_document ON reviews(document_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_citations_message ON citations(message_id)",
    "CREATE INDEX IF NOT EXISTS ix_messages_created ON messages(created_at)",
]

MSSQL_SCHEMA = [
    """IF OBJECT_ID('documents', 'U') IS NULL CREATE TABLE documents (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        owner       NVARCHAR(200)  NOT NULL DEFAULT 'local',
        sha256      CHAR(64)       NOT NULL,
        filename    NVARCHAR(400)  NOT NULL,
        path        NVARCHAR(1000) NOT NULL,
        size_bytes  BIGINT         NOT NULL,
        page_count  INT,
        file_id     VARCHAR(100),
        created_at  DATETIME2(0)   NOT NULL,
        CONSTRAINT uq_documents_owner_sha256 UNIQUE (owner, sha256)
    )""",
    """IF OBJECT_ID('sessions', 'U') IS NULL CREATE TABLE sessions (
        id           INT IDENTITY(1,1) PRIMARY KEY,
        document_id  INT           NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        kind         VARCHAR(10)   NOT NULL CHECK (kind IN ('chat', 'summary')),
        language     NVARCHAR(50),
        created_at   DATETIME2(0)  NOT NULL,
        updated_at   DATETIME2(0)  NOT NULL
    )""",
    """IF OBJECT_ID('messages', 'U') IS NULL CREATE TABLE messages (
        id                  INT IDENTITY(1,1) PRIMARY KEY,
        session_id          INT            NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        seq                 INT            NOT NULL,
        role                VARCHAR(10)    NOT NULL CHECK (role IN ('user', 'assistant')),
        text                NVARCHAR(MAX)  NOT NULL,
        content_json        NVARCHAR(MAX),
        model               VARCHAR(100),
        input_tokens        INT,
        cache_write_tokens  INT,
        cache_read_tokens   INT,
        output_tokens       INT,
        cost_usd            DECIMAL(12, 6),
        latency_ms          INT,
        created_at          DATETIME2(0)   NOT NULL,
        CONSTRAINT uq_messages_session_seq UNIQUE (session_id, seq)
    )""",
    """IF OBJECT_ID('citations', 'U') IS NULL CREATE TABLE citations (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        message_id  INT            NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        start_page  INT            NOT NULL,
        end_page    INT            NOT NULL,
        cited_text  NVARCHAR(MAX)  NOT NULL
    )""",
    """IF OBJECT_ID('pages', 'U') IS NULL CREATE TABLE pages (
        document_id  INT            NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        page         INT            NOT NULL,
        text         NVARCHAR(MAX)  NOT NULL,
        CONSTRAINT pk_pages PRIMARY KEY (document_id, page)
    )""",
    """IF OBJECT_ID('reviews', 'U') IS NULL CREATE TABLE reviews (
        id                  INT IDENTITY(1,1) PRIMARY KEY,
        document_id         INT            NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        playbook            VARCHAR(20)    NOT NULL,
        language            NVARCHAR(50)   NOT NULL,
        report_json         NVARCHAR(MAX)  NOT NULL,
        overall_risk        VARCHAR(10),
        model               VARCHAR(100),
        input_tokens        INT,
        cache_write_tokens  INT,
        cache_read_tokens   INT,
        output_tokens       INT,
        cost_usd            DECIMAL(12, 6),
        latency_ms          INT,
        created_at          DATETIME2(0)   NOT NULL
    )""",
    """IF OBJECT_ID('library', 'U') IS NULL CREATE TABLE library (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        owner       NVARCHAR(200)  NOT NULL,
        question    NVARCHAR(MAX)  NOT NULL,
        answer      NVARCHAR(MAX)  NOT NULL,
        source      NVARCHAR(400),
        updated_at  DATETIME2(0)   NOT NULL
    )""",
    """IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_library_owner')
        CREATE INDEX ix_library_owner ON library(owner)""",
    """IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_reviews_document')
        CREATE INDEX ix_reviews_document ON reviews(document_id, created_at)""",
    """IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_sessions_document')
        CREATE INDEX ix_sessions_document ON sessions(document_id, updated_at)""",
    """IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_citations_message')
        CREATE INDEX ix_citations_message ON citations(message_id)""",
    """IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_messages_created')
        CREATE INDEX ix_messages_created ON messages(created_at)""",
]

# Databases created before documents had owners: add the columns in place.
# The old UNIQUE(sha256) stays, which is fine for the single-user CLI they came from.
MIGRATIONS = {
    "owner": "ALTER TABLE documents ADD owner {text} NOT NULL DEFAULT 'local'",
    "file_id": "ALTER TABLE documents ADD file_id {short}",
}


# Every paid model call, chat answers and reviews alike: the basis for budgets and reports.
BILLED_CALLS = """(
    SELECT s.document_id, m.model, m.input_tokens, m.cache_write_tokens, m.cache_read_tokens,
           m.output_tokens, m.cost_usd, m.latency_ms, m.created_at
    FROM messages m JOIN sessions s ON s.id = m.session_id
    WHERE m.role = 'assistant'
    UNION ALL
    SELECT document_id, model, input_tokens, cache_write_tokens, cache_read_tokens,
           output_tokens, cost_usd, latency_ms, created_at
    FROM reviews
)"""


@dataclass
class Usage:
    """Token usage and estimated cost of one answer."""

    model: str
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int


@dataclass
class Citation:
    start_page: int  # 1-indexed, inclusive
    end_page: int  # 1-indexed, inclusive
    cited_text: str


class Storage:
    """Repository for documents, sessions, messages and citations."""

    def __init__(self, url: str = DEFAULT_URL):
        self.url = url
        parsed = urlparse(url)
        if parsed.scheme == "sqlite":
            self.dialect = "sqlite"
            # sqlite:///relative.db -> "relative.db", sqlite:///:memory: -> ":memory:"
            # check_same_thread=False: a pooled connection is used by one request at
            # a time, but not always on the thread that opened it.
            self.conn = sqlite3.connect(parsed.path.lstrip("/") or ":memory:",
                                        check_same_thread=False, timeout=15)
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
        elif parsed.scheme == "mssql":
            self.dialect = "mssql"
            database = parsed.path.lstrip("/")
            if not parsed.hostname or not database:
                raise ValueError("Use mssql://<server>.database.windows.net/<database>")
            self.conn = _connect_mssql(
                f"Server=tcp:{parsed.hostname},{parsed.port or 1433};Database={database};"
                "Encrypt=yes;TrustServerCertificate=no;"
                # Azure CLI login on a laptop, Managed Identity when hosted in Azure.
                "Authentication=ActiveDirectoryDefault"
            )
        else:
            raise ValueError(f"Unsupported database URL: {url}")
        self._create_schema()

    # ---------- low-level helpers ----------

    def _create_schema(self):
        cur = self.conn.cursor()
        for statement in SQLITE_SCHEMA if self.dialect == "sqlite" else MSSQL_SCHEMA:
            cur.execute(statement)
        cur.execute("SELECT * FROM documents WHERE 1 = 0")
        existing = {col[0] for col in cur.description}
        types = ({"text": "TEXT", "short": "TEXT"} if self.dialect == "sqlite"
                 else {"text": "NVARCHAR(200)", "short": "VARCHAR(100)"})
        for column, statement in MIGRATIONS.items():
            if column not in existing:
                cur.execute(statement.format(**types))
        self.conn.commit()

    def ping(self) -> bool:
        """True when the connection still works (serverless databases drop idle ones)."""
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            return True
        except Exception:
            return False

    def _now(self):
        now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
        return now.isoformat(sep=" ") if self.dialect == "sqlite" else now

    def _insert(self, cur, table: str, values: dict) -> int:
        """Insert one row and return its generated id."""
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        if self.dialect == "sqlite":
            sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) RETURNING id"
        else:
            sql = f"INSERT INTO {table} ({columns}) OUTPUT INSERTED.id VALUES ({placeholders})"
        cur.execute(sql, tuple(values.values()))
        return int(cur.fetchone()[0])

    def _limit(self, n: int) -> tuple[str, str]:
        """Return (prefix after SELECT, suffix at the end) that limit rows to n."""
        n = int(n)
        return (f"TOP ({n}) ", "") if self.dialect == "mssql" else ("", f" LIMIT {n}")

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(sql, params)
        names = [col[0] for col in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    def close(self):
        self.conn.close()

    # ---------- documents ----------

    def upsert_document(self, sha256: str, filename: str, path: str,
                        size_bytes: int, page_count: int | None,
                        owner: str = LOCAL_OWNER) -> int:
        """Return the document id, adding the PDF to the library on first sight.

        The same file under a new name or location is the same document (matched
        by content hash), so its history is kept. Only the path is updated: the
        filename stays as first seen, because it is the document title sent to
        the API and saved conversations must replay byte-for-byte.
        """
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM documents WHERE owner = ? AND sha256 = ?", (owner, sha256))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE documents SET path = ? WHERE id = ?", (path, row[0]))
            doc_id = int(row[0])
        else:
            doc_id = self._insert(cur, "documents", {
                "owner": owner, "sha256": sha256, "filename": filename, "path": path,
                "size_bytes": size_bytes, "page_count": page_count,
                "created_at": self._now(),
            })
        self.conn.commit()
        return doc_id

    def get_document(self, doc_id: int, owner: str | None = None) -> dict | None:
        """The document, or None if it doesn't exist or belongs to someone else."""
        sql, params = "SELECT * FROM documents WHERE id = ?", (doc_id,)
        if owner is not None:
            sql, params = sql + " AND owner = ?", params + (owner,)
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def set_file_id(self, doc_id: int, file_id: str | None):
        """Remember where the PDF was uploaded (Claude Files API), so it is sent only once."""
        cur = self.conn.cursor()
        cur.execute("UPDATE documents SET file_id = ? WHERE id = ?", (file_id, doc_id))
        self.conn.commit()

    def save_pages(self, doc_id: int, pages: list[str]):
        """Store each page's text layer once; review quotes are checked against it."""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM pages WHERE document_id = ?", (doc_id,))
        if int(cur.fetchone()[0]) == 0:
            for number, text in enumerate(pages, start=1):
                cur.execute("INSERT INTO pages (document_id, page, text) VALUES (?, ?, ?)",
                            (doc_id, number, text))
        self.conn.commit()

    def get_pages(self, doc_id: int) -> list[str]:
        rows = self._query("SELECT text FROM pages WHERE document_id = ? ORDER BY page", (doc_id,))
        return [r["text"] for r in rows]

    def delete_document(self, doc_id: int, owner: str) -> dict | None:
        """Delete a document with all its sessions; returns the deleted row."""
        doc = self.get_document(doc_id, owner)
        if doc:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM documents WHERE id = ? AND owner = ?", (doc_id, owner))
            self.conn.commit()
        return doc

    def list_documents(self, owner: str = LOCAL_OWNER) -> list[dict]:
        return self._query(
            """SELECT d.id, d.filename, d.page_count, d.size_bytes, d.created_at,
                      COUNT(DISTINCT s.id) AS sessions,
                      COUNT(m.id)          AS questions
               FROM documents d
               LEFT JOIN sessions s ON s.document_id = d.id
               LEFT JOIN messages m ON m.session_id = s.id AND m.role = 'user'
               WHERE d.owner = ?
               GROUP BY d.id, d.filename, d.page_count, d.size_bytes, d.created_at
               ORDER BY d.id""",
            (owner,),
        )

    # ---------- sessions ----------

    def create_session(self, document_id: int, kind: str = "chat",
                       language: str | None = None) -> int:
        cur = self.conn.cursor()
        now = self._now()
        session_id = self._insert(cur, "sessions", {
            "document_id": document_id, "kind": kind, "language": language,
            "created_at": now, "updated_at": now,
        })
        self.conn.commit()
        return session_id

    def get_session(self, session_id: int, owner: str | None = None) -> dict | None:
        sql = """SELECT s.*, d.filename, d.path, d.sha256, d.owner
                 FROM sessions s JOIN documents d ON d.id = s.document_id
                 WHERE s.id = ?"""
        params: tuple = (session_id,)
        if owner is not None:
            sql, params = sql + " AND d.owner = ?", params + (owner,)
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def latest_session(self, document_id: int, kind: str = "chat",
                       language: str | None = None) -> dict | None:
        top, limit = self._limit(1)
        sql = f"SELECT {top}id FROM sessions WHERE document_id = ? AND kind = ?"
        params: tuple = (document_id, kind)
        if language is not None:
            sql += " AND language = ?"
            params += (language,)
        rows = self._query(sql + " ORDER BY updated_at DESC, id DESC" + limit, params)
        return self.get_session(rows[0]["id"]) if rows else None

    def list_sessions(self, document_id: int | None = None, limit: int = 20,
                      owner: str = LOCAL_OWNER) -> list[dict]:
        top, suffix = self._limit(limit)
        where, params = "WHERE d.owner = ?", (owner,)
        if document_id:
            where, params = where + " AND s.document_id = ?", params + (document_id,)
        return self._query(
            f"""SELECT {top}s.id, s.document_id, s.kind, s.language, s.updated_at, d.filename,
                       SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) AS questions,
                       COALESCE(SUM(m.cost_usd), 0) AS cost_usd
                FROM sessions s
                JOIN documents d ON d.id = s.document_id
                LEFT JOIN messages m ON m.session_id = s.id
                {where}
                GROUP BY s.id, s.document_id, s.kind, s.language, s.updated_at, d.filename
                ORDER BY s.updated_at DESC, s.id DESC{suffix}""",
            params,
        )

    # ---------- messages ----------

    def save_turn(self, session_id: int, question: str, answer_text: str,
                  answer_content: list[dict], usage: Usage,
                  citations: list[Citation]) -> int:
        """Store one question/answer pair atomically and return the answer's id.

        Either the whole turn is saved (question, answer, usage, citations) or
        nothing is, so a failed request never leaves a half-written history.
        """
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?",
                        (session_id,))
            seq = int(cur.fetchone()[0])
            now = self._now()
            self._insert(cur, "messages", {
                "session_id": session_id, "seq": seq + 1, "role": "user",
                "text": question, "created_at": now,
            })
            answer_id = self._insert(cur, "messages", {
                "session_id": session_id, "seq": seq + 2, "role": "assistant",
                "text": answer_text,
                "content_json": json.dumps(answer_content, ensure_ascii=False),
                "model": usage.model,
                "input_tokens": usage.input_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": usage.cost_usd,
                "latency_ms": usage.latency_ms,
                "created_at": now,
            })
            for c in citations:
                cur.execute(
                    "INSERT INTO citations (message_id, start_page, end_page, cited_text) "
                    "VALUES (?, ?, ?, ?)",
                    (answer_id, c.start_page, c.end_page, c.cited_text),
                )
            cur.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            self.conn.commit()
            return answer_id
        except Exception:
            self.conn.rollback()
            raise

    def get_messages(self, session_id: int) -> list[dict]:
        return self._query(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
        )

    def get_citations(self, message_id: int) -> list[Citation]:
        rows = self._query(
            "SELECT start_page, end_page, cited_text FROM citations "
            "WHERE message_id = ? ORDER BY start_page, id",
            (message_id,),
        )
        return [Citation(r["start_page"], r["end_page"], r["cited_text"]) for r in rows]

    # ---------- reviews ----------

    def save_review(self, document_id: int, playbook: str, language: str,
                    report: dict, usage: Usage) -> int:
        cur = self.conn.cursor()
        review_id = self._insert(cur, "reviews", {
            "document_id": document_id, "playbook": playbook, "language": language,
            "report_json": json.dumps(report, ensure_ascii=False),
            "overall_risk": report.get("overall_risk"),
            "model": usage.model,
            "input_tokens": usage.input_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "latency_ms": usage.latency_ms,
            "created_at": self._now(),
        })
        self.conn.commit()
        return review_id

    def get_review(self, review_id: int, owner: str | None = None) -> dict | None:
        """The review with its parsed report, or None if it doesn't exist or is someone else's."""
        sql = """SELECT r.*, d.filename, d.owner FROM reviews r
                 JOIN documents d ON d.id = r.document_id WHERE r.id = ?"""
        params: tuple = (review_id,)
        if owner is not None:
            sql, params = sql + " AND d.owner = ?", params + (owner,)
        rows = self._query(sql, params)
        if not rows:
            return None
        review = rows[0]
        review["report"] = json.loads(review.pop("report_json"))
        return review

    def latest_review(self, document_id: int, playbook: str, language: str) -> dict | None:
        top, limit = self._limit(1)
        rows = self._query(
            f"SELECT {top}id FROM reviews WHERE document_id = ? AND playbook = ? AND language = ? "
            f"ORDER BY created_at DESC, id DESC{limit}",
            (document_id, playbook, language),
        )
        return self.get_review(rows[0]["id"]) if rows else None

    def list_reviews(self, document_id: int) -> list[dict]:
        return self._query(
            "SELECT id, document_id, playbook, language, overall_risk, cost_usd, created_at "
            "FROM reviews WHERE document_id = ? ORDER BY created_at DESC, id DESC",
            (document_id,),
        )

    # ---------- answer library ----------

    def replace_library(self, owner: str, entries: list[dict]) -> int:
        """Replace the owner's approved answers in one transaction; returns how many were saved."""
        cur = self.conn.cursor()
        try:
            cur.execute("DELETE FROM library WHERE owner = ?", (owner,))
            now = self._now()
            for e in entries:
                cur.execute("INSERT INTO library (owner, question, answer, source, updated_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (owner, e["question"], e["answer"], e.get("source") or None, now))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(entries)

    def list_library(self, owner: str) -> list[dict]:
        return self._query("SELECT id, question, answer, source, updated_at FROM library "
                           "WHERE owner = ? ORDER BY id", (owner,))

    def search(self, term: str, limit: int = 20, owner: str = LOCAL_OWNER) -> list[dict]:
        """Find past questions and answers containing a phrase, across all documents."""
        top, suffix = self._limit(limit)
        # Match the phrase literally: % _ [ typed by the user are not wildcards.
        escaped = "".join("!" + ch if ch in "!%_[" else ch for ch in term)
        return self._query(
            f"""SELECT {top}m.session_id, s.document_id, m.seq, m.role, m.text, m.created_at,
                       d.filename
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                JOIN documents d ON d.id = s.document_id
                WHERE d.owner = ? AND m.text LIKE ? ESCAPE '!'
                ORDER BY m.created_at DESC, m.id DESC{suffix}""",
            (owner, f"%{escaped}%"),
        )

    def spent_since(self, since: datetime, owner: str | None = None) -> float:
        """Estimated USD spent on answers and reviews since a UTC time, for one owner or everyone."""
        since = since.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None)
        sql = f"""SELECT COALESCE(SUM(b.cost_usd), 0) AS spent
                  FROM {BILLED_CALLS} b
                  JOIN documents d ON d.id = b.document_id
                  WHERE b.created_at >= ?"""
        params: tuple = (since.isoformat(sep=" ") if self.dialect == "sqlite" else since,)
        if owner is not None:
            sql, params = sql + " AND d.owner = ?", params + (owner,)
        return float(self._query(sql, params)[0]["spent"])

    # ---------- reporting ----------

    def stats(self, owner: str = LOCAL_OWNER) -> dict:
        """Usage totals for one owner (answers and reviews): per document, per model and overall."""
        per_document = self._query(
            f"""SELECT d.filename,
                       COUNT(*) AS answers,
                       COALESCE(SUM(b.input_tokens + b.cache_write_tokens + b.cache_read_tokens), 0)
                           AS prompt_tokens,
                       COALESCE(SUM(b.cache_read_tokens), 0) AS cached_tokens,
                       COALESCE(SUM(b.output_tokens), 0)     AS output_tokens,
                       COALESCE(SUM(b.cost_usd), 0)          AS cost_usd
                FROM {BILLED_CALLS} b
                JOIN documents d ON d.id = b.document_id
                WHERE d.owner = ?
                GROUP BY d.id, d.filename
                ORDER BY cost_usd DESC""",
            (owner,),
        )
        per_model = self._query(
            f"""SELECT b.model, COUNT(*) AS answers,
                       COALESCE(SUM(b.cost_usd), 0) AS cost_usd,
                       AVG(b.latency_ms) AS avg_latency_ms
                FROM {BILLED_CALLS} b
                JOIN documents d ON d.id = b.document_id
                WHERE d.owner = ?
                GROUP BY b.model ORDER BY cost_usd DESC""",
            (owner,),
        )
        prompt = sum(int(r["prompt_tokens"]) for r in per_document)
        cached = sum(int(r["cached_tokens"]) for r in per_document)
        return {
            "per_document": per_document,
            "per_model": per_model,
            "total_cost_usd": sum(float(r["cost_usd"]) for r in per_document),
            "cache_hit_rate": cached / prompt if prompt else 0.0,
        }


class StoragePool:
    """A small pool of Storage connections for the web app.

    Each request borrows one connection and gives it back. A connection that died
    while idle (an auto-paused Azure SQL database closes them) is replaced.
    """

    def __init__(self, url: str, size: int = 4):
        self.url = url
        self.size = size
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._lock = threading.Lock()
        self._idle.put(Storage(url))  # fail fast on a bad URL, and create the schema once
        self._created = 1

    @contextmanager
    def connection(self):
        store = self._acquire()
        try:
            yield store
        except BaseException:
            try:
                store.conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._idle.put(store)

    def _acquire(self) -> Storage:
        try:
            store = self._idle.get_nowait()
        except queue.Empty:
            with self._lock:
                can_create = self._created < self.size
                if can_create:
                    self._created += 1
            if not can_create:
                return self._revive(self._idle.get(timeout=60))
            try:
                return Storage(self.url)
            except Exception:
                with self._lock:
                    self._created -= 1  # free the slot so a later request can retry
                raise
        return self._revive(store)

    def _revive(self, store: Storage) -> Storage:
        if store.ping():
            return store
        try:
            store.close()
        except Exception:
            pass
        try:
            return Storage(self.url)
        except Exception:
            with self._lock:
                self._created -= 1
            raise

    def close(self):
        while not self._idle.empty():
            self._idle.get_nowait().close()


def _connect_mssql(connection_string: str, attempts: int = 8):
    """Connect to Azure SQL, waiting while a paused serverless database resumes.

    An auto-paused database rejects the first logins with "is not currently
    available" for up to about a minute while it wakes up.
    """
    import mssql_python  # only needed for Azure SQL

    for attempt in range(1, attempts + 1):
        try:
            return mssql_python.connect(connection_string, timeout=30)
        except mssql_python.OperationalError as e:
            if "not currently available" not in str(e) or attempt == attempts:
                raise
            if attempt == 1:
                print("Database is resuming from auto-pause, waiting...", file=sys.stderr)
            time.sleep(min(5 * attempt, 20))


def to_datetime(value) -> datetime:
    """Normalize a timestamp from either backend (str from SQLite, datetime from SQL Server)."""
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
