"""Public stable-identity fact API.

The domain classes remain in :mod:`app.instruments.domain`; this module is a
small discoverable facade for callers that want the task-10 fact vocabulary
without importing persistence implementation details.
"""

from app.instruments.domain import (
    AuthorityStatus,
    DisplayAuthorityStatus,
    IdentityStatus,
    InstrumentCodeMapping,
    InstrumentCodeMappingFact,
    InstrumentCodeMappingProvider,
    InstrumentDisplay,
    InstrumentDisplayFact,
    InstrumentDisplayProvider,
    InstrumentIdentityFact,
    InstrumentIdentityResolution,
    InstrumentStatus,
    VersionedReference,
)
from app.instruments.identity_repository import (
    DisplayFactConflictError,
    DisplayFactVersionExistsError,
    IdentityFactConflictError,
    IdentityFactVersionExistsError,
    IdentityMergeEvidenceMissingError,
    InstrumentDisplayFactRepository,
    InstrumentIdentityFactRepository,
    InstrumentIdentityRepository,
    InstrumentIdentityService,
    DisplayResolutionRepository,
    MappingResolutionRepository,
    migrate_existing_etf_identities,
    normalize_identity_lookup_key,
    resolve_instrument_identity,
)

__all__ = [
    "AuthorityStatus",
    "DisplayAuthorityStatus",
    "IdentityStatus",
    "InstrumentCodeMapping",
    "InstrumentCodeMappingFact",
    "InstrumentCodeMappingProvider",
    "InstrumentDisplay",
    "InstrumentDisplayFact",
    "InstrumentDisplayProvider",
    "InstrumentIdentityFact",
    "InstrumentIdentityResolution",
    "InstrumentStatus",
    "VersionedReference",
    "DisplayFactConflictError",
    "DisplayFactVersionExistsError",
    "IdentityFactConflictError",
    "IdentityFactVersionExistsError",
    "IdentityMergeEvidenceMissingError",
    "InstrumentDisplayFactRepository",
    "DisplayResolutionRepository",
    "InstrumentIdentityFactRepository",
    "InstrumentIdentityRepository",
    "InstrumentIdentityService",
    "MappingResolutionRepository",
    "migrate_existing_etf_identities",
    "normalize_identity_lookup_key",
    "resolve_instrument_identity",
]
