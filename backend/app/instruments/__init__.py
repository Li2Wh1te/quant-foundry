"""Stable instrument identity, PIT code mappings, and trading specs."""

from app.instruments.domain import (
    AuthorityStatus,
    CorporateActionRequirement,
    DisplayAuthorityStatus,
    IdentityStatus,
    InstrumentCapabilities,
    InstrumentCodeMapping,
    InstrumentCodeMappingFact,
    InstrumentCodeMappingProvider,
    InstrumentDisplay,
    InstrumentDisplayFact,
    InstrumentDisplayProvider,
    InstrumentIdentityFact,
    InstrumentIdentityResolution,
    InstrumentLifecycleState,
    InstrumentIdentityStatus,
    InstrumentStatus,
    InstrumentSpec,
    InstrumentSpecProvider as InstrumentSpecProviderProtocol,
    MappingConflictError,
    MappingCoverageGapError,
    VersionedReference,
    order_mapping_segments,
)

__all__ = [
    "AuthorityStatus",
    "CorporateActionRequirement",
    "DisplayAuthorityStatus",
    "IdentityStatus",
    "InstrumentCapabilities",
    "InstrumentCodeMapping",
    "InstrumentCodeMappingFact",
    "InstrumentCodeMappingProvider",
    "InstrumentDisplay",
    "InstrumentDisplayFact",
    "InstrumentDisplayProvider",
    "InstrumentIdentityFact",
    "InstrumentIdentityResolution",
    "InstrumentLifecycleState",
    "InstrumentIdentityStatus",
    "InstrumentStatus",
    "InstrumentSpec",
    "InstrumentSpecProvider",
    "InstrumentSpecProviderProtocol",
    "MappingConflictError",
    "MappingCoverageGapError",
    "VersionedReference",
    "order_mapping_segments",
    "DisplayFactConflictError",
    "DisplayFactVersionExistsError",
    "DisplayResolutionRepository",
    "IdentityFactConflictError",
    "IdentityFactVersionExistsError",
    "IdentityMergeEvidenceMissingError",
    "InstrumentDisplayFactRepository",
    "InstrumentIdentityFactRepository",
    "InstrumentIdentityRepository",
    "InstrumentIdentityService",
    "MappingResolutionRepository",
    "normalize_identity_lookup_key",
    "DEFAULT_MAPPING_SOURCE",
    "DEFAULT_RULE_PACKAGE_REFERENCE",
    "DefaultInstrumentSpecProvider",
    "InstrumentEligibility",
    "InstrumentQualificationIssue",
    "InstrumentSpecOrchestrator",
    "InstrumentSpecResolver",
    "InstrumentSpecQualification",
    "QualificationIssue",
    "SingleInstrumentQualification",
    "SingleInstrumentQualificationProvider",
    "resolve_instrument_qualification",
]

# Import persistence adapters only when a caller explicitly requests them.
# Loading app.instruments.domain from the deterministic kernel must not import
# SQLAlchemy, ingestion tables, or application database configuration.
_LAZY_EXPORTS = {'DisplayFactConflictError': ('app.instruments.identity_repository', 'DisplayFactConflictError'), 'DisplayFactVersionExistsError': ('app.instruments.identity_repository', 'DisplayFactVersionExistsError'), 'DisplayResolutionRepository': ('app.instruments.identity_repository', 'DisplayResolutionRepository'), 'IdentityFactConflictError': ('app.instruments.identity_repository', 'IdentityFactConflictError'), 'IdentityFactVersionExistsError': ('app.instruments.identity_repository', 'IdentityFactVersionExistsError'), 'IdentityMergeEvidenceMissingError': ('app.instruments.identity_repository', 'IdentityMergeEvidenceMissingError'), 'InstrumentDisplayFactRepository': ('app.instruments.identity_repository', 'InstrumentDisplayFactRepository'), 'InstrumentIdentityFactRepository': ('app.instruments.identity_repository', 'InstrumentIdentityFactRepository'), 'InstrumentIdentityRepository': ('app.instruments.identity_repository', 'InstrumentIdentityRepository'), 'InstrumentIdentityService': ('app.instruments.identity_repository', 'InstrumentIdentityService'), 'MappingResolutionRepository': ('app.instruments.identity_repository', 'MappingResolutionRepository'), 'normalize_identity_lookup_key': ('app.instruments.identity_repository', 'normalize_identity_lookup_key'), 'DEFAULT_MAPPING_SOURCE': ('app.instruments.spec_provider', 'DEFAULT_MAPPING_SOURCE'), 'DEFAULT_RULE_PACKAGE_REFERENCE': ('app.instruments.spec_provider', 'DEFAULT_RULE_PACKAGE_REFERENCE'), 'DefaultInstrumentSpecProvider': ('app.instruments.spec_provider', 'DefaultInstrumentSpecProvider'), 'InstrumentEligibility': ('app.instruments.spec_provider', 'InstrumentEligibility'), 'InstrumentQualificationIssue': ('app.instruments.spec_provider', 'InstrumentQualificationIssue'), 'InstrumentSpecOrchestrator': ('app.instruments.spec_provider', 'InstrumentSpecOrchestrator'), 'InstrumentSpecProvider': ('app.instruments.spec_provider', 'InstrumentSpecProvider'), 'InstrumentSpecResolver': ('app.instruments.spec_provider', 'InstrumentSpecResolver'), 'InstrumentSpecQualification': ('app.instruments.spec_provider', 'InstrumentSpecQualification'), 'QualificationIssue': ('app.instruments.spec_provider', 'QualificationIssue'), 'SingleInstrumentQualification': ('app.instruments.spec_provider', 'SingleInstrumentQualification'), 'SingleInstrumentQualificationProvider': ('app.instruments.spec_provider', 'SingleInstrumentQualificationProvider'), 'resolve_instrument_qualification': ('app.instruments.spec_provider', 'resolve_instrument_qualification')}


def __getattr__(name):
    from importlib import import_module
    if name not in _LAZY_EXPORTS:
        raise AttributeError(name)
    module_name, attribute = _LAZY_EXPORTS[name]
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
