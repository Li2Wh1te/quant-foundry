"""Durable boundaries must survive a stopped run without publishing partial facts."""
from datetime import date
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_tonghuashun_collections import engine, seed, ticker, reply, NOW
from app.data_ingestion.models.tonghuashun import TonghuashunWorkUnit as Work
from app.data_ingestion.tonghuashun.contracts import CollectionParameters, date_ms
from app.data_ingestion.tonghuashun.control import CollectionControl, CollectionYield, active_control
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun.service import collect


def test_report_budget_resumes_saved_requests_and_publishes_only_complete_scope(engine):
    Work.__table__.create(engine)
    seed(engine, [ticker()])
    reports = [{'end_date_ms': date_ms(date(2025, month, 28)), 'report_type': 'quarter'} for month in (3, 6, 9)]
    def response(interface, params):
        return reply(reports if interface.endswith('report-dates') else [{'holding': params['end_date']}])
    client = Mock(interval_ms=0, request=Mock(side_effect=response))
    params = CollectionParameters()
    monitor = CollectionControl(engine, None, max_requests=2)
    token = active_control.set(monitor)
    try:
        with pytest.raises(CollectionYield):
            collect('fund_stock_history', params, client, engine, now=NOW)
    finally:
        active_control.reset(token)
    with Session(engine) as session:
        assert CollectionRepository(session).read('fund_stock_history', '510300.SH', 'default').data is None
        assert session.scalar(select(func.count()).select_from(Work)) == 2
    monitor = CollectionControl(engine, None, max_requests=2)
    token = active_control.set(monitor)
    try:
        result = collect('fund_stock_history', params, client, engine, now=NOW)
    finally:
        active_control.reset(token)
    assert result['succeeded'] == 1
    assert client.request.call_count == 4  # Directory + three reports, no repeated I/O.
    assert monitor.detail['reused'] == 2
    with Session(engine) as session:
        data = CollectionRepository(session).read('fund_stock_history', '510300.SH', 'default').data
        assert len(data['item']) == 3
        assert session.scalar(select(func.count()).select_from(Work)) == 0
