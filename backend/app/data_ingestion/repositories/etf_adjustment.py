"""PostgreSQL persistence for current ETF adjustment factors."""

from collections.abc import Iterable

from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_ingestion.schemas.etf_adjustment import (
    EtfAdjustmentFactorInput,
    EtfAdjustmentFactorUpsertResult,
)


class EtfAdjustmentFactorRepository:
    """Upsert current source factors without managing the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_factors(
        self,
        records: Iterable[EtfAdjustmentFactorInput],
        *,
        source: str,
    ) -> EtfAdjustmentFactorUpsertResult:
        """Insert factors and overwrite only values corrected by the source."""
        factors = list(records)
        self._reject_duplicate_keys(factors)
        if not factors:
            return EtfAdjustmentFactorUpsertResult(received=0, changed=0, unchanged=0)

        table = EtfAdjustmentFactor.__table__
        statement = insert(table).values(
            [
                {
                    "source": source,
                    "ts_code": factor.ts_code,
                    "trade_date": factor.trade_date,
                    "adj_factor": factor.adj_factor,
                }
                for factor in factors
            ]
        )
        excluded = statement.excluded
        changed_fields = or_(
            table.c.adj_factor.is_distinct_from(excluded.adj_factor),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.source, table.c.ts_code, table.c.trade_date],
            set_={
                "adj_factor": excluded.adj_factor,
                "updated_at": func.now(),
            },
            where=changed_fields,
        ).returning(table.c.ts_code)
        changed = len(self.session.execute(statement).all())
        return EtfAdjustmentFactorUpsertResult(
            received=len(factors),
            changed=changed,
            unchanged=len(factors) - changed,
        )

    @staticmethod
    def _reject_duplicate_keys(factors: list[EtfAdjustmentFactorInput]) -> None:
        keys = [(factor.ts_code, factor.trade_date) for factor in factors]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "ETF adjustment input contains duplicate ts_code and trade_date"
            )
