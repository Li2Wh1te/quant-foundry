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
