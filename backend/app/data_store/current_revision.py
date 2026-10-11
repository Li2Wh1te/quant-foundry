"""Bounded fingerprints of current control rows, never historical snapshots.

PostgreSQL row versions detect insert/delete/update even with identical counts
and timestamps. Only identities and xmin are hashed; no business rows, paths,
credentials, exported snapshots or durable revision records are produced.
"""
from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from .errors import DataStoreError

ROW_LIMIT = 100_000
BYTE_LIMIT = 16 * 1024 * 1024
TIME_LIMIT = 5


def begin_inventory_read(session: Session) -> None:
    """All totals, fingerprints and page rows use the same current SQL view."""
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    session.execute(text("SET LOCAL statement_timeout='5000ms'"))


def inventory_revision(session: Session, kind: str, *, entries=(), dataset=None) -> str:
    deadline = time.monotonic() + TIME_LIMIT
    digest = hashlib.sha256()
    rows_seen = bytes_seen = 0

    def add(value):
        nonlocal bytes_seen
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                             sort_keys=True).encode()
        bytes_seen += len(encoded)
        if bytes_seen > BYTE_LIMIT or time.monotonic() > deadline:
            raise DataStoreError("CONTROL_BUDGET_EXCEEDED")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    add(["current-inventory-v1", kind, dataset])
    if kind == "datasets":
        if len(entries) > ROW_LIMIT:
            raise DataStoreError("CONTROL_BUDGET_EXCEEDED")
        for entry in sorted(entries, key=lambda item: item.spec.name):
            # Hash one bounded declared contract at a time. The row budget
            # measures inventory size, not repeated full descriptor payloads.
            contract = json.dumps([entry.spec.descriptor(), entry.describe_capability()],
                                  ensure_ascii=False, sort_keys=True).encode()
            if len(contract) > 256 * 1024:
                raise DataStoreError("CONTROL_BUDGET_EXCEEDED")
            rows_seen += 1
            add([entry.id, entry.spec.name, entry.domain, entry.source, entry.native,
                 entry.disposition, hashlib.sha256(contract).hexdigest()])
        # Only this boolean affects descriptors before global readiness. Do
        # not fingerprint native business inputs or scan the range queue.
        phase = session.execute(text(
            "SELECT phase FROM data_store_legacy_maintenance WHERE singleton=1"
        )).scalar_one_or_none()
        if phase == "reset_done":
            from .coverage import CAPTURED_RANGE_ENTRIES
            captured = session.execute(text(
                "SELECT to_regclass('data_store_source_ranges') IS NOT NULL"
            )).scalar_one()
            add(["captured-ranges-present", captured])
            if captured:
                for entry in sorted(entries, key=lambda item: item.id):
                    if entry.id in CAPTURED_RANGE_ENTRIES:
                        pending = session.execute(text(
                            "SELECT EXISTS(SELECT 1 FROM data_store_source_ranges WHERE source=:s AND dataset=:d)"
                        ), {"s": entry.source, "d": entry.native}).scalar_one()
                        add(["input-pending", entry.id, pending])
        tables = [("data_store_datasets", "name", ""),
                  ("data_store_entry_status", "entry_id", ""),
                  ("data_store_files", "id", ""),
                  ("data_store_legacy_maintenance", "singleton", "")]
        where = ""
    elif kind == "issues":
        tables = []
        where = "WHERE (CAST(:dataset AS text) IS NULL OR dataset=:dataset OR dataset IS NULL)"
    else:
        raise DataStoreError("INVALID_VALUE")
    tables += [("data_store_issues", "dataset,issue_key", where),
               ("data_store_legacy_restrictions", "origin_key", where)]
    for table, identity, predicate in tables:
        add(table)
        remaining = ROW_LIMIT - rows_seen
        result = session.execute(text(
            f"SELECT {identity},xmin::text AS row_version FROM {table} "
            f"{predicate} ORDER BY {identity} LIMIT :row_limit"
        ).execution_options(stream_results=True, max_row_buffer=128),
            {"dataset": dataset, "row_limit": remaining + 1})
        try:
            for row in result:
                rows_seen += 1
                if rows_seen > ROW_LIMIT:
                    raise DataStoreError("CONTROL_BUDGET_EXCEEDED")
                add([str(value) if value is not None else None for value in row])
        finally:
            result.close()
    return digest.hexdigest()


def check_revision(current: str, requested: str | None) -> None:
    if requested is not None and requested != current:
        raise DataStoreError("DATA_CHANGED")
