"""Public stable-identity fact API.

The domain classes remain in :mod:`app.instruments.domain`; this module is a
small discoverable facade for callers that want the task-10 fact vocabulary
without importing persistence implementation details.
"""

from app.instruments.domain import (
    AuthorityStatus,
    DisplayAuthorityStatus,
    IdentityStatus,
    InstrumentDisplayFact,
    InstrumentIdentityFact,
    InstrumentIdentityResolution,
    InstrumentStatus,
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
)

__all__ = [
    "AuthorityStatus",
    "DisplayAuthorityStatus",
    "IdentityStatus",
    "InstrumentDisplayFact",
    "InstrumentIdentityFact",
    "InstrumentIdentityResolution",
    "InstrumentStatus",
    "DisplayFactConflictError",
    "DisplayFactVersionExistsError",
    "IdentityFactConflictError",
    "IdentityFactVersionExistsError",
    "IdentityMergeEvidenceMissingError",
    "InstrumentDisplayFactRepository",
    "InstrumentIdentityFactRepository",
    "InstrumentIdentityRepository",
    "InstrumentIdentityService",
]
