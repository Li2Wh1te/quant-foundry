"""Keep confirmed bad input/value combinations restricted after re-publication."""
from sqlalchemy import select
from app.data_foundation.work_models import Candidate


def revision_fingerprints(session, model, current_ids, restricted_ids):
    """Read provenance hashes, never provider payloads or candidate values.

    A fresh decision/official ID cannot rehabilitate the same fixed source,
    identity and values. New source evidence or genuinely corrected typed values
    remains distinguishable. The business key is included in values_hash. Only
    referenced revisions are loaded, in one query, and the no-issue path is free.
    """
    restricted = {i for i in restricted_ids if i is not None}
    if not restricted:
        return {}
    ids = restricted | {i for i in current_ids if i is not None}
    return {row[0]: tuple(row[1:]) for row in session.execute(select(
        model.id, Candidate.source_ref_id, Candidate.binding_id, model.values_hash)
        .join(Candidate, Candidate.id == model.candidate_id).where(model.id.in_(ids)))}


def matches_revision(original, current, fingerprints):
    if original is None or original == current:
        return True
    return original in fingerprints and current in fingerprints and fingerprints[original] == fingerprints[current]
