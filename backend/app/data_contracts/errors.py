"""Stable shared calendar/identity errors; no execution admission or old data adapter."""
from __future__ import annotations
import math
from types import MappingProxyType
from typing import Mapping
from app.backtest_service.config_values import DomainValidationError

def freeze_json(value: object, field_name: str) -> Mapping[str, object] | tuple[object, ...] | None | str | bool | int | float:
    """Recursively deep-freeze one JSON value and reject non-JSON objects.

    Only exact JSON scalar types pass through (subclasses could smuggle
    mutable attributes); mappings become read-only ``MappingProxyType``
    instances and sequences become tuples.  Floats must be finite because
    NaN and infinities are not valid JSON numbers.  Sets, custom classes,
    and other non-JSON objects are rejected outright.
    """

    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{field_name} float values must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{field_name} keys must be plain strings")
            frozen[key] = freeze_json(item, f"{field_name}[{key!r}]")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item, field_name) for item in value)
    raise ValueError(
        f"{field_name} must only contain JSON scalars, mappings, or sequences"
    )


class DataContractError(DomainValidationError):
    """Base error for every generic data-contract violation.

    ``code`` is a stable machine identifier that never changes meaning;
    ``message`` is display-safe text and ``details`` is deep-frozen JSON
    context.  Callers branch on ``code``, never on the message.
    """

    code = "data_contract_error"

    def __init__(
        self, message: str, *, details: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(message)
        if details is None:
            self.details: Mapping[str, object] = MappingProxyType({})
        else:
            self.details = freeze_json(details, "details")


class InvalidDataRequestError(DataContractError):
    """A request or query object violates a data-contract invariant."""

    code = "invalid_data_request"


class DataCutoffExceededError(DataContractError):
    """A query would read past ``data_cutoff``; it fails instead of trimming."""

    code = "data_cutoff_exceeded"


class DataCutoffRequiredError(DataContractError):
    """A strict calendar query did not provide an explicit data cutoff."""

    code = "data_cutoff_required"


class IdentityMappingIncompleteError(DataContractError):
    """PIT identity-mapping evidence does not cover the requested window."""

    code = "identity_mapping_incomplete"


class IdentityMappingConflictError(DataContractError):
    """Two or more PIT mappings claim the same requested session."""

    code = "identity_mapping_conflict"


class IdentityMappingEvidenceMissingError(DataContractError):
    """A covering PIT mapping carries no usable evidence."""

    code = "identity_mapping_evidence_missing"


class ProviderContractViolationError(DataContractError):
    """A provider returned wrong, out-of-range, unordered, or mutable data."""

    code = "provider_contract_violation"


class CalendarContractError(DataContractError, DomainValidationError):
    """Base class for stable named-calendar contract failures."""

    code = "calendar_contract_error"


class CalendarIdUnknownError(CalendarContractError):
    code = "calendar_id_unknown"


class CalendarRegistryFactMissingError(CalendarContractError):
    code = "calendar_registry_fact_missing"


class CalendarRegistryReferenceInvalidError(CalendarContractError):
    code = "calendar_registry_reference_invalid"


class CalendarRegistryAmbiguousError(CalendarContractError):
    code = "calendar_registry_ambiguous"


class CalendarBindingUnknownError(CalendarContractError):
    code = "calendar_binding_unknown"


class CalendarBindingAmbiguousError(CalendarContractError):
    code = "calendar_binding_ambiguous"


class InstrumentCalendarUnresolvedError(CalendarContractError):
    code = "instrument_calendar_unresolved"


class CalendarDefinitionMissingError(CalendarContractError):
    code = "calendar_definition_missing"


class CalendarDefinitionAmbiguousError(CalendarContractError):
    code = "calendar_definition_ambiguous"


class CalendarDefinitionInvalidError(CalendarContractError):
    code = "calendar_definition_invalid"


class CalendarPitMetadataMissingError(CalendarContractError):
    code = "calendar_pit_metadata_missing"


class CalendarFactMissingError(CalendarContractError):
    code = "calendar_fact_missing"


class CalendarFactAmbiguousError(CalendarContractError):
    code = "calendar_fact_ambiguous"


class CalendarFactInvalidError(CalendarContractError):
    code = "calendar_fact_invalid"


class CalendarSessionInvalidError(CalendarContractError):
    code = "calendar_session_invalid"


class CalendarTimezoneUnsupportedError(CalendarContractError):
    code = "calendar_timezone_unsupported"


class CalendarSourcePriorityMissingError(CalendarContractError):
    code = "calendar_source_priority_missing"


class CalendarSourcePriorityInvalidError(CalendarContractError):
    code = "calendar_source_priority_invalid"


class CalendarSourcePriorityAmbiguousError(CalendarContractError):
    code = "calendar_source_priority_ambiguous"


class CalendarSourcePriorityChainBrokenError(CalendarContractError):
    code = "calendar_source_priority_chain_broken"


class CalendarJsonInvalidError(CalendarContractError):
    code = "calendar_json_invalid"


class CalendarSessionWindowLimitExceededError(CalendarContractError):
    code = "calendar_session_window_limit_exceeded"


class CalendarCrossMidnightUnsupportedError(CalendarContractError):
    code = "calendar_cross_midnight_unsupported"


class CalendarIngestionRangeIncompleteError(CalendarContractError):
    code = "calendar_ingestion_range_incomplete"


class CalendarSourceRevisionConflictError(CalendarContractError):
    code = "calendar_source_revision_conflict"


class LegacyExchangeAmbiguousError(CalendarContractError):
    code = "legacy_exchange_ambiguous"

ERROR_CODES = frozenset(cls.code for cls in tuple(globals().values()) if isinstance(cls, type) and issubclass(cls, DataContractError))
