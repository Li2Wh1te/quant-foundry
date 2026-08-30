"""Lifecycle service for private strategy drafts and immutable revisions."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any
from types import MappingProxyType
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.strategies.models import Strategy, StrategyDraft, StrategyRevision
from app.strategies.repository import StrategyRepository
from app.strategies.validation import (
    StrategyValidationIssue,
    StrategyValidationResult,
    validate_strategy_draft,
)
from app.strategy_protocol.contract import STRATEGY_CONTRACT_VERSION


MAX_STRATEGY_SOURCE_BYTES = 1_048_576
"""Maximum UTF-8 source size accepted for a single first-phase strategy module."""

DEFAULT_RUNTIME_MANIFEST = {"strategy_contract_version": STRATEGY_CONTRACT_VERSION}
"""Protocol metadata snapshotted when a caller supplies no manifest.

``strategy_contract_version`` identifies the current and only official page
protocol; it is not a migration marker for historical protocol revisions.
"""


_UNSET = object()
"""Sentinel that distinguishes an omitted metadata field from an explicit null."""


class StrategyStorageError(Exception):
    """Base error for expected private strategy storage lifecycle failures."""


class StrategyNotFoundError(StrategyStorageError):
    """Raised when a requested private strategy does not exist."""


class StrategyDraftNotFoundError(StrategyStorageError):
    """Raised when a strategy has no mutable draft to save or publish."""


class StrategyArchivedError(StrategyStorageError):
    """Raised when a lifecycle write targets an archived strategy."""


class StrategyDraftConflictError(StrategyStorageError):
    """Raised when an editor saves or publishes a stale draft version."""


class StrategyStorageValidationError(StrategyStorageError, ValueError):
    """Raised before persistence when strategy source or JSON data is invalid."""


class StrategyDraftIntegrityError(StrategyStorageError):
    """Raised when stored draft text does not match its recorded SHA-256 digest."""


class StrategyMetadataConflictError(StrategyStorageError):
    """Raised when a metadata edit or archive uses an obsolete strategy version."""


class StrategyAlreadyArchivedError(StrategyStorageError):
    """Raised when an archival request targets a strategy already archived."""


class StrategyDraftValidationError(StrategyStorageError):
    """Raised when a draft fails the non-executing strategy contract checks."""

    def __init__(self, issues: tuple[StrategyValidationIssue, ...]) -> None:
        super().__init__("strategy draft did not pass validation")
        self.issues = issues


class StrategyRevisionBindingError(StrategyStorageError, ValueError):
    """Raised when a run cannot bind an exact published revision."""


@dataclass(frozen=True, slots=True)
class PublishedStrategyBinding:
    """Immutable execution snapshot detached from ORM and mutable drafts."""

    strategy_id: UUID
    revision_id: UUID
    revision_number: int
    source_code: str
    source_hash: str
    parameter_schema: Mapping[str, Any]
    parameters: Mapping[str, Any]
    runtime_manifest: Mapping[str, Any]
    strategy_contract_version: int
    # Optional run-scope facts are kept as serializable frozen values.  They
    # are populated by the run coordinator when available and deliberately do
    # not carry ORM/provider objects across the worker boundary.
    static_instrument_ids: tuple[UUID, ...] = ()
    initial_positions: tuple[Mapping[str, Any], ...] = ()
    adjustment_policy_reference: Mapping[str, Any] | None = None

    @property
    def strategy_revision_id(self) -> UUID:
        """Canonical name used by run-start callers."""
        return self.revision_id

    @property
    def frozen_parameters(self) -> Mapping[str, Any]:
        """Canonical immutable parameter snapshot name."""
        return self.parameters


class StrategyStorageService:
    """Create private drafts, save them safely, and publish append-only revisions.

    Static source-contract validation is performed before publication, but this
    service never imports or executes private code.  The next delivery stage adds
    an isolated runtime validation runner while keeping draft persistence safe for
    unfinished work in progress.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = StrategyRepository(session)

    def create_strategy(
        self,
        *,
        name: str,
        source_code: str,
        description: str | None = None,
        parameter_schema: Mapping[str, Any] | None = None,
        default_parameters: Mapping[str, Any] | None = None,
    ) -> Strategy:
        """Create a private strategy identity together with its first draft.

        A caller receives a mutable draft but no executable revision.  It must
        explicitly publish after later validation, which prevents task scheduling
        from silently using half-written source.
        """
        strategy_id = uuid4()
        normalized_source = _validate_source_code(source_code)
        strategy = Strategy(
            id=strategy_id,
            name=_normalize_name(name),
            description=_normalize_description(description),
            state="active",
            version=1,
        )
        draft = StrategyDraft(
            strategy_id=strategy_id,
            source_code=normalized_source,
            source_hash=source_hash(normalized_source),
            parameter_schema=_normalize_json_object(
                parameter_schema, field_name="parameter_schema"
            ),
            default_parameters=_normalize_json_object(
                default_parameters, field_name="default_parameters"
            ),
            version=1,
        )
        self.session.add_all([strategy, draft])
        self.session.flush()
        return strategy

    def save_draft(
        self,
        strategy_id: UUID,
        *,
        expected_version: int,
        source_code: str | object = _UNSET,
        parameter_schema: Mapping[str, Any] | None | object = _UNSET,
        default_parameters: Mapping[str, Any] | None | object = _UNSET,
    ) -> StrategyDraft:
        """Patch a draft only when the editor is based on its current version.

        Omitted fields retain their stored values.  This matters for a browser
        editor that autosaves source and metadata independently: an older form
        submission must never clear a newer parameter contract by omission.
        """
        strategy = self._require_editable_strategy(strategy_id)
        draft = self.repository.get_draft(strategy.id, for_update=True)
        if draft is None:
            raise StrategyDraftNotFoundError(str(strategy_id))
        _assert_expected_draft_version(draft.version, expected_version)

        if (
            source_code is _UNSET
            and parameter_schema is _UNSET
            and default_parameters is _UNSET
        ):
            raise StrategyStorageValidationError(
                "at least one draft field must be supplied"
            )
        if source_code is not _UNSET:
            normalized_source = _validate_source_code(source_code)
            draft.source_code = normalized_source
            draft.source_hash = source_hash(normalized_source)
        if parameter_schema is not _UNSET:
            draft.parameter_schema = _normalize_json_object(
                parameter_schema, field_name="parameter_schema"
            )
        if default_parameters is not _UNSET:
            draft.default_parameters = _normalize_json_object(
                default_parameters, field_name="default_parameters"
            )
        draft.version += 1
        self.session.flush()
        return draft

    def validate_draft(
        self, strategy_id: UUID
    ) -> tuple[StrategyDraft, StrategyValidationResult]:
        """Check one stored draft without importing or executing private code.

        Validation intentionally remains a read-only static operation in this
        phase.  The later isolated worker can add runtime validation without
        changing the draft persistence or HTTP lifecycle contract.
        """
        strategy = self.repository.get_strategy(strategy_id)
        if strategy is None:
            raise StrategyNotFoundError(str(strategy_id))
        draft = self.repository.get_draft(strategy.id)
        if draft is None:
            raise StrategyDraftNotFoundError(str(strategy_id))
        if draft.source_hash != source_hash(draft.source_code):
            raise StrategyDraftIntegrityError(str(strategy_id))
        return draft, validate_strategy_draft(
            draft.source_code,
            parameter_schema=draft.parameter_schema,
            default_parameters=draft.default_parameters,
        )

    def update_strategy_metadata(
        self,
        strategy_id: UUID,
        *,
        expected_version: int,
        name: str | object = _UNSET,
        description: str | None | object = _UNSET,
    ) -> Strategy:
        """Change editable strategy metadata under its own optimistic lock.

        Draft code has an independent version so a name edit never makes an
        open code editor stale, while two concurrent metadata edits still cannot
        silently overwrite each other.
        """
        strategy = self._require_editable_strategy(strategy_id)
        _assert_expected_strategy_version(strategy.version, expected_version)
        if name is _UNSET and description is _UNSET:
            raise StrategyStorageValidationError(
                "at least one strategy metadata field must be supplied"
            )
        if name is not _UNSET:
            strategy.name = _normalize_name(name)
        if description is not _UNSET:
            strategy.description = _normalize_description(description)
        strategy.version += 1
        self.session.flush()
        return strategy

    def archive_strategy(
        self, strategy_id: UUID, *, expected_version: int
    ) -> Strategy:
        """Archive a strategy without deleting its source or run history."""
        strategy = self.repository.get_strategy(strategy_id, for_update=True)
        if strategy is None:
            raise StrategyNotFoundError(str(strategy_id))
        _assert_expected_strategy_version(strategy.version, expected_version)
        if strategy.state == "archived":
            raise StrategyAlreadyArchivedError(str(strategy_id))
        strategy.state = "archived"
        strategy.version += 1
        self.session.flush()
        return strategy

    def publish_revision(
        self,
        strategy_id: UUID,
        *,
        expected_draft_version: int,
        runtime_manifest: Mapping[str, Any] | None = None,
    ) -> StrategyRevision:
        """Snapshot the current draft as the next immutable executable revision.

        The strategy row lock is acquired before calculating the revision number.
        It prevents two web requests from assigning the same next number while
        the unique database constraint protects the invariant against every
        writer, including future maintenance tooling.
        """
        strategy = self._require_editable_strategy(strategy_id)
        draft = self.repository.get_draft(strategy.id, for_update=True)
        if draft is None:
            raise StrategyDraftNotFoundError(str(strategy_id))
        _assert_expected_draft_version(draft.version, expected_draft_version)

        computed_hash = source_hash(draft.source_code)
        if draft.source_hash != computed_hash:
            # Never turn an out-of-band source edit into a falsely attributed
            # published revision. An operator must first repair the draft record.
            raise StrategyDraftIntegrityError(str(strategy_id))

        validation = validate_strategy_draft(
            draft.source_code,
            parameter_schema=draft.parameter_schema,
            default_parameters=draft.default_parameters,
        )
        if not validation.valid:
            raise StrategyDraftValidationError(validation.issues)

        manifest = _normalize_json_object(
            (
                DEFAULT_RUNTIME_MANIFEST
                if runtime_manifest is None
                else runtime_manifest
            ),
            field_name="runtime_manifest",
        )
        # Publication-time protocol metadata check: the manifest must pin the
        # current official contract version, otherwise the revision could not
        # be interpreted by the backtest subprocess.
        declared_version = manifest.get("strategy_contract_version")
        if declared_version != STRATEGY_CONTRACT_VERSION:
            raise StrategyStorageValidationError(
                "runtime_manifest strategy_contract_version must equal "
                f"{STRATEGY_CONTRACT_VERSION}"
            )

        revision = StrategyRevision(
            id=uuid4(),
            strategy_id=strategy.id,
            revision_number=self.repository.next_revision_number(strategy.id),
            source_code=draft.source_code,
            source_hash=computed_hash,
            parameter_schema=deepcopy(draft.parameter_schema),
            default_parameters=deepcopy(draft.default_parameters),
            runtime_manifest=manifest,
        )
        self.session.add(revision)
        # Flush the child first so the composite current-revision foreign key can
        # be checked immediately when the parent pointer is updated below.
        self.session.flush()
        strategy.current_revision_id = revision.id
        strategy.version += 1
        self.session.flush()
        return revision

    def bind_published_revision(
        self,
        strategy_id: UUID,
        revision_id: UUID,
        *,
        parameters: Mapping[str, Any] | None = None,
        expected_source_hash: str | None = None,
    ) -> PublishedStrategyBinding:
        """Bind an exact published revision to a detached, read-only run input."""
        strategy = self.repository.get_strategy(strategy_id)
        revision = self.repository.get_revision(revision_id)
        if strategy is None:
            raise StrategyNotFoundError(str(strategy_id))
        if revision is None:
            raise StrategyRevisionBindingError("published revision not found")
        if revision.strategy_id != strategy_id:
            raise StrategyRevisionBindingError("revision does not belong to strategy")
        if strategy.current_revision_id is None:
            raise StrategyRevisionBindingError("strategy has no published revision")
        # Historical published revisions are valid run inputs when explicitly
        # selected; drafts/current pointers are never substituted implicitly.
        if revision.source_hash != source_hash(revision.source_code):
            raise StrategyRevisionBindingError("published revision source hash mismatch")
        if expected_source_hash is not None and revision.source_hash != expected_source_hash:
            raise StrategyRevisionBindingError("published revision hash mismatch")
        manifest = dict(revision.runtime_manifest or {})
        if manifest.get("strategy_contract_version") != STRATEGY_CONTRACT_VERSION:
            raise StrategyRevisionBindingError("unsupported strategy contract version")
        supplied = revision.default_parameters if parameters is None else parameters
        normalized = _normalize_json_object(supplied, field_name="parameters")
        issues = _validate_runtime_parameters(revision.parameter_schema or {}, normalized)
        if issues:
            raise StrategyRevisionBindingError("parameters do not satisfy revision schema: " + "; ".join(issues))
        return PublishedStrategyBinding(
            strategy_id=strategy_id,
            revision_id=revision.id,
            revision_number=revision.revision_number,
            source_code=revision.source_code,
            source_hash=revision.source_hash,
            parameter_schema=_deep_freeze(revision.parameter_schema or {}),
            parameters=_deep_freeze(normalized),
            runtime_manifest=_deep_freeze(manifest),
            strategy_contract_version=STRATEGY_CONTRACT_VERSION,
        )

    # Explicit alias used by run-start callers; both names intentionally share
    # the same strict binding semantics and return the same detached snapshot.
    resolve_published_binding = bind_published_revision

    def _require_editable_strategy(self, strategy_id: UUID) -> Strategy:
        """Lock and validate the strategy before changing its draft lifecycle."""
        strategy = self.repository.get_strategy(strategy_id, for_update=True)
        if strategy is None:
            raise StrategyNotFoundError(str(strategy_id))
        if strategy.state == "archived":
            raise StrategyArchivedError(str(strategy_id))
        return strategy


