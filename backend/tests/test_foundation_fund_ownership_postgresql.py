"""Composition domains share governed publication and pinned historic reads."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.fund_ownership import DOMAINS
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject


@pytest.mark.parametrize('native', list(DOMAINS))
def test_each_composition_domain_replaces_windows_and_refuses_guessed_values(session, native):
    domain = DOMAINS[native]
    first = release_records(session, setup(session, [{}, {}], native, '160105.SZ'))
    current = release_records(session, setup(session, [], native, '160105.SZ'), first)
    subject = session.scalar(select(RecordSubject.id))
    req = RecordRequirement(dataset_id=domain, semantic_series_id=f'tonghuashun-{domain}-observed-v1',
                            subjects=[subject], fields=['members'])
    for release, count in [(first, 2), (current, 0)]:
        out = json.loads(service(session).query_official(req.model_copy(update={'release': release.id})))
        assert out['request_satisfied'] and len(out['items'][0]['members']) == count
    for changes in [dict(fields=['resolved_holder_identity']), dict(fields=['normalized_allocation']),
                    dict(fields=['comparable_units']), dict(time_mode='strict_public_pit')]:
        out = json.loads(service(session).query_official(req.model_copy(update=changes)))
        assert not out['request_satisfied'] and not out['items']
