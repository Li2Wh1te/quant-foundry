"""Deterministic bootstrap seed metadata for named trading calendars.

The seed is deliberately plain data so Alembic migrations and deployment
checks can consume it without importing ORM models or consulting a wall
clock.  It establishes only the canonical registry, reviewed Asia/Shanghai
definition templates, explicit alias bindings, and the source-priority root;
it does not fabricate daily open/closed facts.  Daily facts are appended by
the ingestion boundary once source evidence is available.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json

BOOTSTRAP_SEED_ID = "calendar-source-priority-bootstrap"
BOOTSTRAP_SEED_VERSION = 1
BOOTSTRAP_SOURCE = "operator-registry"
BOOTSTRAP_SOURCE_PRIORITY_VERSION = "operator-registry-v1"
BOOTSTRAP_SOURCE_REVISION = "seed-2026-01"
BOOTSTRAP_SOURCE_REVISION_ORDER = 1
BOOTSTRAP_SOURCE_PRIORITY = 10
# The URI is deliberately a release-scoped identifier rather than a mutable
# local path.  Deployments may only trust this reviewed artifact location.
BOOTSTRAP_EVIDENCE_URI = "release://calendar-source-priority-bootstrap/1"
BOOTSTRAP_SIGNATURE_STATUS = "verified"
# A fixed, reviewed instant is part of the seed evidence.  It is not
# replaced with datetime.now() during migration replay.
BOOTSTRAP_KNOWN_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
BOOTSTRAP_OBSERVED_AT = BOOTSTRAP_KNOWN_AT
BOOTSTRAP_VALID_FROM = date(1900, 1, 1)

# Stable UUIDs make rerunning an interrupted migration idempotent and keep
# every composite registry reference reproducible across SQLite/PostgreSQL.
SOURCE_PRIORITY_FACT_ID = "10000000-0000-0000-0000-000000000001"
REGISTRY_FACT_IDS = {
    "SSE": "20000000-0000-0000-0000-000000000001",
    "SZSE": "20000000-0000-0000-0000-000000000002",
}
DEFINITION_FACT_IDS = {
    "SSE": "30000000-0000-0000-0000-000000000001",
    "SZSE": "30000000-0000-0000-0000-000000000002",
}
BINDING_FACT_IDS = {
    "SSE": "40000000-0000-0000-0000-000000000001",
    "CHINA_SSE": "40000000-0000-0000-0000-000000000002",
    "XSHG": "40000000-0000-0000-0000-000000000003",
    "SZSE": "40000000-0000-0000-0000-000000000004",
    "CHINA_SZSE": "40000000-0000-0000-0000-000000000005",
    "XSHE": "40000000-0000-0000-0000-000000000006",
}

# This is the complete source -> priority/revision-order control list.  Keep
# it as plain data so both migration-time validation and hash calculation use
# exactly the same canonical payload.
BOOTSTRAP_SEED_ENTRIES = (
    {
        "source": BOOTSTRAP_SOURCE,
        "source_priority": BOOTSTRAP_SOURCE_PRIORITY,
        "source_revision_order": BOOTSTRAP_SOURCE_REVISION_ORDER,
        "revision_order_policy": "approved_integer",
    },
)


def _seed_entries_hash(entries: object) -> str:
    """Hash the complete source-priority list in one canonical form."""

    encoded = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# A derived constant prevents a format-valid but unrelated digest from being
# accepted as the bootstrap root.
BOOTSTRAP_SEED_HASH = _seed_entries_hash(BOOTSTRAP_SEED_ENTRIES)

CALENDAR_DEFINITION_WINDOWS = {
    "SSE": (
        # Keep seed text in the same canonical representation emitted by
        # SessionWindow.semantic_payload() (seconds are omitted when zero).
        {"start": "09:30", "end": "11:30", "day_offset": 0, "end_day_offset": 0},
        {"start": "13:00", "end": "15:00", "day_offset": 0, "end_day_offset": 0},
    ),
    "SZSE": (
        {"start": "09:30", "end": "11:30", "day_offset": 0, "end_day_offset": 0},
        {"start": "13:00", "end": "15:00", "day_offset": 0, "end_day_offset": 0},
    ),
}


def common_provenance(*, content_hash: str) -> dict[str, object]:
    """Return immutable provenance fields shared by seeded ordinary facts."""

    return {
        "source": BOOTSTRAP_SOURCE,
        "source_revision": BOOTSTRAP_SOURCE_REVISION,
        "source_priority_fact_id": SOURCE_PRIORITY_FACT_ID,
        "source_priority_version": BOOTSTRAP_SOURCE_PRIORITY_VERSION,
        "source_priority": BOOTSTRAP_SOURCE_PRIORITY,
        "source_revision_order": BOOTSTRAP_SOURCE_REVISION_ORDER,
        "bootstrap_seed_id": BOOTSTRAP_SEED_ID,
        "bootstrap_seed_version": BOOTSTRAP_SEED_VERSION,
        "bootstrap_seed_hash": BOOTSTRAP_SEED_HASH,
        "evidence": {
            "uri": BOOTSTRAP_EVIDENCE_URI,
            "seed_id": BOOTSTRAP_SEED_ID,
            "seed_version": BOOTSTRAP_SEED_VERSION,
            "signature_status": BOOTSTRAP_SIGNATURE_STATUS,
        },
        "known_at": BOOTSTRAP_KNOWN_AT,
        "knowledge_from": BOOTSTRAP_KNOWN_AT,
        "knowledge_to": None,
        "knowledge_as_of": BOOTSTRAP_KNOWN_AT,
        "observed_at": BOOTSTRAP_OBSERVED_AT,
        "quality_status": "accepted",
        "content_hash": content_hash,
        "created_at": BOOTSTRAP_KNOWN_AT,
    }


def seed_manifest() -> dict[str, object]:
    """Expose the exact immutable seed manifest for startup verification."""

    return {
        "seed_id": BOOTSTRAP_SEED_ID,
        "seed_version": BOOTSTRAP_SEED_VERSION,
        "bootstrap_seed_hash": BOOTSTRAP_SEED_HASH,
        "entries": [dict(entry) for entry in BOOTSTRAP_SEED_ENTRIES],
        "source": BOOTSTRAP_SOURCE,
        "source_priority_version": BOOTSTRAP_SOURCE_PRIORITY_VERSION,
        "source_priority": BOOTSTRAP_SOURCE_PRIORITY,
        "source_revision_order": BOOTSTRAP_SOURCE_REVISION_ORDER,
        "evidence_uri": BOOTSTRAP_EVIDENCE_URI,
        "signature_status": BOOTSTRAP_SIGNATURE_STATUS,
        "trusted_at": BOOTSTRAP_KNOWN_AT,
    }


__all__ = [
    "BINDING_FACT_IDS",
    "BOOTSTRAP_KNOWN_AT",
    "BOOTSTRAP_OBSERVED_AT",
    "BOOTSTRAP_EVIDENCE_URI",
    "BOOTSTRAP_SIGNATURE_STATUS",
    "BOOTSTRAP_SEED_ENTRIES",
    "BOOTSTRAP_SEED_HASH",
    "BOOTSTRAP_SEED_ID",
    "BOOTSTRAP_SEED_VERSION",
    "BOOTSTRAP_SOURCE",
    "BOOTSTRAP_SOURCE_PRIORITY",
    "BOOTSTRAP_SOURCE_PRIORITY_VERSION",
    "BOOTSTRAP_SOURCE_REVISION",
    "BOOTSTRAP_SOURCE_REVISION_ORDER",
    "BOOTSTRAP_VALID_FROM",
    "CALENDAR_DEFINITION_WINDOWS",
    "DEFINITION_FACT_IDS",
    "REGISTRY_FACT_IDS",
    "SOURCE_PRIORITY_FACT_ID",
    "common_provenance",
    "seed_manifest",
]
