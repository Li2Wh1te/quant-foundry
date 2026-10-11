"""Original run roots, read verbatim with original owner and visibility ACLs."""
import json
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Run
from ..repository import identity, summary
from ..runtime_contracts import RuntimeFault
from ..serialization import encoded
from .storage_models import BacktestRunRecord


def legacy_root(session, owner, run_id):
    row = session.scalar(select(BacktestRunRecord).where(
        BacktestRunRecord.id == identity(run_id), BacktestRunRecord.idempotency_scope == owner,
        BacktestRunRecord.run_kind == "backtest_run", BacktestRunRecord.profile == "formal@1"))
    if row is None:
        raise RuntimeFault("DATA_RESTRICTED", "历史运行不存在或无权访问")
    return row


def legacy_summary(row):
    # Retain original values, identifiers and stored configuration. No legacy
    # executor/analyzer or attempts to reinterpret failed/indeterminate results.
    return json.loads(encoded(dict(run_id=str(row.id), id=str(row.id), engine_kind="legacy_python",
                api_schema="legacy", status=row.status, partial=row.status != "succeeded",
                created_at=row.created_at, finished_at=row.finished_at,
                strategy_revision_id=row.strategy_revision_id,
                account_profile_id=row.account_profile_id,
                account_profile_version=row.account_profile_version,
                initial_cash=row.initial_cash, backtest_config=row.backtest_config,
                result_summary=row.result_summary, result_counts=row.result_counts)))


def read_run(repository, owner, run_id):
    with Session(repository.engine) as session:
        row = session.scalar(select(Run).where(Run.id == identity(run_id), Run.owner == owner))
        if row is not None:
            return summary({c.name: getattr(row, c.name) for c in Run.__table__.columns})
        return legacy_summary(legacy_root(session, owner, run_id))
