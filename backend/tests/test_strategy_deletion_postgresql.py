"""Exercise draft deletion against real FK and immutable revision protections."""
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.strategies.models import Strategy, StrategyDraft, StrategyRevision
from app.strategies.service import StrategyStorageService, StrategyDeletionBlockedError, StrategyDraftConflictError

pytestmark = pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL")


def test_delete_draft_preserves_published_history_and_checks_editor_version():
    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            service = StrategyStorageService(session)
            draft = service.create_strategy(name="Deletion regression", source_code="def run(context, parameters):\n    return {'mode': 'hold'}\n")
            draft_id = draft.id
            with pytest.raises(StrategyDraftConflictError):
                service.delete_unpublished_strategy(draft.id, expected_version=draft.version, expected_draft_version=2)
            assert session.get(StrategyDraft, draft_id) is not None
            service.delete_unpublished_strategy(draft.id, expected_version=draft.version, expected_draft_version=1)
            assert session.get(Strategy, draft_id) is None
            assert session.get(StrategyDraft, draft_id) is None
            published = service.create_strategy(name="Preserved revision", source_code="def run(context, parameters):\n    return {'mode': 'hold'}\n")
            revision = service.publish_revision(published.id, expected_draft_version=1, alias="Immutable history")
            with pytest.raises(StrategyDeletionBlockedError):
                service.delete_unpublished_strategy(published.id, expected_version=published.version, expected_draft_version=1)
            assert session.get(StrategyRevision, revision.id) is revision
            assert session.get(Strategy, published.id) is published
            # Session close rolls back fixtures; never bypass immutable triggers.
    finally:
        engine.dispose()
