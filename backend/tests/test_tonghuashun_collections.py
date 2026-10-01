"""Acquisition, publication and API regression tests with isolated source data."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
import importlib
import os
from uuid import uuid4
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.data_ingestion.clients.tonghuashun import TonghuashunError, TonghuashunResponse
from app.data_ingestion.models.tonghuashun import TonghuashunTicker as Ticker, TonghuashunObservation as Observation, TonghuashunCollectionState as State
from app.data_ingestion.tonghuashun.acquisition import Acquisition
from app.data_ingestion.tonghuashun.contracts import (
    ASSET_TYPES, DATASETS, CollectionError, CollectionParameters, date_ms, exact_json, validate_bars, windows,
)
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun.router import latest, observation_page, schedule_templates, tickers
from app.data_ingestion.tonghuashun.service import collect
from app.scheduling.registry import task_registry
from app.scheduling.schemas import schedule_adapter

NOW = datetime(2026, 9, 14, 14, tzinfo=UTC)


def reply(rows, **metadata):
    return TonghuashunResponse({"timestamp": 1720000000000, "item": rows, **metadata}, "trace")


def ticker(code="510300.SH", asset="fund-etf", **fields):
    return {"thscode": code, "asset_type": asset, "name": "测试基金", "exchange": "SH", "list_date": None, **fields}


def bar(day, price="1.234567890123456789"):
    value = Decimal(price)
    return {"date_ms": date_ms(day), "open_price": value, "high_price": value,
            "low_price": value, "close_price": value, "volume": 100, "turnover": Decimal("100.2")}


@pytest.fixture
def engine():
    if os.getenv("POSTGRES_TEST_ENABLED") == "1":
        from app.core.config import get_settings
        from app.data_ingestion.models.tonghuashun import TonghuashunRequestBudget
        # Each integration fixture owns a disposable schema. No test can touch
        # deployed data or interfere with another ingestion suite's rows.
        schema = "ths_test_" + uuid4().hex
        admin = create_engine(get_settings().database_url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(get_settings().database_url, connect_args={"options": f"-csearch_path={schema}"})
        for table in (Ticker.__table__, Observation.__table__, State.__table__, TonghuashunRequestBudget.__table__):
            table.create(engine)
        try:
            yield engine
        finally:
            engine.dispose()
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()
        return
    engine = create_engine("sqlite://")
    for table in (Ticker.__table__, Observation.__table__, State.__table__):
        table.create(engine)
    yield engine
    engine.dispose()


def seed(engine, rows):
    with Session(engine) as session:
        for row in rows:
            session.add(Ticker(thscode=row["thscode"], asset_type=row["asset_type"], name=row["name"],
                exchange=row["exchange"], raw_json=exact_json(row), first_seen_at=NOW, last_seen_at=NOW))
        session.commit()


def test_registry_and_templates_are_valid_and_source_owned():
    for spec in DATASETS.values():
        definition = task_registry.require(f"data.ths.{spec.key}")
        assert definition.source_key == "tonghuashun"
        assert definition.name and definition.english_name
        assert len(definition.key) <= 64
        for template in schedule_templates(spec):
            definition.parameters_model.model_validate(template["parameters"])
            schedule_adapter.validate_python(template["schedule"])


def test_snapshot_task_schema_rejects_unsupported_history_arguments():
    definition = task_registry.require("data.ths.index_constituents")
    with pytest.raises(ValueError):
        definition.parameters_model.model_validate({"start_date": "2020-01-01"})
    assert "start_date" not in definition.parameters_model.model_json_schema()["properties"]
    with pytest.raises(ValueError):
        task_registry.require("data.ths.stock_daily").parameters_model.model_validate({"asset_types": ["fund-etf"]})


def test_migration_roundtrip_leaves_existing_provider_rows_untouched():
    engine = create_engine("sqlite://")
    migration = importlib.import_module("app.db.migrations.versions.20260919_01_tonghuashun_collections")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE original_source (value TEXT)"))
        connection.execute(text("INSERT INTO original_source VALUES ('keep')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            names = set(connection.dialect.get_table_names(connection))
            assert {"tonghuashun_tickers", "tonghuashun_observations", "tonghuashun_collection_states", "tonghuashun_request_budget"} <= names
            migration.downgrade()
        assert connection.execute(text("SELECT value FROM original_source")).scalar() == "keep"
    engine.dispose()


def test_collection_and_version_routes_require_authentication():
    import asyncio
    from app.core.config import Settings
    from app.main import create_app
    from tests.test_auth import request_status
    app = create_app(Settings(api_token="a" * 64, database_password="test-secret", _env_file=None))
    prefix = "/api/admin/data-collections/tonghuashun"
    for path in ("/datasets", "/tickers", "/states", "/versions/00000000-0000-0000-0000-000000000000", "/fund_profile/510300.SH"):
        assert asyncio.run(request_status(app, prefix + path)) == 401
    assert asyncio.run(request_status(app, prefix + "/datasets", "a" * 64)) == 200


def test_date_backfill_cannot_mutate_default_collection_version(engine):
    seed(engine, [ticker()])
    client = Mock(interval_ms=0)
    client.request.return_value = reply([bar(date(2025, 1, 2))])
    parameters = CollectionParameters(subjects=["510300.SH"], start_date=date(2025, 1, 1), end_date=date(2025, 1, 5))
    collect("etf_daily", parameters, client, engine, now=NOW)
    with Session(engine) as session:
        repo = CollectionRepository(session)
        assert repo.read("etf_daily", "510300.SH", "default").data is None
        assert repo.read("etf_daily", "510300.SH", "2025-01-01_2025-01-05").data["item"][0]["date_ms"] == date_ms(date(2025, 1, 2))


def test_exact_decimal_and_null_roundtrip():
    source = {"value": Decimal("1234567890.12345678901234567890"), "unknown": None, "nested": [Decimal("0.00001")]}
    assert json.loads(exact_json(source), parse_float=Decimal) == source
    with pytest.raises(CollectionError):
        exact_json({"value": Decimal("NaN")})


def test_directory_pages_are_validated_before_any_write(engine):
    initial = ticker()
    seed(engine, [initial])
    first = [ticker(f"{i:06d}.SH") for i in range(10000)]
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply(first), reply([first[0]])]
    with pytest.raises(CollectionError, match="部分失败"):
        collect("tickers", CollectionParameters(asset_types=["fund-etf"]), client, engine, now=NOW)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Ticker)) == 1
        assert session.scalar(select(func.count()).select_from(Observation)) == 0
        state = session.get(State, ("tickers", "fund-etf", "default"))
        assert state.status == "failed" and state.observation_id is None


def test_directory_detects_snapshot_change_empty_and_asset_mismatch():
    for responses in (
        [reply([])],
        [reply([ticker(asset="fund-lof")])],
        [reply([ticker(f"{i:06d}.SH") for i in range(10000)]), reply([], timestamp=1)] * 3,
    ):
        client = Mock(interval_ms=0)
        client.request.side_effect = responses
        with pytest.raises(CollectionError):
            Acquisition(client).directory(DATASETS["tickers"], "fund-etf")


def test_directory_preserves_omissions_and_unknown_status(engine):
    seed(engine, [ticker("510500.SH")])
    client = Mock(interval_ms=0)
    client.request.return_value = reply([ticker()])
    result = collect("tickers", CollectionParameters(asset_types=["fund-etf"]), client, engine, now=NOW)
    assert result["received"] == 1
    with Session(engine) as session:
        page = tickers(asset_type=None, keyword=None, limit=50, offset=0, session=session)
        assert page["total"] == 2
        assert all(row["list_status"] is None for row in page["items"])
        assert session.get(Ticker, "510500.SH").last_seen_at.replace(tzinfo=UTC) == NOW


def test_partial_failure_preserves_success_and_retry_skips_completed(engine):
    seed(engine, [ticker(), ticker("510500.SH")])
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply([{"thscode": "510300.SH", "unit_nav": Decimal("1.01")}]), TonghuashunError("data_not_ready")]
    with pytest.raises(CollectionError, match="成功 1 个，失败 1 个"):
        collect("fund_profile", CollectionParameters(), client, engine, now=NOW)
    client.request.reset_mock()
    client.request.side_effect = [reply([{"thscode": "510500.SH"}])]
    result = collect("fund_profile", CollectionParameters(), client, engine, now=NOW)
    assert result["skipped"] == 1 and result["succeeded"] == 1
    assert client.request.call_count == 1


def test_auth_failure_does_not_hammer_remaining_subjects(engine):
    seed(engine, [ticker(), ticker("510500.SH")])
    client = Mock(interval_ms=0)
    client.request.side_effect = TonghuashunError("forbidden")
    with pytest.raises(CollectionError, match="剩余 1 个未执行"):
        collect("fund_profile", CollectionParameters(), client, engine, now=NOW)
    assert client.request.call_count == 1


def test_snapshot_identity_mismatch_does_not_publish(engine):
    seed(engine, [ticker()])
    client = Mock(interval_ms=0)
    client.request.return_value = reply([{"thscode": "510500.SH"}])
    with pytest.raises(CollectionError):
        collect("fund_profile", CollectionParameters(), client, engine, now=NOW)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Observation)) == 0


def test_revision_is_immutable_and_failed_refresh_keeps_last_good(engine):
    seed(engine, [ticker()])
    client = Mock(interval_ms=0)
    params = CollectionParameters(subjects=["510300.SH"])
    for nav in ("1.1234567890123456789", "1.2"):
        client.request.return_value = reply([{"thscode": "510300.SH", "unit_nav": Decimal(nav)}])
        collect("fund_profile", params, client, engine, now=NOW)
    client.request.side_effect = TonghuashunError("data_not_ready")
    with pytest.raises(CollectionError):
        collect("fund_profile", params, client, engine, now=NOW)
    with Session(engine) as session:
        rows = list(session.scalars(select(Observation).order_by(Observation.observed_at)))
        assert len(rows) == 2
        assert observation_page(rows[0], 50, 0)["items"][0]["unit_nav"] == "1.1234567890123456789"
        result = latest("fund_profile", "510300.SH", limit=50, offset=0, session=session)
        assert result["latest_attempt_status"] == "failed"
        assert result["items"][0]["unit_nav"] == "1.2"


def test_stale_publication_cannot_overwrite_newer_observation(engine):
    with Session(engine) as session:
        repo = CollectionRepository(session)
        repo.publish("fund_profile", "510300.SH", "default", expected=0,
            data={"item": [{"unit_nav": 1}]}, requests=[], now=NOW)
        session.commit()
        with pytest.raises(CollectionError, match="其他运行"):
            repo.publish("fund_profile", "510300.SH", "default", expected=0,
                data={"item": [{"unit_nav": 2}]}, requests=[], now=NOW)
        session.rollback()
        assert repo.read("fund_profile", "510300.SH", "default").data["item"][0]["unit_nav"] == 1


def test_date_windows_cover_leap_year_without_overlap():
    result = list(windows(date(2010, 1, 1), date(2026, 9, 14), 5))
    assert result[0][0] == date(2010, 1, 1) and result[-1][1] == date(2026, 9, 14)
    for (a, b), (c, d) in zip(result, result[1:]):
        assert c == b + timedelta(days=1)
        assert (b - a).days < 365 * 5


def test_invalid_ohlc_and_duplicates_are_rejected():
    day = date(2026, 9, 1)
    for rows in ([bar(day), bar(day)], [{**bar(day), "high_price": Decimal("0.1")}], [{**bar(day), "volume": -1}]):
        with pytest.raises(CollectionError):
            validate_bars(rows, day, day)


@pytest.mark.parametrize("value,reason", [
    (None, "缺失"), (True, "布尔类型"), (-1, "负值"),
    ("private-key-do-not-echo", "非数值类型"),
    (1.5, "非数值类型"), (Decimal("NaN"), "非有限数值"),
    (Decimal("Infinity"), "非有限数值"),
])
def test_bar_failure_identifies_safe_field_date_and_category(value, reason):
    day = date(2026, 9, 1)
    with pytest.raises(CollectionError) as raised:
        validate_bars([{**bar(day), "volume": value}], day, day)
    message = str(raised.value)
    assert "2026-09-01" in message and "volume" in message and reason in message
    assert "private-key" not in message


def test_nav_diagnostic_preserves_nullable_contract_and_reports_invalid_field():
    day = date(2026, 9, 1)
    row = {"nav_date": date_ms(day), "unit_nav": None, "adj_nav": Decimal("1.2")}
    validate_bars([row], day, day, "nav_date")
    with pytest.raises(CollectionError, match="adj_nav.*负值"):
        validate_bars([{**row, "adj_nav": -1}], day, day, "nav_date")


def test_etf_revision_refetches_full_history_before_publishing():
    old = {"item": [bar(date(2020, 1, 2)), bar(date(2026, 9, 1))]}
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply([bar(date(2026, 9, 1), "2")]),
                                 reply([bar(date(2020, 1, 2), "2")]),
                                 reply([bar(date(2026, 9, 1), "2")])]
    # Use more than ten old dates so the overlap starts after the original head.
    old["item"] = [bar(date(2020, 1, 2))] + [bar(date(2026, 8, i)) for i in range(1, 20)]
    client.request.side_effect = [reply([bar(date(2026, 8, 15), "2")]), reply([bar(date(2020, 1, 2), "2")]), reply([bar(date(2026, 8, i), "2") for i in range(1, 20)])]
    result = Acquisition(client).fetch(DATASETS["etf_daily"], "510300.SH", CollectionParameters(), old, NOW)
    assert client.request.call_count == 3
    assert result["adjust"] == "forward"
    assert result["item"][0]["close_price"] == 2


def test_partial_multisegment_etf_refetch_leaves_old_version(engine):
    seed(engine, [ticker()])
    with Session(engine) as session:
        CollectionRepository(session).publish("etf_daily", "510300.SH", "default", expected=0,
            data={"item": [bar(date(2020, 1, 2))]}, requests=[], now=NOW)
        session.commit()
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply([bar(date(2020, 1, 2), "2")]), TonghuashunError("timeout")]
    with pytest.raises(CollectionError):
        collect("etf_daily", CollectionParameters(mode="reconcile", subjects=["510300.SH"]), client, engine, now=NOW)
    with Session(engine) as session:
        assert CollectionRepository(session).read("etf_daily", "510300.SH", "default").data["item"][0]["close_price"] != 2


def test_stock_requests_explicit_unadjusted_and_index_has_no_adjust():
    for key in ("stock_daily", "index_daily"):
        client = Mock(interval_ms=0)
        client.request.side_effect = [reply([bar(date(2026, 9, 1))]), reply([])]
        Acquisition(client).fetch(DATASETS[key], "000001.SH", CollectionParameters(), None, NOW)
        params = client.request.call_args.args[1]
        assert params.get("adjust") == ("none" if key == "stock_daily" else None)


def test_nav_does_not_confuse_adjusted_and_accumulated_nav():
    client = Mock(interval_ms=0)
    client.request.return_value = reply([{"nav_date": date_ms(date(2026, 9, 1)), "unit_nav": Decimal("1.1"), "adj_nav": Decimal("2.2")}])
    result = Acquisition(client).fetch(DATASETS["fund_nav"], "510300.SH", CollectionParameters(), None, NOW)
    assert client.request.call_args.args[1]["nav_type"] == "unit,adj"
    assert result["item"][0]["adj_nav"] == Decimal("2.2")
    assert "accumulated_nav" not in result["item"][0]


def test_financials_preserve_disclosure_and_period_dates():
    client = Mock(interval_ms=0)
    row = {"thscode": "600519.SH", "period_end_ms": date_ms(date(2025, 12, 31)), "report_date_ms": date_ms(date(2026, 3, 28)), "profit": None}
    # A ten-year range is split conservatively around leap days.
    client.request.side_effect = [reply([row]), reply([])]
    result = Acquisition(client).fetch(DATASETS["stock_income"], "600519.SH", CollectionParameters(), None, NOW)
    assert result["item"] == [row]
    assert result["historical_revision_evidence"] is False


def test_legal_empty_dividends_are_preserved_as_empty_observation(engine):
    seed(engine, [ticker()])
    client = Mock(interval_ms=0)
    client.request.return_value = reply([])
    result = collect("fund_dividends", CollectionParameters(), client, engine, now=NOW)
    assert result["succeeded"] == 1 and result["received"] == 0
    with Session(engine) as session:
        assert session.get(State, ("fund_dividends", "510300.SH", "default")).status == "empty"


def test_history_parameters_cannot_silently_turn_into_current_snapshot(engine):
    client = Mock(interval_ms=0)
    with pytest.raises(CollectionError, match="不支持指定日期"):
        collect("index_constituents", CollectionParameters(start_date=date(2020, 1, 1)), client, engine, now=NOW)
    client.request.assert_not_called()


def test_failed_old_report_stays_retryable_after_initial_backfill_window():
    client = Mock(interval_ms=0)
    old = {"item": [], "failed_requests": [{"interface": "a-share.financials.indicators",
        "parameters": {"thscode": "600519.SH", "report": "2017-1"}, "error_kind": "data_not_ready"}]}
    def fetch(_, params):
        return TonghuashunResponse({"thscode": params["thscode"], "report": params["report"], "abilities": []}, "trace")
    client.request.side_effect = fetch
    result = Acquisition(client).fetch(DATASETS["stock_indicators"], "600519.SH", CollectionParameters(), old, NOW)
    assert "2017-1" in {row["report"] for row in result["item"]}


def test_partial_report_versions_preserve_success_without_advancing_complete_scope(engine):
    seed(engine, [ticker("600519.SH", "a-share")])
    client = Mock(interval_ms=0)
    params = CollectionParameters(subjects=["600519.SH"], start_date=date(2025, 1, 1), end_date=date(2025, 6, 30))
    def fetch(_, request):
        if request["report"] == "2025-2":
            raise TonghuashunError("data_not_ready")
        return TonghuashunResponse({"thscode": "600519.SH", "report": request["report"], "abilities": []}, "trace")
    client.request.side_effect = fetch
    with pytest.raises(CollectionError, match="部分失败"):
        collect("stock_indicators", params, client, engine, now=NOW)
    with Session(engine) as session:
        previous = CollectionRepository(session).read("stock_indicators", "600519.SH", "2025-01-01_2025-06-30")
        assert previous.status == "partial" and previous.succeeded_at is None
        assert previous.data["item"][0]["report"] == "2025-1"
        assert previous.data["failed_requests"][0]["parameters"]["report"] == "2025-2"


def test_night_reconciliation_does_not_suppress_evening_daily_refresh():
    from app.data_ingestion.tonghuashun.repository import Previous
    from app.data_ingestion.tonghuashun.service import due
    previous = Previous(1, {"item": []}, datetime(2026, 9, 14, 0, tzinfo=UTC), "succeeded")
    assert due(DATASETS["stock_daily"], previous, CollectionParameters(), NOW)
    assert not due(DATASETS["stock_daily"], Previous(2, {"item": []}, NOW, "succeeded"), CollectionParameters(), NOW)


def test_bounded_batches_rotate_past_failing_subjects(engine):
    seed(engine, [ticker(), ticker("510500.SH")])
    client = Mock(interval_ms=0)
    client.request.side_effect = TonghuashunError("data_not_ready")
    with pytest.raises(CollectionError):
        collect("fund_profile", CollectionParameters(batch_size=1), client, engine, now=NOW)
    client.request.side_effect = None
    client.request.return_value = reply([{"thscode": "510500.SH"}])
    result = collect("fund_profile", CollectionParameters(batch_size=1), client, engine, now=NOW)
    assert result["succeeded"] == 1 and result["pending"] == 1
    assert client.request.call_args.args[1]["thscode"] == "510500.SH"


def test_company_and_manager_ids_are_derived_from_source_profiles_only(engine):
    seed(engine, [ticker()])
    client = Mock(interval_ms=0)
    client.request.return_value = reply([{"thscode": "510300.SH", "company_id": "00089990",
        "manager_info": [{"manager_id": "H000200384"}, {"manager_id": "H000200384"}]}])
    collect("fund_profile", CollectionParameters(), client, engine, now=NOW)
    with Session(engine) as session:
        repo = CollectionRepository(session)
        assert repo.related("company_id") == ["00089990"]
        assert repo.related("manager_id") == ["H000200384"]


def test_constituent_changes_are_visible_in_separate_observation_versions(engine):
    seed(engine, [ticker("000300.SH", "a-share-index")])
    client = Mock(interval_ms=0)
    params = CollectionParameters(subjects=["000300.SH"])
    for codes in (["600519.SH", "000001.SZ"], ["600519.SH", "600000.SH"]):
        client.request.return_value = reply([{"thscode": code} for code in codes])
        collect("index_constituents", params, client, engine, now=NOW)
    with Session(engine) as session:
        rows = list(session.scalars(select(Observation).order_by(Observation.observed_at)))
        assert len(rows) == 2
        assert {r["thscode"] for r in json.loads(rows[0].data_json)["item"]} == {"600519.SH", "000001.SZ"}
        assert {r["thscode"] for r in json.loads(rows[1].data_json)["item"]} == {"600519.SH", "600000.SH"}


@pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL")
def test_database_budget_serializes_independent_workers(engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock
    import time
    from app.data_ingestion.tonghuashun.service import BudgetedClient
    starts, lock = [], Lock()
    def request(*_):
        with lock:
            starts.append(time.monotonic())
        time.sleep(0.005)
        return reply([])
    def work():
        client = Mock(interval_ms=20)
        client.request.side_effect = request
        for _ in range(2):
            BudgetedClient(client, engine).request("meta.tickers.list", {})
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: work(), range(2)))
    assert len(starts) == 4
    assert all(b - a >= 0.02 for a, b in zip(starts, starts[1:]))


def test_series_delta_versions_are_exact_bounded_and_readable_by_fixed_id(engine):
    from app.data_ingestion.tonghuashun.repository import materialize
    with Session(engine) as session:
        repo = CollectionRepository(session)
        rows = [bar(date(2020, 1, 1) + timedelta(days=i)) for i in range(100)]
        first = repo.publish("etf_daily", "510300.SH", "default", expected=0,
            data={"item": rows, "adjust": "forward"}, requests=[], now=NOW)
        session.commit()
        from uuid import UUID
        first_id = UUID(first["version_id"])
        original = materialize(session, session.get(Observation, first_id))
        for revision in range(1, 34):
            rows = rows + [bar(date(2020, 1, 1) + timedelta(days=99 + revision))]
            repo.publish("etf_daily", "510300.SH", "default", expected=revision,
                data={"item": rows, "adjust": "forward"}, requests=[], now=NOW + timedelta(seconds=revision))
            session.commit()
        versions = list(session.scalars(select(Observation).order_by(Observation.observed_at)))
        assert versions[1].base_observation_id == first_id
        assert len(versions[1].data_json) < len(versions[0].data_json) / 10
        assert max(version.chain_depth for version in versions) == 30
        assert versions[31].base_observation_id is None
        assert repo.read("etf_daily", "510300.SH", "default").data["item"] == rows
        assert materialize(session, session.get(Observation, first_id)) == original
        page = observation_page(versions[-1], 1000, 0, session)
        assert page["total"] == 133
        assert page["items"][0]["open_price"] == "1.234567890123456789"


def test_delta_preserves_decimal_representation_changes_and_removals(engine):
    with Session(engine) as session:
        repo = CollectionRepository(session)
        rows = [bar(date(2020, 1, 1) + timedelta(days=i), "1.00") for i in range(100)]
        repo.publish("etf_daily", "510300.SH", "default", expected=0,
            data={"item": rows}, requests=[], now=NOW)
        session.commit()
        changed = [{**rows[0], "close_price": Decimal("1.0000")}] + rows[1:-1]
        repo.publish("etf_daily", "510300.SH", "default", expected=1,
            data={"item": changed}, requests=[], now=NOW + timedelta(seconds=1))
        session.commit()
        data = repo.read("etf_daily", "510300.SH", "default").data
        assert str(data["item"][0]["close_price"]) == "1.0000"
        assert len(data["item"]) == 99


def test_directory_restarts_snapshot_without_mixing_attempts():
    first = [ticker(f"{i:06d}.SH") for i in range(10000)]
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply(first), reply([], timestamp=1),
                                  reply([ticker("510300.SH")], timestamp=2)]
    data = Acquisition(client).directory(DATASETS["tickers"], "fund-etf")
    assert data["timestamp"] == 2 and data["item"] == [ticker("510300.SH")]
    assert [call.args[1]["offset"] for call in client.request.call_args_list] == [0, 10000, 0]
    assert all(call.args[1]["limit"] == 10000 for call in client.request.call_args_list)


def test_automatic_scope_uses_last_complete_directory_without_deleting_history(engine):
    live = ticker('000001.SH', 'a-share-index')
    retired = ticker('950084.SH', 'a-share-index')
    seed(engine, [live, retired])
    with Session(engine) as session:
        repo = CollectionRepository(session)
        repo.publish('tickers', 'a-share-index', 'default', expected=0,
                     data={'item': [live]}, requests=[], now=NOW, ticker_rows=[live])
        session.commit()
        assert repo.subjects(('a-share-index',), current_only=True) == ['000001.SH']
        assert repo.subjects(('a-share-index',)) == ['000001.SH', '950084.SH']
        # A failed refresh must neither empty the scope nor resurrect retired
        # subjects: the previously published complete snapshot remains trusted.
        repo.fail('tickers', 'a-share-index', 'default', expected=1, kind='timeout', now=NOW)
        session.commit()
        assert repo.subjects(('a-share-index',), current_only=True) == ['000001.SH']
    c = Mock(interval_ms=0)
    c.request.side_effect = lambda key, p: reply([{'thscode': p['thscodes']}])
    assert collect('index_quote', CollectionParameters(), c, engine, now=NOW)['subjects'] == 1
    assert c.request.call_args.args[1] == {'thscodes': '000001.SH'}
    assert collect('index_quote', CollectionParameters(subjects=['950084.SH']), c, engine, now=NOW)['subjects'] == 1
    assert c.request.call_args.args[1] == {'thscodes': '950084.SH'}

def _index_empty_wide_client(days):
    """Reproduce real index wide-window omission with valid annual responses."""
    from app.data_ingestion.tonghuashun.contracts import provider_date
    client = Mock(interval_ms=0)

    def request(interface, params):
        assert interface == DATASETS["index_daily"].interface
        start, end = provider_date(params["start"]), provider_date(params["end"])
        rows = [bar(day) for day in days if start <= day <= end] if (end - start).days < 365 else []
        return reply(rows)

    client.request.side_effect = request
    return client


def test_index_empty_wide_window_replays_entire_history_in_annual_segments():
    from app.data_ingestion.tonghuashun.contracts import provider_date
    days = [date(2017, 3, 1), date(2024, 10, 6), date(2026, 9, 3)]
    client = _index_empty_wide_client(days)
    acquisition = Acquisition(client)
    result = acquisition.fetch(DATASETS["index_daily"], "000300.SH", CollectionParameters(), None, NOW)
    assert result["item"] == [bar(day) for day in days]
    assert result["requested_start"] == "2016-09-14"
    assert result["requested_end"] == "2026-09-14"
    assert result["coverage"] == "observed_rows_only"
    annual = [call.args[1] for call in client.request.call_args_list if call.args[1]["end"] - call.args[1]["start"] <= 364 * 86400000]
    # The final ordinary short tail is not part of the annual re-read.
    annual = annual[1:]
    assert provider_date(annual[0]["start"]) == date(2016, 9, 14)
    assert provider_date(annual[-1]["end"]) == date(2026, 9, 14)
    for previous, current in zip(annual, annual[1:]):
        assert provider_date(current["start"]) == provider_date(previous["end"]) + timedelta(days=1)
    assert all("adjust" not in call.args[1] for call in client.request.call_args_list)
    assert all(request["key_receipt"] == "actual_returned_keys_v1" for request in acquisition.requests)
    confirmed = {key for request in acquisition.requests for key in request["returned_keys"].get("date_ms", [])}
    assert confirmed == {date_ms(day) for day in days}


def test_index_empty_wide_fallback_publishes_actual_rows_and_receipts(engine):
    seed(engine, [ticker("000300.SH", "a-share-index")])
    with Session(engine) as session:
        CollectionRepository(session).fail("index_daily", "000300.SH", "default", expected=0,
            kind="invalid_data", now=NOW)
        session.commit()
    days = [date(2017, 3, 1), date(2024, 10, 6), date(2026, 9, 3)]
    result = collect("index_daily", CollectionParameters(subjects=["000300.SH"]),
        _index_empty_wide_client(days), engine, now=NOW)
    assert result["succeeded"] == 1 and result["received"] == len(days)
    with Session(engine) as session:
        state = session.get(State, ("index_daily", "000300.SH", "default"))
        assert state.status == "succeeded" and state.revision == 2 and state.error_kind is None
        observation = session.get(Observation, state.observation_id)
        assert observation.row_count == len(days)
        requests = json.loads(observation.request_json)
        assert all(request["key_receipt"] == "actual_returned_keys_v1" for request in requests)
        previous = CollectionRepository(session).read("index_daily", "000300.SH", "default")
        assert previous.data["item"] == [bar(day) for day in days]
        assert previous.data["requested_start"] == "2016-09-14"


def test_genuinely_empty_index_fallback_never_advances_success(engine):
    seed(engine, [ticker("000300.SH", "a-share-index")])
    client = _index_empty_wide_client([])
    with pytest.raises(CollectionError):
        collect("index_daily", CollectionParameters(subjects=["000300.SH"]), client, engine, now=NOW)
    with Session(engine) as session:
        state = session.get(State, ("index_daily", "000300.SH", "default"))
        assert state.status == "failed" and state.error_kind == "invalid_data"
        assert state.observation_id is None and state.succeeded_at is None
        assert session.scalar(select(func.count()).select_from(Observation)) == 0
    # A finite ten-year range needs at most two normal plus eleven annual reads.
    assert client.request.call_count == 13


def test_index_annual_fallback_yield_resumes_exact_cached_windows(engine):
    from app.data_ingestion.models.tonghuashun import TonghuashunWorkUnit
    from app.data_ingestion.tonghuashun.control import CollectionControl, CollectionYield, active_control
    TonghuashunWorkUnit.__table__.create(engine)
    seed(engine, [ticker("000300.SH", "a-share-index")])
    with Session(engine) as session:
        CollectionRepository(session).fail("index_daily", "000300.SH", "default", expected=0,
            kind="invalid_data", now=NOW)
        session.commit()
    days = [date(2017, 3, 1), date(2024, 10, 6), date(2026, 9, 3)]
    client = _index_empty_wide_client(days)
    parameters = CollectionParameters(subjects=["000300.SH"])
    monitor = CollectionControl(engine, None, max_requests=3, max_seconds=180)
    token = active_control.set(monitor)
    try:
        with pytest.raises(CollectionYield):
            collect("index_daily", parameters, client, engine, now=NOW)
    finally:
        active_control.reset(token)
    with Session(engine) as session:
        state = session.get(State, ("index_daily", "000300.SH", "default"))
        assert state.revision == 1 and state.status == "failed" and state.observation_id is None
        assert session.scalar(select(func.count()).select_from(TonghuashunWorkUnit)) == 3
    assert client.request.call_count == 3
    resumed = CollectionControl(engine, None, max_requests=40, max_seconds=180)
    token = active_control.set(resumed)
    try:
        result = collect("index_daily", parameters, client, engine, now=NOW)
    finally:
        active_control.reset(token)
    assert result["received"] == len(days) and result["succeeded"] == 1
    # The ordinary short tail is also an identical annual request: reuse it.
    assert resumed.detail["reused"] == 4 and client.request.call_count == 12
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(TonghuashunWorkUnit)) == 0
        assert CollectionRepository(session).read("index_daily", "000300.SH", "default").data["item"] == [bar(day) for day in days]


@pytest.mark.parametrize("key", ["stock_daily", "etf_daily"])
def test_empty_non_index_bars_do_not_add_annual_fallback(key):
    client = Mock(interval_ms=0)
    client.request.return_value = reply([])
    with pytest.raises(CollectionError):
        Acquisition(client).fetch(DATASETS[key], "000001.SH", CollectionParameters(), None, NOW)
    assert client.request.call_count == 2


@pytest.mark.parametrize("failure", ["invalid_parameters", "rate_limited", "upstream_error"])
def test_index_request_errors_never_become_empty_fallback(failure):
    client = Mock(interval_ms=0)
    client.request.side_effect = TonghuashunError(failure)
    with pytest.raises(TonghuashunError) as captured:
        Acquisition(client).fetch(DATASETS["index_daily"], "000300.SH", CollectionParameters(), None, NOW)
    assert captured.value.kind == failure and client.request.call_count == 1

@pytest.mark.parametrize("malformed", ["wrong_subject", "negative_value", "outside_window"])
def test_invalid_annual_index_response_never_publishes_partial_source(engine, malformed):
    seed(engine, [ticker("000300.SH", "a-share-index")])
    row = bar(date(2017, 3, 1))
    metadata = {}
    if malformed == "wrong_subject":
        metadata["thscode"] = "399001.SZ"
    elif malformed == "negative_value":
        row["volume"] = -1
    else:
        row = bar(date(2026, 9, 1))
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply([]), reply([]), reply([row], **metadata)]
    with pytest.raises(CollectionError):
        collect("index_daily", CollectionParameters(subjects=["000300.SH"]), client, engine, now=NOW)
    with Session(engine) as session:
        state = session.get(State, ("index_daily", "000300.SH", "default"))
        assert state.status == "failed" and state.error_kind == "invalid_data"
        assert state.observation_id is None and state.succeeded_at is None
        assert session.scalar(select(func.count()).select_from(Observation)) == 0
