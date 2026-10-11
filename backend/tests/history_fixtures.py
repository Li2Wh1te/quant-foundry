"""Authoritative roots for portable old-result fixtures (production uses PG)."""
from sqlalchemy import Column, JSON, MetaData, Table
from sqlalchemy.dialects.postgresql import JSONB
from app.backtest_service.history.storage_models import BacktestRunRecord


def history_root(engine, run_id, *, kind="backtest_run", profile="formal@1", owner="default"):
    root = Table("backtest_runs", MetaData(), *[
        Column(c.name, JSON() if isinstance(c.type, JSONB) else c.type,
               primary_key=c.primary_key, nullable=True)
        for c in BacktestRunRecord.__table__.columns])
    root.create(engine, checkfirst=True)
    with engine.begin() as connection:
        connection.execute(root.insert().values(id=run_id, run_kind=kind,
            profile=profile, status="succeeded", idempotency_scope=owner))
