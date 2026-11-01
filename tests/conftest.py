"""Shared fixtures for the web service tests."""

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_pdf_chat import FakeClient  # noqa: E402
from web.app import Settings, create_app  # noqa: E402

TEST_DB = os.environ.get("PDF_CHAT_TEST_DB")


class FakeFiles:
    def __init__(self):
        self.uploaded = []
        self.deleted = []

    def upload(self, file):
        name, data, _ = file
        self.uploaded.append(name)
        return type("FileMetadata", (), {"id": f"file_{uuid.uuid4().hex[:10]}", "size_bytes": len(data)})()

    def delete(self, file_id):
        self.deleted.append(file_id)


class FakeApiClient(FakeClient):
    def __init__(self, *messages):
        super().__init__(*messages)
        self.files = FakeFiles()


@pytest.fixture
def make_client(tmp_path):
    """Build a TestClient around a fresh app; returns (http, fake Claude client)."""
    apps = []

    def build(*messages, **settings):
        fake = FakeApiClient(*messages)
        url = TEST_DB or f"sqlite:///{tmp_path / f'api-{len(apps)}.db'}"
        app = create_app(Settings(database_url=url, **settings), client_factory=lambda: fake)
        http = TestClient(app)
        apps.append(app)
        return http, fake

    yield build
    for app in apps:
        if TEST_DB:  # shared database: remove only test documents
            with app.state.pool.connection() as store:
                cur = store.conn.cursor()
                cur.execute("DELETE FROM documents WHERE filename LIKE 'test-%'")
                store.conn.commit()
        app.state.pool.close()
