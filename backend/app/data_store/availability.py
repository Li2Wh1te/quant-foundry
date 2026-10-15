"""Current-store admission independent of the retired publication runtime.

An existing database has the historical Alembic tables even when it is empty.
The application may serve a truly empty installation immediately, but it must
hold an upgraded database in maintenance until the explicit LF-D03 receipt is
ready.  No request or process startup executes a reset.
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

    @property
    def ready(self) -> bool:
        return self.phase == "ready"


def read_availability(session: Session) -> Availability:
    """Read the maintenance receipt, then prove a receipt-free install empty.

    The exact EXISTS checks are intentionally limited to the receipt-free path.
    PostgreSQL statistics cannot prove that a legacy table has zero rows.
    Historical table names come only from the live catalog and are quoted after
    strict identifier validation; unknown tables are included, never ignored.
    """
    phase = session.execute(text(
        "SELECT phase FROM data_store_legacy_maintenance WHERE singleton=1"
    )).scalar_one_or_none()
    if phase is not None:
        return Availability(phase, False, phase != "ready")

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
    if not state.ready:
        raise DataStoreError("DATA_STORE_REBUILDING")
    return state


def check_local_update_ready(session: Session) -> Availability:
    """Check writer admission without promoting the all-business ready receipt.

    An explicit completed reset permits local rebuilding and independent
    refreshes. CurrentStore and the pipeline still validate each input, commit
    and quality restriction. Call require_local_update_ready during real work
    to fence maintenance transitions, rather than releasing this check early.
    """
    state = read_availability(session)
    if state.ready:
        return state
    if state.phase != 'reset_done':
        raise DataStoreError('DATA_STORE_REBUILDING')
    receipt = session.execute(text(
        'SELECT row_to_json(m) FROM data_store_legacy_maintenance m WHERE singleton=1'
    )).scalar_one_or_none()
    if (not isinstance(receipt, dict)
            or receipt.get('completed_json') != ['hooks', 'derived', 'originals', 'functions', 'files']
            or receipt.get('files_started') is not True
            or not isinstance(receipt.get('plan_hash'), str)
            or re.fullmatch(r'[0-9a-f]{64}', receipt['plan_hash']) is None):
        raise DataStoreError('DATA_STORE_REBUILDING')
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
        EXISTS (SELECT 1 FROM scheduled_tasks WHERE task_type=ANY(:types) AND state='active') OR
        EXISTS (SELECT 1 FROM task_runs WHERE task_type=ANY(:types) AND status IN ('queued','running'))
        """), {'types': list(RETIRED_TASK_TYPES)}).scalar_one()
    if unsafe:
        raise DataStoreError('DATA_STORE_REBUILDING')
    return state


def require_local_update_ready(session: Session) -> Availability:
    """Hold admission through the caller's transaction; isolate a failed gate.

    The savepoint leaves an unrelated collector claim transaction usable after
    a maintenance-lock timeout. Successful locks survive until the outer
    transaction ends, including the handler's complete CurrentStore operation.
    """
    try:
        with session.begin_nested():
            previous_timeout = session.scalar(text('SHOW lock_timeout'))
            session.execute(text("SET LOCAL lock_timeout='500ms'"))
            session.execute(text('LOCK TABLE data_store_legacy_maintenance IN SHARE MODE'))
            state = check_local_update_ready(session)
            session.execute(text("SELECT set_config('lock_timeout', :value, true)"),
                            {'value': previous_timeout})
            return state
    except SQLAlchemyError as error:
        sqlstate = getattr(getattr(error, 'orig', None), 'sqlstate', None)
        raise DataStoreError('LOCK_TIMEOUT' if sqlstate == '55P03' else 'CATALOG_UNAVAILABLE') from None


def request_readable(session: Session, entry, state: Availability) -> bool:
    """Use the formal current coverage proof for only this business dependency.

    Active reset/rebuild and unknown phases remain closed. A completed reset
    may contain independently accepted entries before *all* business entries
    can earn the global ready receipt. Object quality is still checked by the
    existing current reader, including opaque and out-of-range restrictions.
    """
    if state.ready:
        return True
    if state.phase != "reset_done":
        return False
    from .coverage import check_coverage
    raw = session.execute(text(
        "SELECT summary_json FROM data_store_entry_status WHERE entry_id=:id"
    ), {"id": entry.id}).scalar_one_or_none()
    if raw is None:
        return False
    import json
    try:
        status = json.loads(raw)
        return bool(check_coverage(session.connection(), entry, status,
                                  require_all_quality=False)["satisfied"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DataStoreError("CATALOG_UNAVAILABLE") from None


def require_request_ready(session: Session, entry) -> Availability:
    """Fence maintenance transitions without changing the global receipt."""
    session.execute(text("SET LOCAL lock_timeout='500ms'"))
    session.execute(text("LOCK TABLE data_store_legacy_maintenance IN SHARE MODE"))
    state = read_availability(session)
    if not request_readable(session, entry, state):
        raise DataStoreError("DATA_STORE_REBUILDING")
    return state