def source_hash(source_code: str) -> str:
    """Return the lowercase SHA-256 digest for exact UTF-8 source text."""
    return sha256(source_code.encode("utf-8")).hexdigest()


def _validate_source_code(value: str) -> str:
    """Reject blank or oversized source before it reaches PostgreSQL."""
    if not isinstance(value, str):
        raise StrategyStorageValidationError("source_code must be a string")
    if not value.strip():
        raise StrategyStorageValidationError("source_code must not be blank")
    try:
        source_bytes = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StrategyStorageValidationError(
            "source_code must contain valid UTF-8 text"
        ) from exc
    if len(source_bytes) > MAX_STRATEGY_SOURCE_BYTES:
        raise StrategyStorageValidationError(
            f"source_code exceeds {MAX_STRATEGY_SOURCE_BYTES} UTF-8 bytes"
        )
    return value


def _normalize_name(value: str) -> str:
    """Normalize the user-facing name before the database nonblank constraint."""
    if not isinstance(value, str):
        raise StrategyStorageValidationError("name must be a string")
    normalized = value.strip()
    if not normalized:
        raise StrategyStorageValidationError("name must not be blank")
    if len(normalized) > 100:
        raise StrategyStorageValidationError("name exceeds 100 characters")
    return normalized


def _normalize_description(value: str | None) -> str | None:
    """Keep empty descriptions semantically absent and bound their storage size."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise StrategyStorageValidationError("description must be a string or null")
    normalized = value.strip()
    if len(normalized) > 10_000:
        raise StrategyStorageValidationError("description exceeds 10000 characters")
    return normalized or None


def _normalize_json_object(
    value: Mapping[str, Any] | None, *, field_name: str
) -> dict[str, Any]:
    """Deep-copy and normalize a JSON object without accepting Python-only data.

    PostgreSQL JSONB is the final persistence type, but serializing here gives
    callers a deterministic validation error instead of a database-driver error.
    Re-loading the encoded value also converts nested tuples and mapping wrappers
    into the plain JSON structures stored in each revision snapshot.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise StrategyStorageValidationError(f"{field_name} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise StrategyStorageValidationError(
            f"{field_name} object keys must be strings"
        )
    try:
        serialized = json.dumps(
            deepcopy(dict(value)),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        # PostgreSQL text and JSONB both require valid UTF-8.  JSON encoding can
        # preserve a lone surrogate in a Python string, so explicitly test the
        # encoded bytes before allowing that value into an ORM object.
        serialized.encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StrategyStorageValidationError(
            f"{field_name} must contain JSON-compatible values"
        ) from exc
    decoded = json.loads(serialized)
    if not isinstance(decoded, dict):
        raise StrategyStorageValidationError(f"{field_name} must be a JSON object")
    return decoded


def _assert_expected_draft_version(current: int, expected: int) -> None:
    """Raise an explicit conflict instead of silently overwriting another edit."""
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise StrategyStorageValidationError(
            "expected draft version must be a positive integer"
        )
    if current != expected:
        raise StrategyDraftConflictError(
            f"draft version does not match: expected {expected}, current {current}"
        )


def _assert_expected_strategy_version(current: int, expected: int) -> None:
    """Reject stale metadata operations before touching lifecycle state."""
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise StrategyStorageValidationError(
            "expected strategy version must be a positive integer"
        )
    if current != expected:
        raise StrategyMetadataConflictError(
            f"strategy version does not match: expected {expected}, current {current}"
        )


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze JSON values crossing into a worker."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _validate_runtime_parameters(schema: Mapping[str, Any], parameters: Mapping[str, Any]) -> list[str]:
    """Validate the small JSON-Schema profile supported by strategy v1."""
    issues: list[str] = []
    if schema.get("type") not in (None, "object"):
        issues.append("schema type must be object")
    required = schema.get("required", [])
    if isinstance(required, list):
        issues.extend(f"missing required parameter: {name}" for name in required if name not in parameters)
    properties = schema.get("properties", {})
    if isinstance(properties, Mapping):
        if schema.get("additionalProperties") is False:
            issues.extend(
                f"unknown parameter: {name}"
                for name in parameters
                if name not in properties
            )
        for name, rule in properties.items():
            if name not in parameters or not isinstance(rule, Mapping):
                continue
            expected = rule.get("type")
            value = parameters[name]
            valid = {"object": isinstance(value, Mapping), "array": isinstance(value, list),
                     "string": isinstance(value, str), "boolean": isinstance(value, bool),
                     "integer": isinstance(value, int) and not isinstance(value, bool),
                     "number": isinstance(value, (int, float)) and not isinstance(value, bool)}.get(expected, True)
            if not valid:
                issues.append(f"parameter {name} has invalid type")
    return issues
