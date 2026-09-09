"""Exercise persisted configuration and admission behavior on SQLite and CI PostgreSQL."""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import Column, JSON, MetaData, Table, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.data_sources.credentials import decrypt, encrypt
from app.data_sources.models import DataSourceConfig
from app.data_sources.providers import PROVIDERS, ProbeResult, SourceError, TushareProvider
from app.data_sources.service import DataSourceService, initialize_sources, runtime_credentials
from app.scheduling.models import ScheduledTask, TaskRun
from app.scheduling.registry import TaskDefinition, TaskRegistry
from app.scheduling.repository import SchedulerRepository
from app.scheduling.schemas import TriggerType
from app.scheduling.service import SchedulerService, TaskConflictError


def settings(**kwargs):
    return Settings(api_token="a" * 64, cursor_signing_key="b" * 64,
        database_password="test-secret", data_source_encryption_key="c" * 64,
        _env_file=None, **kwargs)


SUCCESS = ProbeResult("connected", "连接测试通过。", datetime.now(UTC))


class DataSourceTest(unittest.TestCase):
    def setUp(self):
        self.postgres = os.getenv("POSTGRES_TEST_ENABLED") == "1"
        self.engine = create_engine(get_settings().database_url if self.postgres else "sqlite://")
        if not self.postgres:
            metadata = MetaData()
            for model in (DataSourceConfig, ScheduledTask, TaskRun):
                Table(model.__tablename__, metadata, *[
                    Column(col.name, JSON() if isinstance(col.type, JSONB) else col.type,
                           primary_key=col.primary_key) for col in model.__table__.columns])
            metadata.create_all(self.engine)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin() if self.postgres else None
        self.session = Session(self.connection, join_transaction_mode="create_savepoint")
        self.settings = settings()
        row = self.session.get(DataSourceConfig, "tushare")
        if row is None:
            self.session.add(DataSourceConfig(key="tushare"))
        else:
            row.initialized, row.enabled, row.version = False, True, 1
            row.encrypted_secrets, row.values = None, {}
            row.check_status, row.checked_at, row.check_message = "not_checked", None, None
        self.session.commit()
        self.registry = TaskRegistry()
        self.task_type = "test." + uuid4().hex
        self.registry.register(TaskDefinition(key=self.task_type, name="测试采集", english_name="Test ingestion",
            parameters_model=BaseModel, handler=lambda *_: None, source_key="tushare"))
        self.service = DataSourceService(self.session, self.settings, self.registry)

    def tearDown(self):
        self.session.close()
        if self.transaction is not None:
            self.transaction.rollback()
        self.connection.close()
        self.engine.dispose()

    def initialize(self, **kwargs):
        initialize_sources(self.session, settings(**kwargs))

    def row(self):
        self.session.expire_all()
        return self.session.get(DataSourceConfig, "tushare")

    def task(self, once=False):
        task = ScheduledTask(name="测试任务", task_type=self.task_type, parameters={}, parameter_version=1,
            schedule=({"type": "once", "run_at": (datetime.now(UTC)+timedelta(hours=1)).isoformat()}
                      if once else {"type": "interval", "seconds": 60,
                                    "start_at": datetime.now(UTC).isoformat()}), state="active")
        self.session.add(task)
        self.session.commit()
        return task

    def test_legacy_migrates_once_and_database_remains_authoritative(self):
        self.initialize(tushare_token="legacy-secret", tushare_api_url="https://legacy.example")
        row = self.row()
        self.assertNotIn("legacy-secret", row.encrypted_secrets)
        self.assertEqual(runtime_credentials(self.session, self.settings, "tushare"),
            ({"api_url": "https://legacy.example"}, {"token": "legacy-secret"}))
        self.initialize(tushare_token="changed-env", tushare_api_url="https://changed.example")
        self.assertEqual(runtime_credentials(self.session, self.settings, "tushare")[1]["token"], "legacy-secret")

    def test_initially_empty_installation_does_not_import_environment_later(self):
        self.initialize()
        self.initialize(tushare_token="late-env")
        self.assertTrue(self.row().initialized)
        self.assertIsNone(self.row().encrypted_secrets)
        with self.assertRaises(SourceError):
            runtime_credentials(self.session, self.settings, "tushare")

    def test_key_loss_never_overwrites_existing_ciphertext(self):
        self.initialize(tushare_token="legacy-secret")
        encrypted = self.row().encrypted_secrets
        broken = self.settings.model_copy(update={"data_source_encryption_key": None})
        with self.assertRaisesRegex(SourceError, "加密密钥"):
            initialize_sources(self.session, broken)
        self.session.rollback()
        self.assertEqual(self.row().encrypted_secrets, encrypted)

    def test_wrong_key_and_swapped_source_are_rejected(self):
        encrypted = encrypt(self.settings, "tushare", {"token": "private"})
        with self.assertRaises(SourceError):
            decrypt(self.settings, "other", encrypted)
        from pydantic import SecretStr
        wrong = self.settings.model_copy(update={"data_source_encryption_key": SecretStr("d"*64)})
        with self.assertRaises(SourceError):
            decrypt(wrong, "tushare", encrypted)

    def test_failed_probe_keeps_credentials_version_and_health(self):
        self.initialize(tushare_token="old")
        before = self.row().encrypted_secrets
        with patch.object(PROVIDERS["tushare"], "probe", return_value=ProbeResult("rejected", "认证失败。", datetime.now(UTC))):
            with self.assertRaises(SourceError):
                self.service.save("tushare", 1, {"token": "new"})
        row = self.row()
        self.assertEqual((row.encrypted_secrets, row.version, row.checked_at), (before, 1, None))

    def test_testing_a_draft_never_persists_config_or_health(self):
        self.initialize(tushare_token="old")
        with patch.object(PROVIDERS["tushare"], "probe", return_value=SUCCESS):
            result = self.service.test("tushare", 1, {"api_url": "https://draft.example", "token": "new"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["saved"])
        self.assertEqual(self.row().values["api_url"], "http://api.tushare.pro")
        self.assertIsNone(self.row().checked_at)

    def test_successful_save_preserves_blank_secret_and_redacts_response(self):
        self.initialize(tushare_token="private-old")
        with patch.object(PROVIDERS["tushare"], "probe", return_value=SUCCESS) as probe:
            detail = self.service.save("tushare", 1, {"token": "", "api_url": "https://new.example"})
        self.assertEqual(probe.call_args.args[1], {"token": "private-old"})
        self.assertEqual(detail["version"], 2)
        self.assertEqual(detail["check_status"], "connected")
        self.assertNotIn("private-old", json.dumps(detail, default=str))
        self.assertNotIn(self.row().encrypted_secrets, json.dumps(detail, default=str))
        self.assertEqual(detail["secret_fields_configured"], ["token"])

    def test_stale_probe_cannot_overwrite_newer_revision(self):
        self.initialize(tushare_token="old")
        def concurrent_change(*_):
            # The probe runs outside a transaction. Simulate a completed edit
            # from another request before its network response arrives.
            self.assertFalse(self.session.in_transaction())
            row = self.row()
            row.version += 1
            row.values = {"api_url": "https://winner.example"}
            self.session.commit()
            return SUCCESS
        with patch.object(PROVIDERS["tushare"], "probe", side_effect=concurrent_change):
            with self.assertRaisesRegex(SourceError, "已被更新"):
                self.service.save("tushare", 1, {"api_url": "https://loser.example"})
        self.assertEqual(self.row().values["api_url"], "https://winner.example")

    def test_disable_skips_only_queued_and_preserves_plans_and_running_client(self):
        self.initialize(tushare_token="old")
        task = self.task()
        repository = SchedulerRepository(self.session)
        from app.scheduling.schemas import RunStatus
        queued = repository.add_run(task, trigger_type=TriggerType.MANUAL, status=RunStatus.QUEUED)
        running = repository.add_run(task, trigger_type=TriggerType.MANUAL, status=RunStatus.QUEUED)
        running.status, running.started_at = "running", datetime.now(UTC)
        self.session.commit()
        detail = self.service.set_enabled("tushare", 1, False)
        self.assertEqual(detail["skipped_runs"], 1)
        self.session.refresh(queued)
        self.session.refresh(running)
        self.assertEqual(queued.status, "skipped")
        self.assertEqual(running.status, "running")
        self.assertEqual(task.state, "active")
        self.assertEqual(runtime_credentials(self.session, self.settings, "tushare")[1]["token"], "old")
        self.service.set_enabled("tushare", 2, True)
        self.session.refresh(queued)
        self.assertEqual(queued.status, "skipped")

    def test_disabled_manual_rejected_and_once_schedule_consumed_without_backfill(self):
        self.initialize(tushare_token="old")
        task = self.task(once=True)
        self.service.set_enabled("tushare", 1, False)
        scheduler = SchedulerService(self.session, self.registry)
        with self.assertRaises(TaskConflictError):
            scheduler.enqueue_run(task.id, trigger_type=TriggerType.MANUAL, max_queued_runs=100)
        run = scheduler.enqueue_run(task.id, trigger_type=TriggerType.SCHEDULED, max_queued_runs=100)
        self.session.commit()
        self.assertEqual(run.status, "skipped")
        self.assertEqual(task.state, "completed")
        self.service.set_enabled("tushare", 2, True)
        self.assertEqual(task.state, "completed")

    def test_unconfigured_source_cannot_enable_or_enqueue(self):
        self.initialize()
        with self.assertRaises(SourceError):
            self.service.set_enabled("tushare", 1, True)
        task = self.task()
        with self.assertRaises(TaskConflictError):
            SchedulerService(self.session, self.registry).enqueue_run(task.id,
                trigger_type=TriggerType.MANUAL, max_queued_runs=100)

    def test_enabled_source_admits_manual_and_scheduled_interval_runs(self):
        self.initialize(tushare_token="old")
        scheduler = SchedulerService(self.session, self.registry)
        for trigger in (TriggerType.MANUAL, TriggerType.SCHEDULED):
            task = self.task()
            run = scheduler.enqueue_run(task.id, trigger_type=trigger, max_queued_runs=100)
            self.session.commit()
            self.assertEqual(run.status, "queued")
            self.assertEqual(task.state, "active")

    def test_other_provider_has_independent_fields(self):
        class OtherProvider:
            key, name = "other", "其他测试源"
            fields = ({"key": "region", "label": "区域", "type": "string", "required": True, "default": "cn"},
                      {"key": "access_key", "label": "访问密钥", "type": "secret", "required": True})
            def validate(self, fields, secrets):
                return {"region": fields["region"]}, {"access_key": fields.get("access_key") or secrets["access_key"]}
            def probe(self, *_):
                return SUCCESS
        self.session.add(DataSourceConfig(key="other", initialized=True, values={"region": "cn"}))
        self.session.commit()
        with patch.dict(PROVIDERS, other=OtherProvider()):
            result = self.service.save("other", 1, {"region": "eu", "access_key": "private-other"})
        self.assertEqual(result["values"], {"region": "eu"})
        self.assertEqual(result["secret_fields_configured"], ["access_key"])
        self.assertNotIn("token", json.dumps(result, default=str))


class SourceProbeTest(unittest.TestCase):
    def setUp(self):
        self.provider = TushareProvider()

    def response(self, data, status=200):
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [json.dumps(data).encode()]
        return response

    def test_bounded_read_only_probe_accepts_real_calendar_shape(self):
        response = self.response({"code": 0, "data": {"fields": ["cal_date", "is_open"], "items": [["20250102", 1]]}})
        with patch("app.data_sources.providers.requests.post", return_value=response) as post:
            result = self.provider.probe({"api_url": "https://example.test"}, {"token": "private"})
        self.assertTrue(result.ok)
        self.assertEqual(post.call_args.kwargs["json"]["api_name"], "trade_cal")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(post.call_args.kwargs["timeout"], (3, 8))

    def test_vendor_reflections_http_failures_empty_data_and_timeouts_are_safe(self):
        import requests
        responses = [self.response({"code": -1, "msg": "private-secret"}),
                     self.response({}, 500), self.response({}, 302),
                     self.response({"code": 0, "data": {"fields": [], "items": []}})]
        for response in responses:
            with patch("app.data_sources.providers.requests.post", return_value=response):
                result = self.provider.probe({"api_url": "https://example.test"}, {"token": "private-secret"})
            self.assertFalse(result.ok)
            self.assertNotIn("private-secret", str(asdict(result)))
        with patch("app.data_sources.providers.requests.post", side_effect=requests.Timeout("private-secret")):
            result = self.provider.probe({"api_url": "https://example.test"}, {"token": "private-secret"})
        self.assertEqual(result.status, "timeout")
        self.assertNotIn("private-secret", result.message)

    def test_validation_allows_lan_proxy_but_rejects_credential_urls_and_unknown_fields(self):
        values, _ = self.provider.validate({"api_url": "http://10.0.0.2:8000/proxy", "token": "secret"}, {})
        self.assertEqual(values["api_url"], "http://10.0.0.2:8000/proxy")
        for url in ("file:///tmp/x", "http://u:p@host", "http://host?token=secret", "http://host#fragment", "http://host:invalid"):
            with self.assertRaises(SourceError):
                self.provider.validate({"api_url": url, "token": "secret"}, {})
        with self.assertRaises(SourceError):
            self.provider.validate({"account": "other-provider-field", "token": "secret"}, {})


class SourceRouteTest(unittest.TestCase):
    def test_invalid_forms_never_echo_secret_input(self):
        from app.main import create_app
        from app.data_sources.router import service
        app = create_app(settings())
        app.dependency_overrides[service] = lambda: Mock()

        async def request(payload):
            body = json.dumps(payload).encode()
            path = "/api/data-sources/tushare/config"
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "PUT", "scheme": "http", "path": path,
                "raw_path": path.encode(), "query_string": b"", "root_path": "",
                "headers": [(b"authorization", b"Bearer " + b"a"*64), (b"content-type", b"application/json")],
                "client": ("test", 1), "server": ("test", 80)}
            sent, messages = False, []
            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Future()
            async def send(message):
                messages.append(message)
            await app(scope, receive, send)
            return messages
        for payload in ({"fields": {"token": "private-input"}},
                        {"version": 1, "fields": ["private-input"]},
                        {"version": True, "fields": {"token": "private-input"}}):
            messages = asyncio.run(request(payload))
            self.assertEqual(next(msg["status"] for msg in messages if msg["type"] == "http.response.start"), 422)
            self.assertNotIn("private-input", str(messages))
            self.assertIn("no-store", str(messages))

    def test_routes_require_authentication(self):
        from app.main import create_app
        from tests.test_auth import request_status
        app = create_app(settings())
        for path, operations in app.openapi()["paths"].items():
            if path.startswith("/api/data-sources"):
                for operation in operations.values():
                    self.assertEqual(operation["security"], [{"API Token": []}])
        self.assertEqual(asyncio.run(request_status(app, "/api/data-sources")), 401)
