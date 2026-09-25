"""Current API admission and pagination against an isolated PostgreSQL schema."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
import json
import os
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.data_store.adapters.registry import BY_ID
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import run_entry
from app.data_store import router as store_router
from app.db.session import get_db_session
from app.main import create_app
from tests.test_data_store_domain_samples import ready  # noqa: F401 - fixture
from tests.test_data_store_kernel import database, limits, store  # noqa: F401 - fixtures


pytestmark = pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1",
                                reason="isolated PostgreSQL required")
TOKEN = "a" * 64


@dataclass
class ASGIResponse:
    status_code: int
    text: str

    def json(self):
        return json.loads(self.text)


class ASGIClient:
    """Small transport for real route/dependency execution without httpx2."""

    def __init__(self, app):
        self.app = app

    def get(self, path, *, headers=None):
        return self._request("GET", path, headers=headers)

    def post(self, path, *, headers=None, json=None):
        return self._request("POST", path, headers=headers, payload=json)

    def _request(self, method, target, *, headers=None, payload=None):
        async def execute():
            parts = urlsplit(target)
            body = b"" if payload is None else json.dumps(payload).encode()
            sent = False
            messages = []
            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": method, "scheme": "http",
                "path": parts.path, "raw_path": parts.path.encode(),
                "query_string": parts.query.encode(),
                "headers": [(key.lower().encode(), value.encode())
                            for key, value in (headers or {}).items()]
                           + ([(b"content-type", b"application/json")] if payload is not None else []),
                "client": ("test", 1), "server": ("test", 80), "root_path": "",
            }

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Future()

            async def send(message):
                messages.append(message)

            await self.app(scope, receive, send)
            status = next(item["status"] for item in messages
                          if item["type"] == "http.response.start")
            content = b"".join(item.get("body", b"") for item in messages
                               if item["type"] == "http.response.body")
            return ASGIResponse(status, content.decode())

        return asyncio.run(execute())


@pytest.fixture
def api(ready, monkeypatch):
    engine = ready.catalog.engine
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE scheduled_tasks (id uuid PRIMARY KEY, task_type text)"))
        connection.execute(text("""CREATE TABLE data_store_legacy_maintenance (
            singleton integer PRIMARY KEY, phase text NOT NULL)"""))
        connection.execute(text("""CREATE TABLE data_store_legacy_restrictions (
            origin_key text PRIMARY KEY, dataset text, scope_key text,
            captured_at timestamptz DEFAULT clock_timestamp())"""))
    monkeypatch.setattr(store_router, "get_engine", lambda: engine)
    monkeypatch.setattr(store_router, "CurrentStore",
                        partial(store_router.CurrentStore, limits=ready.limits))
    settings = Settings(
        api_token=TOKEN, cursor_signing_key="b" * 64,
        database_password="isolated-test", data_store_root=ready.files.root,
        environment="test", scheduler_enabled=False,
    )
    app = create_app(settings)

    def session_dependency():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db_session] = session_dependency
    return ASGIClient(app), ready


def query_body(entry, preview_key, **overrides):
    body = {
        "dataset": entry.spec.name, "frequency": "report",
        "representation": preview_key["representation"],
        "subject": preview_key["subject"],
        "from_key": preview_key["object_key"],
        "to_key": preview_key["object_key"],
        "columns": ["representation", "subject", "object_key", "member_key"],
        "page_size": 1,
    }
    return {**body, **overrides}


def test_auth_legacy_refusal_and_fresh_empty_catalog(api):
    client, _ = api
    assert client.get("/api/admin/data-store/datasets").status_code == 401
    assert client.get("/api/admin/data-foundation/query").status_code == 401
    headers = {"Authorization": f"Bearer {TOKEN}"}
    catalog = client.get("/api/admin/data-store/datasets?limit=2", headers=headers)
    assert catalog.status_code == 200
    assert catalog.json()["phase"] == "ready"
    assert len(catalog.json()["items"]) == 2
    assert all(item["status"] == "not_checked" for item in catalog.json()["items"])
    retired = client.get("/api/admin/data-foundation/query?release_id=old", headers=headers)
    assert retired.status_code == 410
    assert retired.json()["detail"]["code"] == "LEGACY_FOUNDATION_REMOVED"


def test_existing_legacy_rows_and_reset_receipt_hold_queries(api):
    client, store = api
    headers = {"Authorization": f"Bearer {TOKEN}"}
    entry = BY_ID["E07"]
    body = query_body(entry, {"representation": "r", "subject": "s", "object_key": "k"})
    with store.catalog.engine.begin() as connection:
        connection.execute(text("CREATE TABLE foundation_artifacts (id integer PRIMARY KEY)"))
        connection.execute(text("INSERT INTO foundation_artifacts VALUES (1)"))
    assert client.get("/api/admin/data-store/status", headers=headers).json()["phase"] == "rebuilding"
    blocked = client.post("/api/admin/data-store/query", headers=headers, json=body)
    assert blocked.status_code == 503
    assert blocked.json()["detail"]["code"] == "DATA_STORE_REBUILDING"
    with store.catalog.engine.begin() as connection:
        connection.execute(text("DROP TABLE foundation_artifacts"))
        connection.execute(text("INSERT INTO data_store_legacy_maintenance VALUES (1,'reset_done')"))
    assert client.post("/api/admin/data-store/query", headers=headers, json=body).status_code == 503
    with store.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE data_store_legacy_maintenance SET phase='ready' WHERE singleton=1"))
    assert client.get("/api/admin/data-store/status", headers=headers).json()["phase"] == "ready"


def test_empty_current_scope_reports_schema_and_does_not_hide_issues(api):
    client, store = api
    headers = {"Authorization": f"Bearer {TOKEN}"}
    entry = BY_ID["E07"]
    store.register(entry.spec)
    body = query_body(entry, {"representation": "r", "subject": "s", "object_key": "k"})

    empty = client.post("/api/admin/data-store/query", headers=headers, json=body)
    assert empty.status_code == 200
    assert empty.json()["status"] == "empty"
    assert empty.json()["schema_id"] == entry.spec.schema_id
    assert empty.json()["business_date_coverage_verified"] is False

    with store.catalog.engine.begin() as connection:
        connection.execute(text("""INSERT INTO data_store_issues
            (dataset, issue_key, scope_key, reason, evidence_token, target_json, resolution_json)
            VALUES (:dataset, 'test-issue', 'scope', 'SOURCE_CONFIRMATION_UNPROVEN',
                    'test-evidence', '{}', '{}')"""), {"dataset": entry.spec.name})
    blocked = client.post("/api/admin/data-store/query", headers=headers, json=body)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "DATA_RESTRICTED"
    partial = client.post("/api/admin/data-store/query", headers=headers,
                          json={**body, "allow_partial": True})
    assert partial.status_code == 200
    assert partial.json()["status"] == "restricted"
    assert partial.json()["unresolved_issues"] == 1


def test_current_preview_and_generation_change_rejects_continuation(api):
    client, store = api
    headers = {"Authorization": f"Bearer {TOKEN}"}
    entry = BY_ID["E07"]
    outcome = run_entry(store, entry, NativeSources(store.catalog.engine))
    assert outcome["complete"] and outcome["qualified"]
    descriptor = client.get(f"/api/admin/data-store/datasets/{entry.spec.name}", headers=headers)
    assert descriptor.status_code == 200
    detail = descriptor.json()
    assert detail["status"] == "available"
    assert detail["row_count"] >= 2
    assert detail["preview_key"] is not None
    body = query_body(entry, detail["preview_key"])
    first = client.post("/api/admin/data-store/query", headers=headers, json=body)
    assert first.status_code == 200, first.text
    assert len(first.json()["rows"]) == 1
    assert first.json()["next_cursor"]
    assert client.post("/api/admin/data-store/query?release_id=old", headers=headers, json=body).status_code == 422
    assert client.post("/api/admin/data-store/query", headers=headers,
                       json={**body, "release_id": "old"}).status_code == 422
    with store.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE data_store_datasets SET generation=generation+1 "
                                "WHERE name=:dataset"), {"dataset": entry.spec.name})
    continuation = client.post("/api/admin/data-store/query", headers=headers,
                               json={**body, "cursor": first.json()["next_cursor"]})
    assert continuation.status_code == 409
    assert continuation.json()["detail"]["code"] == "DATA_CHANGED"
