"""Each statement domain uses immutable releases and rejects inferred finances."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.financial_windows import DOMAINS, NUMBERS
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject


@pytest.mark.parametrize('native', list(DOMAINS))
def test_statement_release_replacement_and_unsupported_semantics(session, native):
    domain = DOMAINS[native]
    first = release_records(session, setup(session, [{NUMBERS[native][0]: '-1.25'}], native, '000001.SZ'))
    current = release_records(session, setup(session, [], native, '000001.SZ'), first)
    subject = session.scalar(select(RecordSubject.id))
    req = RecordRequirement(dataset_id=domain, semantic_series_id=f'tonghuashun-{domain}-observed-v1',
                            subjects=[subject], fields=['reports'])
    for release, count in [(first, 1), (current, 0)]:
        out = json.loads(service(session).query_official(req.model_copy(update={'release': release.id})))
        assert out['request_satisfied'] and len(out['items'][0]['reports']) == count
    for changes in [dict(fields=['single_period_values']), dict(fields=['as_filed_history']),
                    dict(fields=['accounting_basis']), dict(time_mode='strict_public_pit')]:
        out = json.loads(service(session).query_official(req.model_copy(update=changes)))
        assert not out['request_satisfied'] and not out['items']
