"""Exact ``key + version`` registry for instrument rule packages.

The registry deliberately has no "latest version" lookup: callers must
name the exact version they were written against.  Definitions become
immutable once registered, and duplicate registration of the same
``(key, version)`` is a development/configuration error that raises.
"""

from __future__ import annotations

from app.backtesting.domain import DomainValidationError
from app.instruments.domain import VersionedReference
from app.instruments.rules.contracts import RulePackageDefinition


class RulePackageNotRegisteredError(DomainValidationError):
    """Raised by :meth:`RulePackageRegistry.require` for unknown packages."""


class RulePackageRegistrationError(DomainValidationError):
    """Raised when a package cannot be registered (for example duplicates)."""


class RulePackageRegistry:
    """Immutable store of rule-package definitions keyed by ``(key, version)``."""

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, int], RulePackageDefinition] = {}

    def register(self, definition: RulePackageDefinition) -> None:
        """Register one definition; duplicate ``key + version`` is rejected.

        The definition is stored as-is: it is already frozen after
        construction, so registered packages can never be modified in
        place.  New behavior requires a new version.
        """

        if not isinstance(definition, RulePackageDefinition):
            raise DomainValidationError(
                "definition must be a RulePackageDefinition"
            )
        identity = (definition.reference.key, definition.reference.version)
        if identity in self._definitions:
            existing = self._definitions[identity]
            raise RulePackageRegistrationError(
                "rule package already registered for "
                f"{existing.reference.key}@{existing.reference.version}; "
                "registering a different definition under an existing "
                "key/version is forbidden"
            )
        self._definitions[identity] = definition

    def get(self, reference: VersionedReference) -> RulePackageDefinition | None:
        """Return the definition for an exact reference, or ``None``.

        A missing version never falls back to another version of the same
        key: there is intentionally no "latest" resolution path.
        """

        if not isinstance(reference, VersionedReference):
            raise DomainValidationError("reference must be a VersionedReference")
        return self._definitions.get((reference.key, reference.version))

    def require(self, reference: VersionedReference) -> RulePackageDefinition:
        """Return the definition or raise a stable not-registered error."""

        definition = self.get(reference)
        if definition is None:
            raise RulePackageNotRegisteredError(
                f"no rule package registered for {reference.key}@{reference.version}"
            )
        return definition

    def list(self) -> tuple[RulePackageDefinition, ...]:
        """Return all definitions sorted stably by key then version."""

        return tuple(
            self._definitions[identity]
            for identity in sorted(self._definitions)
        )
