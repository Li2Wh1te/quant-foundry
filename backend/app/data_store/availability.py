"""Current-store admission independent of the retired publication runtime.

An existing database has the historical Alembic tables even when it is empty.
The application may serve a truly empty installation immediately, but it must
hold an upgraded database while destructive maintenance is unfinished. A valid
reset receipt permits independent current reads and updates; final readiness
still belongs to the unchanged all-domain LF-D03 finish guard. No request or
process startup executes a reset.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .errors import DataStoreError


RETIRED_TASK_TYPES = (
    "foundation.formalize_local_updates",
    "foundation.formalize_local_table_updates",
)


@dataclass(frozen=True)
class Availability:
    phase: str
    fresh_install: bool
    legacy_rows_present: bool
    system_safe: bool = True

    @property
    def ready(self) -> bool:
        return self.phase == "ready"

    @property
    def operable(self) -> bool:
        return self.system_safe and self.phase in ("ready", "reset_done")


def read_availability(session: Session) -> Availability:
    """Read the maintenance receipt, then prove a receipt-free install empty.

    The exact EXISTS checks are intentionally limited to the receipt-free path.
    PostgreSQL statistics cannot prove that a legacy table has zero rows.
    Historical table names come only from the live catalog and are quoted after
    strict identifier validation; unknown tables are included, never ignored.
    """
    receipt = session.execute(text(
        "SELECT row_to_json(m) FROM data_store_legacy_maintenance m WHERE singleton=1"
    )).scalar_one_or_none()
    if receipt is not None:
        phase = receipt['phase']
        safe = phase == 'ready' or (
            phase == 'reset_done' and
            receipt.get('completed_json') == ['hooks', 'derived', 'originals', 'functions', 'files'] and
            receipt.get('files_started') is True and
            isinstance(receipt.get('plan_hash'), str) and
            re.fullmatch(r'[0-9a-f]{64}', receipt['plan_hash']) is not None
        )
        if safe:
            # A phase label cannot hide reintroduced retired writers/objects.
            # Root identity and each selected file are separately checked by
            # CurrentStore, so unrelated domain quality is absent here.
            unsafe = session.execute(text("""SELECT
                EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=current_schema() AND left(c.relname,11)='foundation_'
                    AND c.relkind IN ('r','p','v','m','f')) OR
                EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname=current_schema() AND left(p.proname,11)='foundation_') OR
                EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
                    WHERE n.nspname=current_schema() AND NOT t.tgisinternal
                    AND (left(t.tgname,11)='foundation_' OR left(p.proname,11)='foundation_')) OR
                EXISTS (SELECT 1 FROM scheduled_tasks WHERE task_type=ANY(:types))
                """), {'types': list(RETIRED_TASK_TYPES)}).scalar_one()
            safe = not unsafe
        return Availability(phase, False, not safe, safe)

    old_task = session.execute(text(
        "SELECT EXISTS (SELECT 1 FROM scheduled_tasks "
        "WHERE task_type = ANY(:types))"
    ), {"types": list(RETIRED_TASK_TYPES)}).scalar_one()
    if old_task:
        return Availability("rebuilding", False, True)

    rows = session.execute(text(
        "SELECT n.nspname, c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND left(c.relname,11)='foundation_' "
        "AND c.relkind IN ('r','p') ORDER BY c.relname"
    )).all()
    for schema, table in rows:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema) or not re.fullmatch(
            r"[a-z_][a-z0-9_]*", table
        ):
            raise DataStoreError("CATALOG_UNAVAILABLE")
        has_rows = session.execute(text(
            f'SELECT EXISTS (SELECT 1 FROM "{schema}"."{table}")'
        )).scalar_one()
        if has_rows:
            return Availability("rebuilding", False, True)
    return Availability("ready", True, False)


def require_ready(session: Session) -> Availability:
    state = read_availability(session)
    if not state.ready or not state.operable:
        raise DataStoreError("DATA_STORE_REBUILDING")
    return state


def require_operable(session: Session) -> Availability:
    """Fence destructive maintenance for the caller's entire transaction.

    A SHARE table lock also fences insertion of the first maintenance receipt
    on a fresh install. Callers retain this session through actual reads or
    writes, rather than releasing admission before opening the store. Ordinary
    collectors and current commits never write this small maintenance table.
    """
    try:
        session.execute(text("SELECT set_config('lock_timeout','5s',true)"))
        session.execute(text('LOCK TABLE data_store_legacy_maintenance IN SHARE MODE'))
        state = read_availability(session)
    except SQLAlchemyError as error:
        sqlstate = getattr(getattr(error, 'orig', None), 'sqlstate', None)
        raise DataStoreError('LOCK_TIMEOUT' if sqlstate == '55P03' else 'CATALOG_UNAVAILABLE') from None
    if not state.operable:
        raise DataStoreError('DATA_STORE_REBUILDING')
    return state
