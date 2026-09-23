"""Every empty-at-capture table has a strict future-row contract."""
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy import Boolean, Date, DateTime, Integer, JSON, Numeric, String, Text, Uuid
from app.data_foundation.table_intake import TABLE_SOURCES
from app.data_foundation.local_table_contracts import DOMAINS
from app.data_foundation.record_adapters import rows_for, convert
from app.data_foundation.canonical import FoundationError


def sample(spec):
    row={}
    for column in spec.model.__table__.columns:
        category=column.type
        if isinstance(category, JSON):value={}
        elif isinstance(category, DateTime):value=datetime(2026,1,2,tzinfo=timezone.utc).isoformat()
        elif isinstance(category, Date):value='2026-01-02'
        elif isinstance(category, Boolean):value=True
        elif isinstance(category, Uuid):value=str(uuid4())
        elif isinstance(category, (Numeric,Integer)):value='1.230000' if isinstance(category,Numeric) else 1
        elif isinstance(category, (String,Text)):value='reported-text'
        else:raise AssertionError(column.type)
        row[column.name]=value
    if spec.source_column:row[spec.source_column]='tushare'
    return row


@pytest.mark.parametrize('native',tuple(DOMAINS))
def test_all_audit_fact_and_coverage_rows_have_typed_evidence(native):
    spec=next(item for item in TABLE_SOURCES if item.dataset==native)
    source=SimpleNamespace(source='tushare',dataset=native,representation='local_table_baseline')
    row=sample(spec)
    parsed=convert(source,rows_for(source,[row])[0])
    assert parsed['dataset']==DOMAINS[native]
    assert parsed['body']['native_dataset']==native
    assert parsed['body']['verified_public_time'] is None
    assert parsed['body']['verified_market_completeness'] is None
    assert len(parsed['body']['source_row_hash'])==64
    assert all(value in {'1.23', '1'} for value in parsed['body']['reported_numbers'].values())
    invalid=dict(row, unregistered='not admitted')
    with pytest.raises(FoundationError):convert(source,invalid)
    if spec.source_column:
        with pytest.raises(FoundationError):convert(source,dict(row,**{spec.source_column:'other'}))
