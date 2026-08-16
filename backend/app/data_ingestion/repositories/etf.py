"""PostgreSQL persistence for ETF reference data."""

from collections.abc import Iterable
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.data_ingestion.models.etf import EtfCode, EtfCodeMappingAudit, EtfEntity
from app.data_ingestion.schemas.etf import EtfBasicUpsertResult, EtfInstrumentInput


class EtfCodeRepository:
    """Write ETF code snapshots and explicit entity mappings without transactions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_codes(
        self,
        records: Iterable[EtfInstrumentInput],
        *,
        source: str,
        observed_at: datetime,
    ) -> EtfBasicUpsertResult:
        """Insert new trading codes and update only changed source fields.

        Each newly observed code receives a new local ETF entity. A code change is
        never guessed from mutable source fields; a later verified mapping may
        reassign the code to an existing entity through ``reassign_code_entity``.
        Missing source rows are never deleted because an upstream omission must not
        erase a delisted ETF or a historical identifier.
        """
        codes = list(records)
        self._reject_duplicate_codes(codes)
        if not codes:
            return EtfBasicUpsertResult(received=0, changed=0, unchanged=0)

        table = EtfCode.__table__
        known_entities = self._existing_entities(
            source=source, ts_codes=[code.ts_code for code in codes]
        )
        new_entities = {
            code.ts_code: uuid4()
            for code in codes
            if code.ts_code not in known_entities
        }
        if new_entities:
            self.session.execute(
                insert(EtfEntity.__table__).values(
                    [{"id": entity_id} for entity_id in new_entities.values()]
                )
            )
        values = [
            {
                "source": source,
                "ts_code": code.ts_code,
                "etf_id": known_entities.get(code.ts_code)
                or new_entities[code.ts_code],
                "csname": code.csname,
                "extname": code.extname,
                "cname": code.cname,
                "index_code": code.index_code,
                "index_name": code.index_name,
                "setup_date": code.setup_date,
                "list_date": code.list_date,
                "list_status": code.list_status,
                "exchange": code.exchange,
                "mgr_name": code.mgr_name,
                "custod_name": code.custod_name,
                "mgt_fee": code.mgt_fee,
                "etf_type": code.etf_type,
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
            }
            for code in codes
        ]
        statement = insert(table).values(values)
        excluded = statement.excluded
        source_columns = (
            "csname", "extname", "cname", "index_code", "index_name", "setup_date",
            "list_date", "list_status", "exchange", "mgr_name", "custod_name",
            "mgt_fee", "etf_type",
        )
        changed_fields = or_(
            *(getattr(table.c, column).is_distinct_from(getattr(excluded, column))
              for column in source_columns)
        )
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.source, table.c.ts_code],
            set_={
                **{column: getattr(excluded, column) for column in source_columns},
                "updated_at": func.now(),
            },
            where=changed_fields,
        ).returning(table.c.ts_code)
        changed = len(self.session.execute(statement).all())
        self.session.execute(
            update(table)
            .where(
                table.c.source == source,
                table.c.ts_code.in_([code.ts_code for code in codes]),
            )
            .values(last_seen_at=observed_at)
        )
        return EtfBasicUpsertResult(
            received=len(codes), changed=changed, unchanged=len(codes) - changed
        )

    def reassign_code_entity(
        self,
        *,
        source: str,
        ts_code: str,
        target_etf_id: UUID,
        mapping_source: str,
        evidence: str | None = None,
        actor: str | None = None,
    ) -> bool:
        """Link a code to a verified entity and record the previous association.

        The caller must supply evidence from a provider, exchange announcement, or
        reviewed operating procedure. This method intentionally has no fuzzy-match
        path because names and index codes are insufficient proof of continuity.
        """
        code = self.session.get(EtfCode, {"source": source, "ts_code": ts_code})
        if code is None:
            raise KeyError(f"unknown ETF code: {source}/{ts_code}")
        if code.etf_id == target_etf_id:
            return False
        if self.session.get(EtfEntity, target_etf_id) is None:
            raise KeyError(f"unknown ETF entity: {target_etf_id}")
        old_etf_id = code.etf_id
        code.etf_id = target_etf_id
        self.session.add(
            EtfCodeMappingAudit(
                id=uuid4(),
                source=source,
                ts_code=ts_code,
                old_etf_id=old_etf_id,
                new_etf_id=target_etf_id,
                mapping_source=mapping_source,
                evidence=evidence,
                actor=actor,
            )
        )
        return True

    def earliest_list_date(self, *, source: str) -> date | None:
        """Return the earliest known listed ETF date for a source snapshot.

        Daily-bar full backfills derive their boundary from reference data instead
        of requiring an operator to guess a date before the ETF market existed.
        """
        return self.session.scalar(
            select(func.min(EtfCode.list_date)).where(
                EtfCode.source == source,
                EtfCode.list_date.is_not(None),
            )
        )

    def _existing_entities(self, *, source: str, ts_codes: list[str]) -> dict[str, UUID]:
        rows = self.session.execute(
            select(EtfCode.ts_code, EtfCode.etf_id).where(
                EtfCode.source == source, EtfCode.ts_code.in_(ts_codes)
            )
        ).all()
        return {ts_code: etf_id for ts_code, etf_id in rows}

    @staticmethod
    def _reject_duplicate_codes(codes: list[EtfInstrumentInput]) -> None:
        values = [code.ts_code for code in codes]
        if len(values) != len(set(values)):
            raise ValueError("ETF input contains duplicate ts_code values")
