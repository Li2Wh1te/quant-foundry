"""D16 private saved-value samples, deliberately synthetic, never production."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy.orm import Session
from fastapi import Depends, HTTPException
from app.core.auth import require_api_token
from app.backtest_service.history.storage_models import BacktestRunRecord
from app.backtest_service.history.result_records import BacktestEquityCurveRecord, BacktestMetricRecord
from app.backtest_service.history.saved_comparisons import SavedComparison, SavedComparisonMember
from s3_runservice_suite import OWNER


def seed(fixture):
    amount = Decimal("7922816251426433759.123456789012345678")
    identity, partial_id, saved_id = uuid4(), uuid4(), uuid4()
    with Session(fixture.engine) as session:
        old = session.get(BacktestRunRecord, fixture.old_run)
        for run_id, status in ((identity, "succeeded"), (partial_id, "failed")):
            values = {column.name: getattr(old, column.name) for column in BacktestRunRecord.__table__.columns}
            values.update(id=run_id, idempotency_key=uuid4().hex, status=status, terminal_status=status,
                backtest_config={"spec": {"start_date": "2026-01-01", "end_date": "2026-05-01", "frequency": "1d", "currency": "CNY", "timezone": "Asia/Shanghai", "initial_cash": str(amount)}},
                result_summary={"synthetic": True, "value": str(amount), "time_ns": "1767225600000000123", "original_text": "原历史说明保持"})
            session.add(BacktestRunRecord(**values)); session.flush()
            for index in range(105):
                missing = index == 3
                session.add(BacktestEquityCurveRecord(run_id=run_id, sequence=index + 1,
                    as_of=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index),
                    valuation_status="blocked" if missing else "complete", valuation_reason="SYNTHETIC_MISSING_PRICE" if missing else None,
                    cash=amount, market_value=None if missing else Decimal("0"), equity=None if missing else amount,
                    cumulative_fees=Decimal("1.000000000000000001")))
            session.add(BacktestMetricRecord(run_id=run_id, metric_key="total_return", formula_version="original-synthetic-formula",
                value=Decimal("0.123456789012345678"), unit="ratio", sample_count=105))
            session.add(BacktestMetricRecord(run_id=run_id, metric_key="sharpe", formula_version="original-synthetic-formula",
                value=None, unavailable_reason="SYNTHETIC_MISSING_PRICE", unit="ratio", sample_count=105))
        session.add(SavedComparison(id=saved_id, owner_scope=OWNER, name="原保存比较 · 只读", baseline_run_id=fixture.old_run, version=1))
        session.flush()
        session.add_all([SavedComparisonMember(comparison_id=saved_id, run_id=run_id, position=index)
            for index, run_id in enumerate((fixture.old_run, identity))])
        session.commit()
    # Same private authenticated-principal switch as D13's HTTP owner test.
    # Saved run ownership is immutable; do not change rows or DB constraints.
    from pydantic import SecretStr
    original_settings = fixture.app.state.settings
    other_token = "d16-private-other-owner-" + "c" * 40
    @fixture.app.post("/api/test/d16/access", dependencies=[Depends(require_api_token)])
    def access(payload: dict):
        action = payload.get("action")
        if action not in ("deny", "restore"):
            raise HTTPException(422, "Invalid private access control")
        fixture.app.state.settings = original_settings.model_copy(update={"api_token": SecretStr(other_token)}) if action == "deny" else original_settings
        return {"synthetic": True, "production_executed": False, "action": action}
    return dict(decimal_run_id=str(identity), partial_legacy_run_id=str(partial_id), saved_comparison_id=str(saved_id), exact_amount=str(amount), access_control="private_authenticated_owner_switch", other_token=other_token)
