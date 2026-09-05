"""Add append-only named-calendar registry, facts and reconciliation tables.

Revision ID: 20260829_01
Revises: 20260828_01
Create Date: 2026-08-29

The migration creates the storage contract only.  It intentionally does not
invent SSE/SZSE holiday rows or default sessions: those rows require reviewed
source evidence and are loaded by the calendar ingestion boundary.  Existing
``trading_calendar_days`` data remains the compatibility source until an
explicit, audited backfill is run.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
import hashlib
import json
from uuid import UUID

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.backtesting.calendar_seed import (
    BINDING_FACT_IDS,
    BOOTSTRAP_KNOWN_AT,
    BOOTSTRAP_OBSERVED_AT,
    BOOTSTRAP_EVIDENCE_URI,
    BOOTSTRAP_SIGNATURE_STATUS,
    BOOTSTRAP_SEED_ENTRIES,
    BOOTSTRAP_SEED_HASH,
    BOOTSTRAP_SEED_ID,
    BOOTSTRAP_SEED_VERSION,
    BOOTSTRAP_SOURCE,
    BOOTSTRAP_SOURCE_PRIORITY,
    BOOTSTRAP_SOURCE_PRIORITY_VERSION,
    BOOTSTRAP_SOURCE_REVISION,
    BOOTSTRAP_SOURCE_REVISION_ORDER,
    BOOTSTRAP_VALID_FROM,
    CALENDAR_DEFINITION_WINDOWS,
    DEFINITION_FACT_IDS,
    REGISTRY_FACT_IDS,
    SOURCE_PRIORITY_FACT_ID,
    common_provenance,
    seed_manifest,
)
from app.backtesting.data.errors import (
    CalendarSourcePriorityChainBrokenError,
    CalendarSourcePriorityInvalidError,
    CalendarSourcePriorityMissingError,
)

revision: str = "20260829_01"
down_revision: str | None = "20260828_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine:
    """Use JSONB on PostgreSQL and portable JSON elsewhere."""

    return postgresql.JSONB(astext_type=sa.Text()) if op.get_bind().dialect.name == "postgresql" else sa.JSON()


def _uuid_type() -> sa.types.TypeEngine:
    """Use the SQLAlchemy UUID abstraction on both supported dialects."""

    return sa.Uuid()


def _create_append_only_trigger(table_name: str, function_name: str, trigger_name: str) -> None:
    """Protect immutable fact rows on both PostgreSQL and SQLite."""

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            f"""CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; % is not allowed', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;"""
        )
        op.execute(
            f"""CREATE TRIGGER {trigger_name}
BEFORE UPDATE OR DELETE ON {table_name}
FOR EACH ROW EXECUTE FUNCTION {function_name}();"""
        )
    elif dialect == "sqlite":
        # SQLite has no PL/pgSQL, but a pair of portable triggers provides the
        # same append-only business invariant for local deployments/tests.
        op.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {trigger_name}_update
BEFORE UPDATE ON {table_name}
BEGIN
    SELECT RAISE(ABORT, '{table_name} is append-only');
END;"""
        )
        op.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {trigger_name}_delete
BEFORE DELETE ON {table_name}
BEGIN
    SELECT RAISE(ABORT, '{table_name} is append-only');
END;"""
        )


def _drop_append_only_trigger(table_name: str, function_name: str, trigger_name: str) -> None:
    """Drop one append-only guard during downgrade."""

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
        op.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
    elif dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}_update")
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}_delete")


def _append_only_trigger_exists(table_name: str, trigger_name: str) -> bool:
    """Return whether the migration already installed an append-only guard."""

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return bool(
            bind.execute(
                sa.text(
                    "SELECT 1 FROM pg_trigger trigger_row "
                    "JOIN pg_class table_row ON table_row.oid = trigger_row.tgrelid "
                    "WHERE trigger_row.tgname = :trigger_name "
                    "AND table_row.relname = :table_name LIMIT 1"
                ),
                {"trigger_name": trigger_name, "table_name": table_name},
            ).first()
        )
    if bind.dialect.name == "sqlite":
        return (
            bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'trigger' AND name IN (:update_name, :delete_name)"
                ),
                {"update_name": f"{trigger_name}_update", "delete_name": f"{trigger_name}_delete"},
            ).scalar_one()
            == 2
        )
    return False


def _ensure_append_only_triggers() -> None:
    """Install missing guards when replaying an already-created schema."""

    for table, function, trigger in (
        ("calendar_registry", "guard_calendar_registry_append_only", "trg_calendar_registry_append_only"),
        ("calendar_source_priorities", "guard_calendar_source_priority_append_only", "trg_calendar_source_priority_append_only"),
        ("calendar_definitions", "guard_calendar_definitions_append_only", "trg_calendar_definitions_append_only"),
        ("calendar_session_facts", "guard_calendar_session_facts_append_only", "trg_calendar_session_facts_append_only"),
        ("calendar_exchange_bindings", "guard_calendar_bindings_append_only", "trg_calendar_bindings_append_only"),
        ("calendar_capability_declarations", "guard_calendar_capabilities_append_only", "trg_calendar_capabilities_append_only"),
    ):
        if not _append_only_trigger_exists(table, trigger):
            _create_append_only_trigger(table, function, trigger)


