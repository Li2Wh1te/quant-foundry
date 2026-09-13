"""Exercise typed HTTP serialization of immutable admission evidence."""
import asyncio
import json
from types import MappingProxyType
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI

from app.backtesting.run_router import router
from app.db.session import get_db_session


@pytest.mark.parametrize("status", ["ready", "degraded", "blocked"])
def test_frozen_preflight_evidence_is_serializable_without_mutating_gate(status):
    identity = uuid4()
    evidence = {
        "status": status,
        "report_hash": "report-hash",
        "issues": (MappingProxyType({"message": "标的数据不完整", "details": MappingProxyType({"instrument_id": identity})}),),
        "gates": MappingProxyType({"allowed": status == "ready", "checks": (MappingProxyType({"passed": False}),)}),
    }
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db_session] = lambda: object()
    payload = json.dumps({"strategy_revision_id": str(uuid4()), "backtest_config": {
        "start_date": "2026-08-03", "end_date": "2026-08-07", "initial_cash": "100000",
    }}).encode()

    async def invoke():
        messages = []
        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}
        async def send(message):
            messages.append(message)
        await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                   "method": "POST", "scheme": "http", "path": "/api/admin/backtest-runs/preflight",
                   "root_path": "", "query_string": b"", "headers": [(b"content-type", b"application/json")],
                   "server": ("test", 80), "client": ("127.0.0.1", 1234)}, receive, send)
        assert next(m["status"] for m in messages if m["type"] == "http.response.start") == 200
        return json.loads(b"".join(m.get("body", b"") for m in messages))
    with patch("app.backtesting.run_router._preflight_evidence", return_value=evidence):
        result = asyncio.run(invoke())
    assert result["status"] == status
    assert result["gates"]["allowed"] is (status == "ready")
    assert result["issues"][0]["details"]["instrument_id"] == str(identity)
    assert isinstance(evidence["gates"], MappingProxyType)
