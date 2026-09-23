"""Operational receipts are published and readable without market claims."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_import_progress import payload
from app.data_foundation.import_progress import CHANNELS
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.record_query import RecordRequirement


@pytest.mark.parametrize('native',CHANNELS)
def test_pending_control_receipt_publishes_without_claiming_business_completion(session,native):
    raw=payload();raw.pop('item')
    release=release_records(session,setup(session,[],native,'market',**raw))
    subject=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key==native+':market'))
    request=RecordRequirement(dataset_id='operations.import_progress',
        semantic_series_id='tonghuashun-operations.import_progress-observed-v1',
        subjects=[subject],fields=['pending_requests','imported_subjects'],release=release.id)
    result=json.loads(service(session).query_official(request))
    assert result['request_satisfied'] and result['items'][0]['imported_subjects']==4
    assert result['items'][0]['pending_requests'][0]['pending']==6
    denied=json.loads(service(session).query_official(request.model_copy(update={'fields':['business_publication_complete']})))
    assert not denied['request_satisfied'] and not denied['items']