def _seed_content_hash(payload: object) -> str:
    """Hash one fixed seed payload without using runtime state."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_missing_seed_value(value: object) -> bool:
    """Treat NULL/blank bootstrap metadata as a missing trusted seed."""

    return value is None or (isinstance(value, str) and not value.strip())


def _seed_error_details(table_name: str, fact_id: object, field: str | None = None) -> dict[str, str]:
    """Build JSON-safe, stable error details for migration failures."""

    details = {"table": table_name, "fact_id": str(fact_id)}
    if field is not None:
        details["field"] = field
    return details


def _normalise_uuid(value: object) -> UUID | None:
    """Normalise UUID values returned by SQLite/PostgreSQL text rows."""

    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _normalise_datetime(value: object) -> object:
    """Normalise SQLite's timezone-less rendering of UTC seed timestamps."""

    if not isinstance(value, datetime):
        if not isinstance(value, str):
            return value
        try:
            value = datetime.fromisoformat(value.replace(" ", "T"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=BOOTSTRAP_KNOWN_AT.tzinfo)
    return value.astimezone(BOOTSTRAP_KNOWN_AT.tzinfo)


def _normalise_seed_value(field: str, value: object) -> object:
    """Convert backend-specific row representations into comparable values."""

    if field.endswith("fact_id") or field == "fact_id":
        return _normalise_uuid(value)
    if field in {"known_at", "knowledge_from", "knowledge_to", "knowledge_as_of", "observed_at", "created_at"}:
        return _normalise_datetime(value)
    if field in {"valid_from", "valid_to"} and isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    if field in {"evidence", "default_sessions"} and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _validate_seed_manifest() -> None:
    """Fail before writes when the checked-in bootstrap manifest is unusable."""

    manifest = seed_manifest()
    if not isinstance(manifest, Mapping):
        raise CalendarSourcePriorityMissingError(
            "calendar bootstrap seed is missing",
            details={"seed_id": None},
        )
    required_fields = (
        "seed_id",
        "seed_version",
        "bootstrap_seed_hash",
        "entries",
        "evidence_uri",
        "signature_status",
    )
    if any(_is_missing_seed_value(manifest.get(name)) for name in required_fields):
        raise CalendarSourcePriorityMissingError(
            "calendar bootstrap seed is missing",
            details={"seed_id": str(manifest.get("seed_id"))},
        )
    seed_hash = manifest["bootstrap_seed_hash"]
    entries = manifest["entries"]
    if not isinstance(entries, (list, tuple)) or not entries:
        raise CalendarSourcePriorityInvalidError(
            "calendar bootstrap seed entries are invalid"
        )
    # The hash is over the complete, controlled source list.  Comparing the
    # entries themselves also prevents a caller from replacing the list while
    # recomputing a format-valid digest without a reviewed seed release.
    expected_entries = [dict(entry) for entry in BOOTSTRAP_SEED_ENTRIES]
    try:
        entries_hash = _seed_content_hash(entries)
    except (TypeError, ValueError):
        entries_hash = None
    if list(entries) != expected_entries or entries_hash != seed_hash:
        raise CalendarSourcePriorityInvalidError(
            "calendar bootstrap seed hash is invalid"
        )
    if (
        manifest.get("seed_id") != BOOTSTRAP_SEED_ID
        or manifest.get("seed_version") != BOOTSTRAP_SEED_VERSION
        or manifest.get("source") != BOOTSTRAP_SOURCE
        or manifest.get("source_priority_version") != BOOTSTRAP_SOURCE_PRIORITY_VERSION
        or manifest.get("source_priority") != BOOTSTRAP_SOURCE_PRIORITY
        or manifest.get("source_revision_order") != BOOTSTRAP_SOURCE_REVISION_ORDER
        or not isinstance(seed_hash, str)
        or len(seed_hash) != hashlib.sha256().digest_size * 2
        or any(character not in "0123456789abcdef" for character in seed_hash)
        or seed_hash != BOOTSTRAP_SEED_HASH
        or manifest.get("evidence_uri") != BOOTSTRAP_EVIDENCE_URI
        or manifest.get("signature_status") != BOOTSTRAP_SIGNATURE_STATUS
        or manifest.get("trusted_at") != BOOTSTRAP_KNOWN_AT
    ):
        raise CalendarSourcePriorityInvalidError("calendar bootstrap seed manifest is invalid")


def _validate_existing_seed_row(
    table_name: str,
    expected: dict[str, object],
    actual: Mapping[str, object],
) -> None:
    """Reject drift for an existing deterministic row instead of skipping it."""

    fact_id = expected["fact_id"]
    seed_fields = ("bootstrap_seed_id", "bootstrap_seed_version", "bootstrap_seed_hash")
    for field in seed_fields:
        value = actual.get(field)
        if _is_missing_seed_value(value):
            raise CalendarSourcePriorityMissingError(
                "calendar bootstrap seed metadata is missing",
                details=_seed_error_details(table_name, fact_id, field),
            )
        if value != expected[field]:
            raise CalendarSourcePriorityInvalidError(
                "calendar bootstrap seed metadata is invalid",
                details=_seed_error_details(table_name, fact_id, field),
            )

    # A seeded row is immutable.  A changed content hash is the persistence
    # equivalent of a bad signature and must never be silently repaired.
    if actual.get("content_hash") != expected.get("content_hash"):
        raise CalendarSourcePriorityInvalidError(
            "calendar bootstrap fact content hash is invalid",
            details=_seed_error_details(table_name, fact_id, "content_hash"),
        )
    if actual.get("supersedes_fact_id") is not None:
        raise CalendarSourcePriorityChainBrokenError(
            "calendar bootstrap fact revision chain is broken",
            details=_seed_error_details(table_name, fact_id, "supersedes_fact_id"),
        )

    for field, expected_value in expected.items():
        if field in {"bootstrap_seed_id", "bootstrap_seed_version", "bootstrap_seed_hash", "content_hash", "supersedes_fact_id"}:
            continue
        actual_value = actual.get(field)
        if _normalise_seed_value(field, actual_value) != _normalise_seed_value(field, expected_value):
            raise CalendarSourcePriorityInvalidError(
                "calendar bootstrap fact signature is invalid",
                details=_seed_error_details(table_name, fact_id, field),
            )


def _validate_priority_revision_chain(bind) -> None:
    """Validate every persisted priority predecessor before replay continues."""

    rows = bind.execute(sa.text("SELECT * FROM calendar_source_priorities")).mappings().all()
    by_id = {_normalise_uuid(row.get("fact_id")): row for row in rows}
    if None in by_id:
        raise CalendarSourcePriorityInvalidError("calendar source-priority fact_id is invalid")
    for row in rows:
        fact_id = _normalise_uuid(row.get("fact_id"))
        if any(_is_missing_seed_value(row.get(field)) for field in ("bootstrap_seed_id", "bootstrap_seed_version", "bootstrap_seed_hash")):
            raise CalendarSourcePriorityMissingError(
                "calendar source-priority bootstrap seed metadata is missing",
                details=_seed_error_details("calendar_source_priorities", fact_id),
            )
        if (
            row.get("bootstrap_seed_id") != BOOTSTRAP_SEED_ID
            or row.get("bootstrap_seed_version") != BOOTSTRAP_SEED_VERSION
            or row.get("bootstrap_seed_hash") != BOOTSTRAP_SEED_HASH
        ):
            raise CalendarSourcePriorityInvalidError(
                "calendar source-priority bootstrap seed metadata is invalid",
                details=_seed_error_details("calendar_source_priorities", fact_id),
            )
        if row.get("source_priority_fact_id") is not None:
            raise CalendarSourcePriorityInvalidError(
                "calendar source-priority rows must not self-reference a priority fact",
                details=_seed_error_details("calendar_source_priorities", fact_id, "source_priority_fact_id"),
            )
        raw_predecessor_id = row.get("supersedes_fact_id")
        if raw_predecessor_id is None:
            continue
        predecessor_id = _normalise_uuid(raw_predecessor_id)
        if predecessor_id is None:
            raise CalendarSourcePriorityChainBrokenError(
                "calendar source-priority revision chain is broken",
                details=_seed_error_details("calendar_source_priorities", fact_id, "supersedes_fact_id"),
            )
        predecessor = by_id.get(predecessor_id)
        if predecessor is None or predecessor_id == fact_id:
            raise CalendarSourcePriorityChainBrokenError(
                "calendar source-priority revision chain is broken",
                details=_seed_error_details("calendar_source_priorities", fact_id, "supersedes_fact_id"),
            )
        if (
            predecessor.get("logical_fact_key") != row.get("logical_fact_key")
            or predecessor.get("fact_version") != row.get("fact_version", 0) - 1
        ):
            raise CalendarSourcePriorityChainBrokenError(
                "calendar source-priority revision chain is not contiguous",
                details=_seed_error_details("calendar_source_priorities", fact_id, "supersedes_fact_id"),
            )


def _ensure_seed_rows(bind, table_name: str, table: sa.TableClause, rows: list[dict[str, object]]) -> None:
    """Insert missing rows and verify rows with the same fact_id byte-for-byte."""

    existing = {
        _normalise_uuid(row["fact_id"]): row
        for row in bind.execute(sa.text(f"SELECT * FROM {table_name}")).mappings()
    }
    missing: list[dict[str, object]] = []
    for expected in rows:
        fact_id = _normalise_uuid(expected["fact_id"])
        actual = existing.get(fact_id)
        if actual is None:
            missing.append(expected)
        else:
            _validate_existing_seed_row(table_name, expected, actual)
    if missing:
        bind.execute(sa.insert(table), missing)


def _seed_initial_calendar_facts() -> None:
    """Insert deterministic registry/definition/binding bootstrap rows.

    This is intentionally an append-only insert-if-missing operation.  The
    fixed UUIDs, timestamps and content hashes make interrupted migrations
    safe to replay on both SQLite and PostgreSQL.  No daily session facts are
    fabricated: until the ingestion source supplies explicit open/closed
    evidence, strict snapshots correctly remain blocked for missing dates.
    """

    bind = op.get_bind()
    _validate_seed_manifest()
    uuid_type = _uuid_type()
    common_column_types = {
        "fact_id": uuid_type,
        "fact_version": sa.Integer(),
        "logical_fact_key": sa.Text(),
        "supersedes_fact_id": uuid_type,
        "valid_from": sa.Date(),
        "valid_to": sa.Date(),
        "source": sa.String(),
        "source_revision": sa.String(),
        "source_priority_fact_id": uuid_type,
        "source_priority_version": sa.String(),
        "source_priority": sa.Integer(),
        "source_revision_order": sa.Integer(),
        "bootstrap_seed_id": sa.String(),
        "bootstrap_seed_version": sa.Integer(),
        "bootstrap_seed_hash": sa.String(),
        "evidence": _json_type(),
        "known_at": sa.DateTime(timezone=True),
        "knowledge_from": sa.DateTime(timezone=True),
        "knowledge_to": sa.DateTime(timezone=True),
        "knowledge_as_of": sa.DateTime(timezone=True),
        "observed_at": sa.DateTime(timezone=True),
        "quality_status": sa.String(),
        "content_hash": sa.String(),
        "created_at": sa.DateTime(timezone=True),
    }

    def table(name: str, extra: dict[str, sa.types.TypeEngine]) -> sa.TableClause:
        columns = [sa.column(column_name, column_type) for column_name, column_type in common_column_types.items()]
        columns.extend(sa.column(column_name, column_type) for column_name, column_type in extra.items())
        return sa.table(name, *columns)

    provenance = common_provenance(content_hash="")
    # SQLAlchemy's portable UUID type expects UUID objects (not textual
    # aliases) for bound parameters on SQLite as well as PostgreSQL.
    provenance["source_priority_fact_id"] = UUID(SOURCE_PRIORITY_FACT_ID)
    priority_payload = {
        "source": BOOTSTRAP_SOURCE,
        "source_priority_version": BOOTSTRAP_SOURCE_PRIORITY_VERSION,
        "source_priority": BOOTSTRAP_SOURCE_PRIORITY,
        "source_revision_order": BOOTSTRAP_SOURCE_REVISION_ORDER,
        "valid_from": BOOTSTRAP_VALID_FROM.isoformat(),
        "valid_to": None,
    }
    priority_hash = _seed_content_hash(priority_payload)
    priority = {
        **provenance,
        "fact_id": UUID(SOURCE_PRIORITY_FACT_ID),
        "fact_version": 1,
        "logical_fact_key": "calendar_source_priority:operator-registry",
        "supersedes_fact_id": None,
        "valid_from": BOOTSTRAP_VALID_FROM,
        "valid_to": None,
        "content_hash": priority_hash,
        "source_priority_fact_id": None,
        "source_priority_version": BOOTSTRAP_SOURCE_PRIORITY_VERSION,
        "source_priority": BOOTSTRAP_SOURCE_PRIORITY,
        "source_revision_order": BOOTSTRAP_SOURCE_REVISION_ORDER,
    }
    priority_table = table("calendar_source_priorities", {})
    _ensure_seed_rows(bind, "calendar_source_priorities", priority_table, [priority])
    # Validate the complete priority chain before any ordinary fact is written.
    _validate_priority_revision_chain(bind)

    registry_table = table("calendar_registry", {
        "calendar_id": sa.String(),
        "registry_version": sa.Integer(),
        "display_name": sa.String(),
        "timezone_policy": sa.String(),
        "status": sa.String(),
    })
    definition_table = table("calendar_definitions", {
        "calendar_id": sa.String(),
        "registry_fact_id": uuid_type,
        "registry_version": sa.Integer(),
        "definition_version": sa.String(),
        "timezone": sa.String(),
        "default_sessions": _json_type(),
    })
    binding_table = table("calendar_exchange_bindings", {
        "alias": sa.String(),
        "canonical_calendar_id": sa.String(),
        "registry_fact_id": uuid_type,
        "registry_version": sa.Integer(),
        "binding_version": sa.String(),
    })
    registry_rows: list[dict[str, object]] = []
    definition_rows: list[dict[str, object]] = []
    for calendar_id, display_name in (
        ("SSE", "Shanghai Stock Exchange"),
        ("SZSE", "Shenzhen Stock Exchange"),
    ):
        registry_id = UUID(REGISTRY_FACT_IDS[calendar_id])
        registry_semantics = {
            "calendar_id": calendar_id,
            "registry_version": 1,
            "display_name": display_name,
            "timezone_policy": "fixed_asia_shanghai",
            "status": "active",
            "valid_from": BOOTSTRAP_VALID_FROM.isoformat(),
            "valid_to": None,
        }
        registry_rows.append({
            **provenance,
            "fact_id": registry_id,
            "fact_version": 1,
            "logical_fact_key": f"calendar_registry:{calendar_id}",
            "supersedes_fact_id": None,
            "valid_from": BOOTSTRAP_VALID_FROM,
            "valid_to": None,
            "content_hash": _seed_content_hash(registry_semantics),
            "calendar_id": calendar_id,
            "registry_version": 1,
            "display_name": display_name,
            "timezone_policy": "fixed_asia_shanghai",
            "status": "active",
        })
        windows = list(CALENDAR_DEFINITION_WINDOWS[calendar_id])
        definition_semantics = {
            "calendar_id": calendar_id,
            "timezone": "Asia/Shanghai",
            "default_sessions": windows,
            "valid_from": BOOTSTRAP_VALID_FROM.isoformat(),
            "valid_to": None,
        }
        definition_rows.append({
            **provenance,
            "fact_id": UUID(DEFINITION_FACT_IDS[calendar_id]),
            "fact_version": 1,
            "logical_fact_key": f"calendar_definition:{calendar_id}:bootstrap",
            "supersedes_fact_id": None,
            "valid_from": BOOTSTRAP_VALID_FROM,
            "valid_to": None,
            "content_hash": _seed_content_hash(definition_semantics),
            "calendar_id": calendar_id,
            "registry_fact_id": registry_id,
            "registry_version": 1,
            "definition_version": f"{calendar_id.lower()}-bootstrap-v1",
            "timezone": "Asia/Shanghai",
            "default_sessions": windows,
        })
    _ensure_seed_rows(bind, "calendar_registry", registry_table, registry_rows)
    _ensure_seed_rows(bind, "calendar_definitions", definition_table, definition_rows)

    binding_rows: list[dict[str, object]] = []
    for alias, canonical in (
        ("SSE", "SSE"), ("CHINA_SSE", "SSE"), ("XSHG", "SSE"),
        ("SZSE", "SZSE"), ("CHINA_SZSE", "SZSE"), ("XSHE", "SZSE"),
    ):
        semantics = {
            "alias": alias,
            "canonical_calendar_id": canonical,
            "valid_from": BOOTSTRAP_VALID_FROM.isoformat(),
            "valid_to": None,
        }
        binding_rows.append({
            **provenance,
            "fact_id": UUID(BINDING_FACT_IDS[alias]),
            "fact_version": 1,
            "logical_fact_key": f"calendar_binding:{alias}:{canonical}",
            "supersedes_fact_id": None,
            "valid_from": BOOTSTRAP_VALID_FROM,
            "valid_to": None,
            "content_hash": _seed_content_hash(semantics),
            "alias": alias,
            "canonical_calendar_id": canonical,
            "registry_fact_id": UUID(REGISTRY_FACT_IDS[canonical]),
            "registry_version": 1,
            "binding_version": f"binding-{canonical.lower()}-v1",
        })
    _ensure_seed_rows(bind, "calendar_exchange_bindings", binding_table, binding_rows)


def upgrade() -> None:
    """Create named-calendar facts, PIT indexes and reconciliation work items."""

    # Verify the controlled artifact before any schema/data mutation.  This is
    # intentionally repeated by the seed writer as a defense for direct calls.
    _validate_seed_manifest()

    # The existing result endpoint remains canonical; only its persisted hash
    # payload version is extended for calendar evidence.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("backtest_data_preflight") and "hash_schema_version" not in {
        column["name"] for column in inspector.get_columns("backtest_data_preflight")
    }:
        op.add_column(
            "backtest_data_preflight",
            sa.Column("hash_schema_version", sa.SmallInteger(), nullable=False, server_default="1"),
        )
    # Make direct migration smoke/recovery calls idempotent.  Alembic normally
    # guards this through its version table, but an interrupted deployment can
    # legitimately rerun the upgrade body before recording the revision.
    calendar_tables = {
        "calendar_registry",
        "calendar_source_priorities",
        "calendar_definitions",
        "calendar_session_facts",
        "calendar_exchange_bindings",
        "calendar_capability_declarations",
        "calendar_resolution_heads",
        "calendar_reconciliation_ranges",
    }
    existing_calendar_tables = {name for name in calendar_tables if inspector.has_table(name)}
    if existing_calendar_tables:
        if existing_calendar_tables == calendar_tables:
            head_columns = {
                column["name"]
                for column in inspector.get_columns("calendar_resolution_heads")
            }
            if "is_open" not in head_columns:
                raise RuntimeError(
                    "20260829_01 found a pre-release resolution-head schema without is_open"
                )
            # A prior attempt may have committed DDL before seeding or guard
            # installation.  Replay both idempotent steps instead of
            # returning with an incomplete calendar contract.
            _seed_initial_calendar_facts()
            _ensure_append_only_triggers()
            return
        raise RuntimeError(
            "20260829_01 found a partially-created calendar schema: "
            + ", ".join(sorted(existing_calendar_tables))
        )

    uuid = _uuid_type()
    json_type = _json_type()
    def common_fact_columns() -> list[sa.Column]:
        """Return fresh Column objects for each table definition."""

        return [
            sa.Column("fact_id", uuid, nullable=False),
            sa.Column("fact_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("logical_fact_key", sa.Text(), nullable=False),
            sa.Column("supersedes_fact_id", uuid, nullable=True),
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("valid_to", sa.Date(), nullable=True),
            sa.Column("source", sa.String(128), nullable=False),
            sa.Column("source_revision", sa.String(256), nullable=False),
            sa.Column("source_priority_fact_id", uuid, nullable=True),
            sa.Column("source_priority_version", sa.String(128), nullable=True),
            sa.Column("source_priority", sa.Integer(), nullable=True),
            sa.Column("source_revision_order", sa.Integer(), nullable=True),
            sa.Column("bootstrap_seed_id", sa.String(128), nullable=True),
            sa.Column("bootstrap_seed_version", sa.Integer(), nullable=True),
            sa.Column("bootstrap_seed_hash", sa.String(64), nullable=True),
            sa.Column("evidence", json_type, nullable=False),
            sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("knowledge_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("knowledge_to", sa.DateTime(timezone=True), nullable=True),
            sa.Column("knowledge_as_of", sa.DateTime(timezone=True), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("quality_status", sa.String(24), nullable=False, server_default="accepted"),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        ]

    # Source-priority roots must exist before registry and ordinary facts so
    # PostgreSQL can create their real composite foreign keys in one upgrade.
    # Priority rows are the one bootstrap exception and never self-reference.
    op.create_table(
        "calendar_source_priorities",
        *common_fact_columns(),
        sa.PrimaryKeyConstraint("fact_id"),
        sa.UniqueConstraint("source", "source_priority_version", name="uq_calendar_source_priority_version"),
        sa.UniqueConstraint("source", "source_priority_version", "fact_id", name="uq_calendar_source_priority_composite_ref"),
        sa.UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_source_priority_logical_version"),
        sa.CheckConstraint("fact_version > 0", name="calendar_source_priority_fact_version_positive"),
        sa.CheckConstraint(
            "source_priority_version IS NOT NULL AND source_priority IS NOT NULL "
            "AND source_priority >= 0 AND source_revision_order IS NOT NULL "
            "AND source_revision_order >= 0",
            name="calendar_source_priority_values_required",
        ),
        sa.CheckConstraint(
            "source_priority_fact_id IS NULL AND bootstrap_seed_id IS NOT NULL "
            "AND bootstrap_seed_version IS NOT NULL AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_source_priority_bootstrap_root",
        ),
        sa.ForeignKeyConstraint(["supersedes_fact_id"], ["calendar_source_priorities.fact_id"], name="fk_calendar_source_priority_supersedes"),
    )

    op.create_table(
        "calendar_registry",
        *common_fact_columns(),
        sa.Column("calendar_id", sa.String(32), nullable=False),
        sa.Column("registry_version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("timezone_policy", sa.String(64), nullable=False, server_default="fixed_asia_shanghai"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.PrimaryKeyConstraint("fact_id"),
        sa.UniqueConstraint("calendar_id", "registry_version", name="uq_calendar_registry_version"),
        sa.UniqueConstraint("calendar_id", "registry_version", "fact_id", name="uq_calendar_registry_composite_ref"),
        sa.UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_registry_logical_version"),
        sa.CheckConstraint("fact_version > 0", name="calendar_registry_fact_version_positive"),
        sa.CheckConstraint("registry_version > 0", name="calendar_registry_version_positive"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="calendar_registry_valid_range"),
        sa.CheckConstraint("knowledge_to IS NULL OR knowledge_to > knowledge_from", name="calendar_registry_knowledge_range"),
        sa.CheckConstraint("status IN ('active','deprecated')", name="calendar_registry_status"),
        sa.CheckConstraint("timezone_policy = 'fixed_asia_shanghai'", name="calendar_registry_timezone_policy"),
        sa.CheckConstraint(
            "source_priority_fact_id IS NOT NULL AND source_priority_version IS NOT NULL "
            "AND source_priority IS NOT NULL AND source_revision_order IS NOT NULL "
            "AND bootstrap_seed_id IS NOT NULL AND bootstrap_seed_version IS NOT NULL "
            "AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_registry_source_priority_required",
        ),
        sa.ForeignKeyConstraint(["source", "source_priority_version", "source_priority_fact_id"], ["calendar_source_priorities.source", "calendar_source_priorities.source_priority_version", "calendar_source_priorities.fact_id"], name="fk_calendar_registry_source_priority"),
        sa.ForeignKeyConstraint(["supersedes_fact_id"], ["calendar_registry.fact_id"], name="fk_calendar_registry_supersedes"),
    )
    op.create_index("ix_calendar_registry_lookup", "calendar_registry", ["calendar_id", "valid_from", "known_at"])

    op.create_table(
        "calendar_definitions",
        *common_fact_columns(),
        sa.Column("calendar_id", sa.String(32), nullable=False),
        sa.Column("registry_fact_id", uuid, nullable=False),
        sa.Column("registry_version", sa.Integer(), nullable=False),
        sa.Column("definition_version", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("default_sessions", json_type, nullable=False),
        sa.PrimaryKeyConstraint("fact_id"),
        sa.UniqueConstraint("calendar_id", "definition_version", name="uq_calendar_definition_semantic_version"),
        sa.UniqueConstraint("calendar_id", "definition_version", "fact_id", name="uq_calendar_definition_composite_ref"),
        sa.UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_definition_logical_version"),
        sa.ForeignKeyConstraint(["calendar_id", "registry_version", "registry_fact_id"], ["calendar_registry.calendar_id", "calendar_registry.registry_version", "calendar_registry.fact_id"], name="fk_calendar_definition_registry"),
        sa.CheckConstraint("fact_version > 0", name="calendar_definition_fact_version_positive"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="calendar_definition_valid_range"),
        sa.CheckConstraint("knowledge_to IS NULL OR knowledge_to > knowledge_from", name="calendar_definition_knowledge_range"),
        sa.CheckConstraint("registry_version > 0", name="calendar_definition_registry_version_positive"),
        sa.CheckConstraint(
            "source_priority_fact_id IS NOT NULL AND source_priority_version IS NOT NULL "
            "AND source_priority IS NOT NULL AND source_revision_order IS NOT NULL "
            "AND bootstrap_seed_id IS NOT NULL AND bootstrap_seed_version IS NOT NULL "
            "AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_definition_source_priority_required",
        ),
        sa.ForeignKeyConstraint(["source", "source_priority_version", "source_priority_fact_id"], ["calendar_source_priorities.source", "calendar_source_priorities.source_priority_version", "calendar_source_priorities.fact_id"], name="fk_calendar_definition_source_priority"),
        sa.ForeignKeyConstraint(["supersedes_fact_id"], ["calendar_definitions.fact_id"], name="fk_calendar_definition_supersedes"),
    )
    op.create_index("ix_calendar_definition_lookup", "calendar_definitions", ["calendar_id", "valid_from", "known_at"])

    op.create_table(
        "calendar_session_facts",
        *common_fact_columns(),
        sa.Column("calendar_id", sa.String(32), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("registry_fact_id", uuid, nullable=False),
        sa.Column("registry_version", sa.Integer(), nullable=False),
        sa.Column("definition_version", sa.String(128), nullable=False),
        sa.Column("definition_fact_id", uuid, nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column("timezone_override", sa.String(64), nullable=True),
        sa.Column("sessions_override", json_type, nullable=True),
        sa.Column("override_mode", sa.String(16), nullable=False, server_default="inherit"),
        sa.PrimaryKeyConstraint("fact_id"),
        sa.UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_session_logical_version"),
        sa.ForeignKeyConstraint(["calendar_id", "registry_version", "registry_fact_id"], ["calendar_registry.calendar_id", "calendar_registry.registry_version", "calendar_registry.fact_id"], name="fk_calendar_session_registry"),
        sa.ForeignKeyConstraint(["calendar_id", "definition_version", "definition_fact_id"], ["calendar_definitions.calendar_id", "calendar_definitions.definition_version", "calendar_definitions.fact_id"], name="fk_calendar_session_definition"),
        sa.CheckConstraint("fact_version > 0", name="calendar_session_fact_version_positive"),
        sa.CheckConstraint("valid_to > valid_from", name="calendar_session_valid_range"),
        sa.CheckConstraint("knowledge_to IS NULL OR knowledge_to > knowledge_from", name="calendar_session_knowledge_range"),
        sa.CheckConstraint("registry_version > 0", name="calendar_session_registry_version_positive"),
        sa.CheckConstraint(
            "source_priority_fact_id IS NOT NULL AND source_priority_version IS NOT NULL "
            "AND source_priority IS NOT NULL AND source_revision_order IS NOT NULL "
            "AND bootstrap_seed_id IS NOT NULL AND bootstrap_seed_version IS NOT NULL "
            "AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_session_source_priority_required",
        ),
        sa.CheckConstraint("override_mode IN ('inherit','explicit')", name="calendar_session_override_mode"),
        sa.ForeignKeyConstraint(["source", "source_priority_version", "source_priority_fact_id"], ["calendar_source_priorities.source", "calendar_source_priorities.source_priority_version", "calendar_source_priorities.fact_id"], name="fk_calendar_session_source_priority"),
        sa.CheckConstraint("(NOT is_open AND override_mode = 'explicit') OR is_open", name="calendar_session_closed_explicit"),
        sa.ForeignKeyConstraint(["supersedes_fact_id"], ["calendar_session_facts.fact_id"], name="fk_calendar_session_supersedes"),
    )
    op.create_index("ix_calendar_session_lookup", "calendar_session_facts", ["calendar_id", "session_date", "known_at"])
    op.create_index("ix_calendar_session_quality", "calendar_session_facts", ["calendar_id", "quality_status", "session_date"])

    op.create_table(
        "calendar_exchange_bindings",
        *common_fact_columns(),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column("canonical_calendar_id", sa.String(32), nullable=False),
        sa.Column("registry_fact_id", uuid, nullable=False),
        sa.Column("registry_version", sa.Integer(), nullable=False),
        sa.Column("binding_version", sa.String(128), nullable=False),
        sa.PrimaryKeyConstraint("fact_id"),
        sa.UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_binding_logical_version"),
        sa.ForeignKeyConstraint(["canonical_calendar_id", "registry_version", "registry_fact_id"], ["calendar_registry.calendar_id", "calendar_registry.registry_version", "calendar_registry.fact_id"], name="fk_calendar_binding_registry"),
        sa.CheckConstraint("fact_version > 0", name="calendar_binding_fact_version_positive"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="calendar_binding_valid_range"),
        sa.CheckConstraint("knowledge_to IS NULL OR knowledge_to > knowledge_from", name="calendar_binding_knowledge_range"),
        sa.CheckConstraint("registry_version > 0", name="calendar_binding_registry_version_positive"),
        sa.CheckConstraint(
            "source_priority_fact_id IS NOT NULL AND source_priority_version IS NOT NULL "
            "AND source_priority IS NOT NULL AND source_revision_order IS NOT NULL "
            "AND bootstrap_seed_id IS NOT NULL AND bootstrap_seed_version IS NOT NULL "
            "AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_binding_source_priority_required",
        ),
        sa.ForeignKeyConstraint(["source", "source_priority_version", "source_priority_fact_id"], ["calendar_source_priorities.source", "calendar_source_priorities.source_priority_version", "calendar_source_priorities.fact_id"], name="fk_calendar_binding_source_priority"),
        sa.ForeignKeyConstraint(["supersedes_fact_id"], ["calendar_exchange_bindings.fact_id"], name="fk_calendar_binding_supersedes"),
    )
    op.create_index("ix_calendar_binding_lookup", "calendar_exchange_bindings", ["alias", "valid_from", "known_at"])

    op.create_table(
        "calendar_capability_declarations",
        *common_fact_columns(),
        sa.Column("scope_kind", sa.String(24), nullable=False),
        sa.Column("scope_key", sa.String(256), nullable=False),
        sa.Column("provider_key", sa.String(128), nullable=True),
        sa.Column("package_key", sa.String(128), nullable=True),
        sa.Column("package_version", sa.String(64), nullable=True),
        sa.Column("calendar_id", sa.String(32), nullable=True),
        sa.Column("registry_fact_id", uuid, nullable=True),
        sa.Column("registry_version", sa.Integer(), nullable=True),
        sa.Column("instrument_id", uuid, nullable=True),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("value", sa.String(24), nullable=False, server_default="unknown"),
        sa.Column("applicability", sa.String(24), nullable=True),
        sa.PrimaryKeyConstraint("fact_id"),
        sa.UniqueConstraint("logical_fact_key", "fact_version", name="uq_calendar_capability_logical_version"),
        sa.CheckConstraint("fact_version > 0", name="calendar_capability_fact_version_positive"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="calendar_capability_valid_range"),
        sa.CheckConstraint("knowledge_to IS NULL OR knowledge_to > knowledge_from", name="calendar_capability_knowledge_range"),
        sa.CheckConstraint("scope_kind IN ('provider','rule_package','calendar','instrument')", name="calendar_capability_scope_kind"),
        sa.CheckConstraint("value IN ('supported','unsupported','unknown')", name="calendar_capability_value"),
        sa.CheckConstraint("applicability IS NULL OR applicability IN ('required','not_applicable')", name="calendar_capability_applicability"),
        sa.CheckConstraint("scope_kind <> 'calendar' OR (registry_fact_id IS NOT NULL AND registry_version IS NOT NULL)", name="calendar_capability_registry_reference"),
        sa.CheckConstraint("scope_kind <> 'calendar' OR calendar_id IS NOT NULL", name="calendar_capability_calendar_id_required"),
        sa.CheckConstraint("scope_kind = 'calendar' OR (calendar_id IS NULL AND registry_fact_id IS NULL AND registry_version IS NULL)", name="calendar_capability_non_calendar_columns"),
        sa.CheckConstraint(
            "source_priority_fact_id IS NOT NULL AND source_priority_version IS NOT NULL "
            "AND source_priority IS NOT NULL AND source_revision_order IS NOT NULL "
            "AND bootstrap_seed_id IS NOT NULL AND bootstrap_seed_version IS NOT NULL "
            "AND bootstrap_seed_hash IS NOT NULL",
            name="calendar_capability_source_priority_required",
        ),
        sa.ForeignKeyConstraint(["source", "source_priority_version", "source_priority_fact_id"], ["calendar_source_priorities.source", "calendar_source_priorities.source_priority_version", "calendar_source_priorities.fact_id"], name="fk_calendar_capability_source_priority"),
        sa.ForeignKeyConstraint(["calendar_id", "registry_version", "registry_fact_id"], ["calendar_registry.calendar_id", "calendar_registry.registry_version", "calendar_registry.fact_id"], name="fk_calendar_capability_registry"),
        sa.ForeignKeyConstraint(["supersedes_fact_id"], ["calendar_capability_declarations.fact_id"], name="fk_calendar_capability_supersedes"),
    )
    op.create_index("ix_calendar_capability_scope", "calendar_capability_declarations", ["scope_kind", "scope_key", "capability", "valid_from", "known_at"])

    op.create_table(
        "calendar_resolution_heads",
        sa.Column("id", uuid, nullable=False),
        sa.Column("logical_fact_key", sa.Text(), nullable=False),
        sa.Column("calendar_id", sa.String(32), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("selected_fact_id", uuid, nullable=True),
        sa.Column("selected_fact_version", sa.Integer(), nullable=True),
        sa.Column("knowledge_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("knowledge_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision_digest", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "logical_fact_key",
            "effective_date",
            "knowledge_from",
            name="uq_calendar_resolution_head_slot",
        ),
        sa.ForeignKeyConstraint(
            ["selected_fact_id"],
            ["calendar_session_facts.fact_id"],
            name="fk_calendar_resolution_head_selected_fact",
        ),
        sa.CheckConstraint(
            "(selected_fact_id IS NULL AND selected_fact_version IS NULL) OR "
            "(selected_fact_id IS NOT NULL AND selected_fact_version IS NOT NULL AND selected_fact_version > 0)",
            name="calendar_resolution_head_selected_fact_pair",
        ),
    )
    op.create_index("ix_calendar_resolution_head_calendar_date", "calendar_resolution_heads", ["calendar_id", "effective_date"])

    op.create_table(
        "calendar_reconciliation_ranges",
        sa.Column("id", uuid, nullable=False),
        sa.Column("calendar_id", sa.String(32), nullable=False),
        sa.Column("range_start", sa.Date(), nullable=False),
        sa.Column("range_end", sa.Date(), nullable=False),
        sa.Column("source_revision", sa.String(256), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rescan_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("range_end > range_start", name="calendar_reconciliation_range_ordered"),
        sa.CheckConstraint("status IN ('pending','running','completed','blocked')", name="calendar_reconciliation_status"),
    )
    op.create_index("ix_calendar_reconciliation_pending", "calendar_reconciliation_ranges", ["calendar_id", "status", "range_start"])

    # Install the reviewed, deterministic bootstrap evidence before enabling
    # append-only triggers.  Daily session facts remain absent until the
    # ingestion boundary supplies explicit source evidence.
    _seed_initial_calendar_facts()

    _ensure_append_only_triggers()


def downgrade() -> None:
    """Remove only empty calendar tables; never erase persisted evidence."""

    bind = op.get_bind()
    for table in (
        "calendar_reconciliation_ranges",
        "calendar_resolution_heads",
        "calendar_capability_declarations",
        "calendar_exchange_bindings",
        "calendar_session_facts",
        "calendar_definitions",
        "calendar_source_priorities",
        "calendar_registry",
    ):
        if bind.dialect.has_table(bind, table):
            count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            if count:
                raise RuntimeError(f"refusing downgrade 20260829_01: {table} contains persisted evidence")

    for table, function, trigger in (
        ("calendar_capability_declarations", "guard_calendar_capabilities_append_only", "trg_calendar_capabilities_append_only"),
        ("calendar_exchange_bindings", "guard_calendar_bindings_append_only", "trg_calendar_bindings_append_only"),
        ("calendar_session_facts", "guard_calendar_session_facts_append_only", "trg_calendar_session_facts_append_only"),
        ("calendar_definitions", "guard_calendar_definitions_append_only", "trg_calendar_definitions_append_only"),
        ("calendar_source_priorities", "guard_calendar_source_priority_append_only", "trg_calendar_source_priority_append_only"),
        ("calendar_registry", "guard_calendar_registry_append_only", "trg_calendar_registry_append_only"),
    ):
        _drop_append_only_trigger(table, function, trigger)
    for table in (
        "calendar_reconciliation_ranges",
        "calendar_resolution_heads",
        "calendar_capability_declarations",
        "calendar_exchange_bindings",
        "calendar_session_facts",
        "calendar_definitions",
        "calendar_source_priorities",
        "calendar_registry",
    ):
        op.drop_table(table)

    inspector = sa.inspect(bind)
    if inspector.has_table("backtest_data_preflight") and "hash_schema_version" in {
        column["name"] for column in inspector.get_columns("backtest_data_preflight")
    }:
        op.drop_column("backtest_data_preflight", "hash_schema_version")
