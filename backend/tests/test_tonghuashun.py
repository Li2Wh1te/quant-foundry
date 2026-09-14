"""Contract fixtures only: no real API keys, remote calls or production data."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
import importlib
import json
import traceback
import unittest
from unittest.mock import Mock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
import requests
from sqlalchemy import create_engine, text

from app.data_ingestion.clients import tonghuashun as transport
from app.data_sources.providers import TonghuashunProvider, SourceError
from app.data_sources.router import list_source_interfaces
from app.data_sources.tonghuashun_catalog import INTERFACES


def response(body=None, status=200, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = [json.dumps(body).encode()]
    return result


def success(items=None):
    return response({"code": 0, "request_id": "safe-request-id", "data": {"item": [] if items is None else items}})


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.gate = patch.object(transport, "_gate", transport._RequestGate())
        self.clock = patch.object(transport.time, "monotonic", side_effect=lambda: self.now)
        self.sleep = patch.object(transport.time, "sleep", side_effect=self.advance)
        self.gate.start(); self.clock.start(); self.sleep.start()
        self.addCleanup(self.gate.stop); self.addCleanup(self.clock.stop); self.addCleanup(self.sleep.stop)
        self.client = transport.TonghuashunClient("https://example.test", "fixture-private-key", interval_ms=0)

    def advance(self, seconds):
        self.now += seconds

    def test_request_keeps_units_nulls_and_empty_results_and_uses_header_auth(self):
        for items in ([], [{"price": None, "volume": 123, "per_ten_cash_before_tax": 0.8}]):
            with patch.object(transport.requests, "get", return_value=success(items)) as get:
                result = self.client.request("fund.market.historical", {"thscode": "510300.SH"})
            self.assertEqual(result.data["item"], json.loads(json.dumps(items), parse_float=Decimal))
            self.assertEqual(result.request_id, "safe-request-id")
            self.assertEqual(get.call_args.args, ("https://example.test/api/fund/market/historical",))
            self.assertEqual(get.call_args.kwargs["headers"]["X-api-key"], "fixture-private-key")
            self.assertNotIn("fixture-private-key", str(get.call_args.kwargs["params"]))
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
            self.assertTrue(get.call_args.kwargs["stream"])

    def test_only_allowlisted_read_paths_can_be_called(self):
        with patch.object(transport.requests, "get") as get:
            for key in ("https://other.test", "../../admin", "/api/meta/tickers/list", "stock.basics"):
                with self.assertRaises(SourceError):
                    self.client.request(key)
            get.assert_not_called()

    def test_business_failures_are_not_successes_or_empty_data(self):
        for code, kind in transport.BUSINESS_ERRORS.items():
            with self.subTest(code=code), patch.object(transport.requests, "get",
                return_value=response({"code": code, "message": "fixture-private-key", "data": None})) as get:
                transport._gate.next_start = 0
                with self.assertRaises(transport.TonghuashunError) as raised:
                    self.client.request("meta.tickers.list")
                self.assertEqual(raised.exception.kind, kind)
                self.assertNotIn("fixture-private-key", str(raised.exception))
                self.assertEqual(get.call_count, 3 if kind in transport.RETRYABLE else 1)

    def test_http_and_business_rate_limits_back_off_and_recover(self):
        for limited in (response({}, 429, {"Retry-After": "4"}), response({"code": 4001}, headers={"Retry-After": "4"})):
            transport._gate.next_start = 0
            start = self.now
            with patch.object(transport.requests, "get", side_effect=[limited, success()]) as get:
                self.client.request("meta.tickers.list")
            self.assertEqual(get.call_count, 2)
            self.assertGreaterEqual(self.now - start, 4)

    def test_retry_after_beyond_budget_returns_without_early_retry_and_cools_other_clients(self):
        with patch.object(transport.requests, "get", return_value=response({}, 429, {"Retry-After": "120"})) as get:
            with self.assertRaises(transport.TonghuashunError) as raised:
                self.client.request("meta.tickers.list")
            self.assertEqual(raised.exception.kind, "rate_limited")
            with self.assertRaises(transport.TonghuashunError):
                transport.TonghuashunClient("https://example.test", "another-key").request("meta.tickers.list")
            self.assertEqual(get.call_count, 1)
            self.assertEqual(self.now, 100)

    def test_retry_after_parses_http_date_and_ignores_invalid_values(self):
        future = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
        self.assertTrue(18 <= self.client._retry_after(future) <= 20)
        for value in (None, "bad", "-3", "NaN", "Infinity"):
            self.assertEqual(self.client._retry_after(value), 0)

    def test_network_and_timeout_exceptions_do_not_leak_trace_context(self):
        for failure in (requests.Timeout("fixture-private-key"), requests.ConnectionError("fixture-private-key")):
            with patch.object(transport.requests, "get", side_effect=failure) as get:
                try:
                    self.client.request("meta.tickers.list")
                except transport.TonghuashunError:
                    self.assertNotIn("fixture-private-key", traceback.format_exc())
                else:
                    self.fail("network failure was accepted")
                self.assertEqual(get.call_count, 3)

    def test_redirects_auth_and_unknown_codes_are_never_retried(self):
        for body in (response({}, 302), response({}, 401), response({}, 403), response({"code": 9999})):
            with patch.object(transport.requests, "get", return_value=body) as get:
                with self.assertRaises(transport.TonghuashunError):
                    self.client.request("meta.tickers.list")
                self.assertEqual(get.call_count, 1)

    def test_malformed_and_oversized_responses_fail_closed(self):
        for body in ([], {}, {"code": False, "data": {}}, {"code": "0", "data": {}},
                     {"code": 0, "data": None}, {"code": 0, "data": []}):
            with patch.object(transport.requests, "get", return_value=response(body)) as get:
                with self.assertRaises(transport.TonghuashunError) as raised:
                    self.client.request("meta.tickers.list")
                self.assertEqual(raised.exception.kind, "invalid_response")
                self.assertEqual(get.call_count, 1)
        raw = success()
        raw.iter_content.return_value = [b"x" * 51]
        with patch.object(transport.requests, "get", return_value=raw):
            with self.assertRaises(transport.TonghuashunError) as raised:
                transport.TonghuashunClient("https://example.test", "key", max_response_bytes=50).request("meta.tickers.list")
            self.assertEqual(raised.exception.kind, "response_too_large")
        raw.__exit__.assert_called()

    def test_slow_drip_is_bounded_by_wall_clock(self):
        raw = success()
        def drip():
            for char in b'{"code":0}':
                self.advance(3)
                yield bytes([char])
        raw.iter_content.side_effect = lambda *_: drip()
        with patch.object(transport.requests, "get", return_value=raw):
            with self.assertRaises(transport.TonghuashunError) as raised:
                transport.TonghuashunClient("https://example.test", "key", deadline_seconds=5, max_attempts=1).request("meta.tickers.list")
            self.assertEqual(raised.exception.kind, "timeout")
        self.assertFalse(transport._gate.lock.locked())

    def test_reflected_request_id_is_removed(self):
        raw = response({"code": 0, "request_id": "fixture-private-key", "data": {"item": []}})
        with patch.object(transport.requests, "get", return_value=raw):
            self.assertIsNone(self.client.request("meta.tickers.list").request_id)

    def test_decimal_precision_and_invalid_numeric_constants(self):
        raw = success()
        raw.iter_content.return_value = [b'{"code":0,"data":{"value":123456789.123456789}}']
        with patch.object(transport.requests, "get", return_value=raw):
            self.assertEqual(self.client.request("meta.tickers.list").data["value"], Decimal("123456789.123456789"))
        for constant in (b'NaN', b'Infinity', b'-Infinity'):
            raw.iter_content.return_value = [b'{"code":0,"data":{"value":' + constant + b'}}']
            with patch.object(transport.requests, "get", return_value=raw):
                with self.assertRaises(transport.TonghuashunError) as raised:
                    self.client.request("meta.tickers.list")
                self.assertEqual(raised.exception.kind, "invalid_response")

    def test_gate_paces_independent_client_instances(self):
        first = transport.TonghuashunClient("https://example.test", "one", interval_ms=2000)
        second = transport.TonghuashunClient("https://example.test", "two", interval_ms=2000)
        starts = []
        def get(*_, **__):
            starts.append(self.now)
            return success()
        with patch.object(transport.requests, "get", side_effect=get):
            first.request("meta.tickers.list")
            second.request("meta.tickers.list")
        self.assertGreaterEqual(starts[1] - starts[0], 2)

    def test_probe_checks_one_etf_and_never_retries_or_imports(self):
        provider = TonghuashunProvider()
        with patch.object(transport.requests, "get", return_value=success([{"thscode": "510300.SH", "asset_type": "fund-etf"}])) as get:
            probe = provider.probe({"api_url": "https://example.test"}, {"api_key": "key"})
        self.assertTrue(probe.ok)
        self.assertIn("其他接口", probe.message)
        self.assertEqual(get.call_args.kwargs["params"], {"asset_type": "fund-etf", "limit": 1, "offset": 0})
        for raw in (success(), success([{}]), success([{"thscode": "", "asset_type": "fund-etf"}]),
                    response({"code": 4001})):
            transport._gate.next_start = 0
            with patch.object(transport.requests, "get", return_value=raw) as get:
                self.assertFalse(provider.probe({"api_url": "https://example.test"}, {"api_key": "key"}).ok)
                self.assertEqual(get.call_count, 1)


class ConfigurationAndCatalogTest(unittest.TestCase):
    def test_validation_keeps_keys_separate_and_preserves_blank_existing_key(self):
        provider = TonghuashunProvider()
        values, secrets = provider.validate({"api_key": " "}, {"api_key": "saved-key"})
        self.assertEqual(values, {"api_url": "https://fuyao.aicubes.cn"})
        self.assertEqual(secrets, {"api_key": "saved-key"})
        for key in ("", "a\nb", "a\rb", "中文", "x" * 4097, False):
            with self.assertRaises(SourceError):
                provider.validate({"api_key": key}, {})
        for url in ("file:///tmp/x", "https://user:secret@host", "https://host?key=x", "https://host#x", "http://host:bad"):
            with self.assertRaises(SourceError):
                provider.validate({"api_key": "key", "api_url": url}, {})
        with self.assertRaises(SourceError):
            provider.validate({"token": "tushare-token"}, {})

    def test_catalog_is_complete_unique_and_not_a_permission_or_task_registry(self):
        items = list_source_interfaces("tonghuashun")["items"]
        self.assertEqual(len(items), 94)
        self.assertEqual(len({item["path"] for item in items}), len(items))
        self.assertEqual({item["group"] for item in items}, {"a-share", "a-share-index", "meta", "fund", "futures", "options", "dump"})
        for item in items:
            self.assertTrue(item["name"] and item["english_name"])
            self.assertEqual(item["method"], "GET")
            self.assertEqual(item["verification_status"], "not_verified")
            self.assertEqual(item["ingestion_status"], "not_implemented")
        self.assertIn("a-share.capital-flow.snapshot", INTERFACES)
        self.assertIn("dump.market-dumps.daily-k.download-url", INTERFACES)
        items[0]["name"] = "mutated"
        self.assertNotEqual(list_source_interfaces("tonghuashun")["items"][0]["name"], "mutated")
        with self.assertRaises(SourceError):
            list_source_interfaces("unknown")

    def test_migration_upgrades_existing_source_without_modifying_values(self):
        old = importlib.import_module("app.db.migrations.versions.20260913_01_data_source_configs")
        new = importlib.import_module("app.db.migrations.versions.20260918_01_tonghuashun_source")
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                old.upgrade()
                connection.execute(text("UPDATE data_source_configs SET encrypted_secrets='keep-ciphertext', version=7, initialized=true"))
                before = connection.execute(text("SELECT * FROM data_source_configs WHERE key='tushare'")).one()
                new.upgrade()
                self.assertEqual(connection.execute(text("SELECT * FROM data_source_configs WHERE key='tushare'")).one(), before)
                new_row = connection.execute(text("SELECT initialized, encrypted_secrets, version FROM data_source_configs WHERE key='tonghuashun'")).one()
                self.assertEqual(tuple(new_row), (0, None, 1))
                new.downgrade()
                self.assertEqual(connection.execute(text("SELECT * FROM data_source_configs")).one(), before)
        engine.dispose()
