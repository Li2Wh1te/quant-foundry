"""Versioned ETF adjustment policy and its fail-closed activation gate.

The policy is deliberately data, not a switch.  A caller may describe the
first-version Tushare contract while it is inactive, but an active instance
must carry a published verification artifact with complete hashes and source
semantics.  Keeping this object immutable means a strategy cannot turn an
unverified policy on after a run has been admitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from app.backtesting.data.errors import InvalidDataRequestError, freeze_json
from app.instruments.references import VersionedReference

__all__ = [
    "ADJUSTMENT_SERIES_POLICY_KEY",
    "ADJUSTMENT_SERIES_POLICY_VERSION",
    "ADJUSTMENT_SERIES_POLICY_REF",
    "ADJUSTMENT_ADAPTER_VERSION",
    "AdjustmentPolicyStatus",
    "AdjustmentSeriesPolicy",
    "AdjustmentPolicy",
    "INACTIVE_ADJUSTMENT_POLICY",
    "get_registered_adjustment_policy",
    "registered_adjustment_policies",
]


ADJUSTMENT_SERIES_POLICY_KEY = "tushare_adj_factor_native"
"""Stable machine key for the only registered first-version policy."""

ADJUSTMENT_SERIES_POLICY_VERSION = 1
"""Stable version for :data:`ADJUSTMENT_SERIES_POLICY_KEY`."""

ADJUSTMENT_SERIES_POLICY_REF = VersionedReference(
    key=ADJUSTMENT_SERIES_POLICY_KEY,
    version=ADJUSTMENT_SERIES_POLICY_VERSION,
)

ADJUSTMENT_ADAPTER_VERSION = "etf_raw_bar_adapter@1"

_CUTOFF_RULE = "effective_date <= data_cutoff"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:access[_-]?token|api[_-]?key|authorization|bearer|credential|password|secret|token)",
    re.IGNORECASE,
)
_ACTIVE_STATUSES = frozenset({"verified", "passed", "active"})


class AdjustmentPolicyStatus(StrEnum):
    """Lifecycle state of the registered adjustment policy."""

    INACTIVE = "inactive"
    ACTIVE = "active"


def _text(value: object, field: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise InvalidDataRequestError(f"{field} must be non-blank text")
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidDataRequestError(f"{field} must be non-blank text")
    return value.strip()


def _hash(value: object, field: str, *, required: bool = True) -> str | None:
    text = _text(value, field, required=required)
    if text is None:
        return None
    if _HASH_RE.fullmatch(text) is None:
        raise InvalidDataRequestError(
            f"{field} must be a lowercase SHA-256 hexadecimal digest"
        )
    return text


def _precision(value: object, field: str) -> int | str:
    # Source declarations occasionally name a decimal precision instead of
    # giving a number.  Preserve either form, but reject empty/negative data.
    if isinstance(value, bool):
        raise InvalidDataRequestError(f"{field} must be a non-negative integer or text")
    if isinstance(value, int):
        if value < 0:
            raise InvalidDataRequestError(f"{field} must be non-negative")
        return value
    if isinstance(value, Mapping):
        for key in ("price_decimal_places", "decimal_places", "places"):
            if key in value:
                return _precision(value[key], field)
        raise InvalidDataRequestError(
            f"{field} must declare decimal places"
        )
    return _text(value, field)  # type: ignore[return-value]


def _mapping(value: Mapping[str, object] | None) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise InvalidDataRequestError("verification must be a mapping")

    def redact(item: object) -> object:
        """Remove credential-shaped fields before evidence enters the policy.

        A full verifier artifact still fails closed when it contains a
        credential.  Compact, already-derived evidence mappings are handled
        at this boundary by dropping those fields so that a source token can
        never be retained in an immutable policy, preflight payload, or
        content hash.
        """

        if isinstance(item, Mapping):
            return {
                str(key): redact(nested)
                for key, nested in item.items()
                if not (
                    isinstance(key, str) and _SENSITIVE_KEY_RE.search(key)
                )
            }
        if isinstance(item, (list, tuple)):
            return [redact(nested) for nested in item]
        return item

    sanitized = redact(value)
    if not isinstance(sanitized, Mapping):  # pragma: no cover - defensive
        raise InvalidDataRequestError("verification must be a mapping")
    try:
        frozen = freeze_json(dict(sanitized), "verification")
    except ValueError as exc:
        raise InvalidDataRequestError(str(exc)) from exc
    assert isinstance(frozen, MappingProxyType)
    return frozen


def _artifact_value(artifact: object, *path: str, default: object = None) -> object:
    """Read a value from either a mapping or a verification artifact object."""

    current = artifact
    for part in path:
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
        else:
            current = getattr(current, part, default)
            if current is default:
                return default
    return current


def _artifact_fields(artifact: object) -> dict[str, object]:
    """Flatten the verification artifact shapes emitted by task 14-02."""

    # The JSON artifact uses nested ``policy``, ``adapter``, ``semantics``
    # and ``verification`` objects.  Dataclass artifacts expose the same
    # names as attributes.  Accepting both keeps policy construction
    # independent of the artifact serializer.
    nested = {
        "key": _artifact_value(artifact, "policy", "key"),
        "version": _artifact_value(artifact, "policy", "version"),
        "adapter_version": _artifact_value(artifact, "adapter", "version"),
        "source": _artifact_value(artifact, "source", "name"),
        "factor_field": _artifact_value(artifact, "field_mapping", "adj_factor"),
        "effective_date": _artifact_value(
            artifact, "field_mapping", "effective_date"
        ),
        "cutoff_rule": _artifact_value(artifact, "semantics", "cutoff_rule"),
        "qfq_formula": _artifact_value(artifact, "semantics", "qfq_formula"),
        "hfq_formula": _artifact_value(artifact, "semantics", "hfq_formula"),
        "qfq_anchor": _artifact_value(artifact, "semantics", "qfq_anchor"),
        "hfq_anchor": _artifact_value(artifact, "semantics", "hfq_anchor"),
        "precision": _artifact_value(artifact, "semantics", "precision"),
        "rounding": _artifact_value(artifact, "semantics", "rounding"),
        "verification_summary": _artifact_value(
            artifact, "verification", "summary"
        ),
        "verification_status": _artifact_value(
            artifact, "verification", "status"
        ),
        "verification_input_hash": _artifact_value(
            artifact, "verification", "input_hash"
        ),
        "verification_output_hash": _artifact_value(
            artifact, "verification", "output_hash"
        ),
        "verification_evidence_hash": _artifact_value(
            artifact, "verification", "evidence_hash"
        ),
        "verification_published": _artifact_value(
            artifact, "verification", "published"
        ),
    }

    # The checked-in verifier artifact uses ``mapping`` rather than the
    # compact ``field_mapping`` spelling and nests hashes below
    # ``verification.hashes``.  Read those exact paths as a second supported
    # shape; no arbitrary recursive search is performed.
    verifier_fields = {
        "factor_field": _artifact_value(artifact, "mapping", "factor_field"),
        "effective_date": _artifact_value(artifact, "mapping", "effective_date"),
        "cutoff_rule": _artifact_value(artifact, "semantics", "cutoff_rule"),
        "verification_input_hash": _artifact_value(
            artifact, "verification", "hashes", "input_hash"
        ),
        "verification_output_hash": _artifact_value(
            artifact, "verification", "hashes", "output_hash"
        ),
        "verification_evidence_hash": _artifact_value(
            artifact, "verification", "hashes", "evidence_hash"
        ),
    }
    for field, value in verifier_fields.items():
        if nested[field] is None and value is not None:
            nested[field] = value
    if nested["verification_published"] is None:
        # ``adjustment_verification`` has no mutable publication switch: a
        # verifier status of ``passed`` plus reproducible hashes is the
        # publication marker for its checked-in artifact.
        nested["verification_published"] = (
            isinstance(nested["verification_status"], str)
            and nested["verification_status"].casefold() == "passed"
            and all(
                nested[name] is not None
                for name in (
                    "verification_input_hash",
                    "verification_output_hash",
                    "verification_evidence_hash",
                )
            )
        )
    if nested["verification_summary"] is None and nested["verification_status"] is not None:
        nested["verification_summary"] = (
            "published verification artifact status="
            + str(nested["verification_status"])
        )

    # Flat artifact DTOs use the machine names directly.  These fallbacks are
    # intentionally explicit; arbitrary attribute copying would make it too
    # easy for an unrelated field to masquerade as verification evidence.
    flat_aliases = {
        "key": ("policy_key", "key"),
        "version": ("policy_version", "version"),
        "adapter_version": ("adapter_version",),
        "source": ("source",),
        "factor_field": ("factor_field",),
        "effective_date": ("effective_date",),
        "cutoff_rule": ("cutoff_rule",),
        "qfq_formula": ("qfq_formula",),
        "hfq_formula": ("hfq_formula",),
        "qfq_anchor": ("qfq_anchor",),
        "hfq_anchor": ("hfq_anchor",),
        "precision": ("precision",),
        "rounding": ("rounding",),
        "verification_summary": ("verification_summary", "summary"),
        "verification_status": ("verification_status", "status"),
        "verification_input_hash": ("verification_input_hash", "input_hash"),
        "verification_output_hash": ("verification_output_hash", "output_hash"),
        "verification_evidence_hash": (
            "verification_evidence_hash",
            "evidence_hash",
        ),
        "verification_published": ("verification_published", "published"),
    }
    for output_name, names in flat_aliases.items():
        if nested[output_name] is not None:
            continue
        for name in names:
            value = _artifact_value(artifact, name)
            if value is not None:
                nested[output_name] = value
                break

    if nested["source"] is not None and isinstance(nested["source"], Mapping):
        nested["source"] = nested["source"].get("name")

    # Some artifact classes expose policy fields directly.  Direct fields
    # fill only missing nested values, so nested JSON remains authoritative.
    aliases = {
        "adapter_version": "adapter_version",
        "source": "source",
        "factor_field": "factor_field",
        "effective_date": "effective_date",
        "cutoff_rule": "cutoff_rule",
        "qfq_formula": "qfq_formula",
        "hfq_formula": "hfq_formula",
        "qfq_anchor": "qfq_anchor",
        "hfq_anchor": "hfq_anchor",
        "precision": "precision",
        "rounding": "rounding",
        "verification_summary": "verification_summary",
        "verification_status": "verification_status",
        "verification_input_hash": "verification_input_hash",
        "verification_output_hash": "verification_output_hash",
        "verification_evidence_hash": "verification_evidence_hash",
        "verification_published": "verification_published",
    }
    for output_name, attr_name in aliases.items():
        if nested[output_name] is None:
            value = _artifact_value(artifact, attr_name)
            if value is not None:
                nested[output_name] = value

    # ``verification_evidence`` is a common compact summary name.
    if nested["verification_summary"] is None:
        nested["verification_summary"] = _artifact_value(
            artifact, "verification_evidence"
        )
    return nested


@dataclass(frozen=True, slots=True)
class AdjustmentSeriesPolicy:
    """Immutable description of the registered ETF adjustment contract.

    Inactive policies may omit verification details so the capability can be
    advertised safely.  Active policies are accepted only through
    :meth:`from_verification_artifact` or :meth:`active`, both of which run
    :meth:`validate_activation` before returning.
    """

    key: str = ADJUSTMENT_SERIES_POLICY_KEY
    version: int = ADJUSTMENT_SERIES_POLICY_VERSION
    status: AdjustmentPolicyStatus = AdjustmentPolicyStatus.INACTIVE
    adapter_version: str = ADJUSTMENT_ADAPTER_VERSION
    source: str = "tushare"
    factor_field: str = "adj_factor"
    effective_date: str = "trade_date"
    cutoff_rule: str = _CUTOFF_RULE
    qfq_formula: str | None = None
    hfq_formula: str | None = None
    qfq_anchor: str | None = None
    hfq_anchor: str | None = None
    precision: int | str | None = None
    rounding: str | None = None
    verification_summary: str | None = None
    verification_input_hash: str | None = None
    verification_output_hash: str | None = None
    verification_evidence_hash: str | None = None
    verification_status: str | None = None
    verification_published: bool = False
    verification: Mapping[str, object] = MappingProxyType({})
    # Naming aliases retained for artifact serializers that call the
    # evidence summary or cutoff rule by their persisted field names.
    verification_evidence: str | None = None
    factor_cutoff_rule: str | None = None

    def __post_init__(self) -> None:
        if self.key != ADJUSTMENT_SERIES_POLICY_KEY:
            raise InvalidDataRequestError(
                "only tushare_adj_factor_native is registered for ETF adjustments"
            )
        if type(self.version) is not int or self.version != ADJUSTMENT_SERIES_POLICY_VERSION:
            raise InvalidDataRequestError(
                "only version 1 of tushare_adj_factor_native is registered"
            )
        status = self.status
        try:
            status = AdjustmentPolicyStatus(status)
        except (TypeError, ValueError) as exc:
            raise InvalidDataRequestError(
                "adjustment policy status must be inactive or active"
            ) from exc
        object.__setattr__(self, "status", status)
        for name in (
            "adapter_version",
            "source",
            "factor_field",
            "effective_date",
            "cutoff_rule",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.source.casefold() != "tushare":
            raise InvalidDataRequestError(
                "tushare_adj_factor_native@1 requires source=tushare"
            )
        if self.factor_field != "adj_factor":
            raise InvalidDataRequestError(
                "tushare_adj_factor_native@1 requires factor_field=adj_factor"
            )
        if not (
            self.effective_date in {"trade_date", "trade_date -> effective_date"}
            or (
                self.effective_date.startswith("trade_date")
                and "normalized" in self.effective_date
            )
        ):
            raise InvalidDataRequestError(
                "effective_date must map the source trade_date field"
            )
        if self.cutoff_rule != _CUTOFF_RULE:
            raise InvalidDataRequestError(
                "adjustment policy cutoff_rule must be effective_date <= data_cutoff"
            )
        for name in ("qfq_formula", "hfq_formula", "qfq_anchor", "hfq_anchor", "rounding"):
            object.__setattr__(
                self,
                name,
                _text(getattr(self, name), name, required=False),
            )
        if self.precision is not None:
            object.__setattr__(self, "precision", _precision(self.precision, "precision"))
        for name in (
            "verification_summary",
            "verification_status",
        ):
            object.__setattr__(
                self,
                name,
                _text(getattr(self, name), name, required=False),
            )
        if self.verification_summary is None and self.verification_evidence is not None:
            object.__setattr__(
                self,
                "verification_summary",
                _text(self.verification_evidence, "verification_evidence"),
            )
        if self.verification_evidence is not None:
            object.__setattr__(
                self, "verification_evidence", self.verification_summary
            )
        if self.factor_cutoff_rule is not None:
            if self.factor_cutoff_rule != _CUTOFF_RULE:
                raise InvalidDataRequestError(
                    "factor_cutoff_rule must be effective_date <= data_cutoff"
                )
            object.__setattr__(self, "cutoff_rule", self.factor_cutoff_rule)
        if self.verification_status is not None:
            object.__setattr__(
                self, "verification_status", self.verification_status.casefold()
            )
        for name in (
            "verification_input_hash",
            "verification_output_hash",
            "verification_evidence_hash",
        ):
            object.__setattr__(
                self,
                name,
                _hash(getattr(self, name), name, required=False),
            )
        if not isinstance(self.verification_published, bool):
            raise InvalidDataRequestError("verification_published must be a boolean")
        object.__setattr__(self, "verification", _mapping(self.verification))
        if status is AdjustmentPolicyStatus.ACTIVE:
            self.validate_activation()

    @classmethod
    def inactive(cls, **overrides: object) -> "AdjustmentSeriesPolicy":
        """Return the sole registered policy in its safe default state."""

        values = {"status": AdjustmentPolicyStatus.INACTIVE}
        values.update(overrides)
        return cls(**values)

    @classmethod
    def active(
        cls,
        *,
        verification: Mapping[str, object] | object | None = None,
        **overrides: object,
    ) -> "AdjustmentSeriesPolicy":
        """Construct active policy only from complete published evidence."""

        values: dict[str, object] = dict(overrides)
        if verification is not None:
            # A task-14 verifier artifact has a complete schema and can be
            # checked before any fields are copied into the policy.  Compact
            # evidence mappings used by callers/tests still go through the
            # field and hash checks below.
            if isinstance(verification, Mapping) and "artifact_schema_version" in verification:
                try:
                    from app.backtesting.data.adjustment_verification import assert_verified

                    assert_verified(verification)
                except Exception as exc:
                    raise InvalidDataRequestError(
                        "verification artifact has not passed real-source validation"
                    ) from exc
            values.update(_artifact_fields(verification))
            if isinstance(verification, Mapping):
                values["verification"] = verification
        values["status"] = AdjustmentPolicyStatus.ACTIVE
        return cls(**values)

    @classmethod
    def from_verification_artifact(cls, artifact: Mapping[str, object] | object) -> "AdjustmentSeriesPolicy":
        """Alias emphasizing that active construction is artifact-bound."""

        return cls.active(verification=artifact)

    def validate_activation(self) -> None:
        """Fail closed unless all frozen semantics and artifact evidence exist."""

        required_text = {
            "qfq_formula": self.qfq_formula,
            "hfq_formula": self.hfq_formula,
            "qfq_anchor": self.qfq_anchor,
            "hfq_anchor": self.hfq_anchor,
            "rounding": self.rounding,
            "verification_summary": self.verification_summary,
            "verification_evidence": self.verification_summary,
            "verification_status": self.verification_status,
        }
        for field, value in required_text.items():
            if value is None or not value.strip():
                raise InvalidDataRequestError(
                    f"active adjustment policy requires {field}"
                )
        if self.precision is None:
            raise InvalidDataRequestError("active adjustment policy requires precision")
        for field in (
            "verification_input_hash",
            "verification_output_hash",
            "verification_evidence_hash",
        ):
            if getattr(self, field) is None:
                raise InvalidDataRequestError(
                    f"active adjustment policy requires {field}"
                )
        if self.verification_status not in _ACTIVE_STATUSES:
            raise InvalidDataRequestError(
                "active adjustment policy requires a passed verification artifact"
            )
        if not self.verification_published:
            raise InvalidDataRequestError(
                "active adjustment policy requires a published verification artifact"
            )
        if self.adapter_version != ADJUSTMENT_ADAPTER_VERSION:
            raise InvalidDataRequestError(
                "active adjustment policy adapter_version is not the registered ETF adapter"
            )
        # The descriptor is the source of truth for the research generator;
        # reject unknown formula/anchor/rounding identifiers at activation
        # time instead of allowing a policy that can only fail halfway
        # through a strategy read.  Import lazily to keep this policy module
        # independent of the adapter's fact DTO imports during package init.
        try:
            from app.backtesting.data.etf_adjustment import (
                _anchor_index,
                _formula_kind,
                _precision as _research_precision,
                _rounding as _research_rounding,
            )
            from app.backtesting.data.requests import PriceBasis

            _formula_kind(self.qfq_formula, PriceBasis.QFQ)
            _formula_kind(self.hfq_formula, PriceBasis.HFQ)
            _anchor_index(self.qfq_anchor, PriceBasis.QFQ)
            _anchor_index(self.hfq_anchor, PriceBasis.HFQ)
            _research_precision(self.precision)
            _research_rounding(self.rounding)
        except InvalidDataRequestError:
            raise
        except Exception as exc:  # pragma: no cover - defensive import guard
            raise InvalidDataRequestError(
                "active adjustment policy research semantics are invalid"
            ) from exc
        artifact_adapter = self.verification.get("adapter_version")
        if artifact_adapter is not None and artifact_adapter != self.adapter_version:
            raise InvalidDataRequestError(
                "verification artifact adapter_version does not match policy"
            )
        artifact_key = self.verification.get("policy_key")
        if artifact_key is not None and artifact_key != self.key:
            raise InvalidDataRequestError(
                "verification artifact policy key does not match policy"
            )
        artifact_version = self.verification.get("policy_version")
        if artifact_version is not None and artifact_version != self.version:
            raise InvalidDataRequestError(
                "verification artifact policy version does not match policy"
            )

    @property
    def reference(self) -> VersionedReference:
        return ADJUSTMENT_SERIES_POLICY_REF

    @property
    def policy_key(self) -> str:
        """Render the machine key in the persisted ``key@version`` form."""

        return f"{self.key}@{self.version}"

    @property
    def formula_version(self) -> str | None:
        if self.qfq_formula is None or self.hfq_formula is None:
            return None
        return f"qfq:{self.qfq_formula};hfq:{self.hfq_formula}"

    def is_active(self) -> bool:
        return self.status is AdjustmentPolicyStatus.ACTIVE

    def as_dict(self) -> Mapping[str, object]:
        """Return a JSON-safe immutable policy description for audit records."""

        payload = {
            "key": self.key,
            "version": self.version,
            "policy_key": self.policy_key,
            "status": self.status.value,
            "adapter_version": self.adapter_version,
            "source": self.source,
            "factor_field": self.factor_field,
            "effective_date": self.effective_date,
            "cutoff_rule": self.cutoff_rule,
            "qfq_formula": self.qfq_formula,
            "hfq_formula": self.hfq_formula,
            "formula_version": self.formula_version,
            "qfq_anchor": self.qfq_anchor,
            "hfq_anchor": self.hfq_anchor,
            "precision": self.precision,
            "rounding": self.rounding,
            "verification_summary": self.verification_summary,
            "verification_status": self.verification_status,
            "verification_published": self.verification_published,
            "verification_input_hash": self.verification_input_hash,
            "verification_output_hash": self.verification_output_hash,
            "verification_evidence_hash": self.verification_evidence_hash,
            # Keep raw artifact rows out of capability/run hashes.  The
            # individual digests and concise status below are sufficient to
            # identify the evidence; the full artifact remains versioned in
            # its own file and is never embedded in a run record.
            "verification": {
                "summary": self.verification_summary,
                "status": self.verification_status,
                "published": self.verification_published,
                "input_hash": self.verification_input_hash,
                "output_hash": self.verification_output_hash,
                "evidence_hash": self.verification_evidence_hash,
            },
            "factor_cutoff_rule": self.cutoff_rule,
        }
        frozen = freeze_json(payload, "adjustment_policy")
        assert isinstance(frozen, MappingProxyType)
        return frozen


# Short alias for callers that call all versioned descriptors "policies".
AdjustmentPolicy = AdjustmentSeriesPolicy


INACTIVE_ADJUSTMENT_POLICY = AdjustmentSeriesPolicy.inactive()
"""The one registered policy descriptor, safe until evidence is published."""


def registered_adjustment_policies() -> Mapping[str, AdjustmentSeriesPolicy]:
    """Return a read-only registry containing exactly one policy."""

    return MappingProxyType({INACTIVE_ADJUSTMENT_POLICY.policy_key: INACTIVE_ADJUSTMENT_POLICY})


def get_registered_adjustment_policy(
    key: str = ADJUSTMENT_SERIES_POLICY_KEY,
    version: int = ADJUSTMENT_SERIES_POLICY_VERSION,
) -> AdjustmentSeriesPolicy:
    """Resolve the fixed registration; unknown key/version fails closed."""

    if (
        key != ADJUSTMENT_SERIES_POLICY_KEY
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version != ADJUSTMENT_SERIES_POLICY_VERSION
    ):
        raise InvalidDataRequestError(
            "unknown adjustment policy; only tushare_adj_factor_native@1 is registered"
        )
    return INACTIVE_ADJUSTMENT_POLICY
