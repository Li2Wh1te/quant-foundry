"""Legacy direct responses are evidence; storage differences are not receipts."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.data_store.adapters.contracts import digest, native_json, instant_ns
from app.data_store.local_sources import (
    EffectiveBasis, materialize_observation, direct_nav_response,
)
from app.data_store.adapters.canonical import NativeInputError


WHEN = datetime(2026, 1, 2, tzinfo=timezone.utc)


def observation(data, when=WHEN, *, previous=None, scope='fyear'):
    from app.data_ingestion.tonghuashun.repository import archive_data
    from types import SimpleNamespace
    requests = [{'interface': 'fund.performance.nav', 'request_id': 'synthetic-trace',
                 'parameters': {'thscode': '001473.OF', 'range': scope, 'nav_type': 'unit,adj'}}]
    body, base_id, depth = archive_data('fund_nav', data,
        SimpleNamespace(id=previous['id'], chain_depth=previous['chain_depth']) if previous else None,
        materialize_observation(previous, lambda _: None)[0] if previous else None)
    return {'id': str(uuid4()), 'dataset': 'fund_nav', 'subject': '001473.OF', 'variant': 'default',
            'observed_at': when, 'content_hash': digest(data), 'request_json': native_json(requests),
            'data_json': body, 'base_observation_id': base_id, 'chain_depth': depth,
            'row_count': len(data['item'])}, requests


def response():
    return {'coverage': 'provider_rolling_window', 'observed_start': '2025-01-02',
            'observed_end': '2025-02-02', 'item': [
                {'nav_date': 1735747200000 + offset * 86400000,
                 'unit_nav': Decimal('1.23'), 'adj_nav': Decimal('2.34')}
                for offset in range(32)]}


def test_direct_full_response_reconfirms_equal_points_through_real_delta_compression():
    data = response()
    first, requests = observation(data)
    second, later_requests = observation(data, WHEN + timedelta(days=1), previous=first)
    assert second['base_observation_id'] == first['id']
    assert 'returned_keys' not in later_requests[0]
    tracker = EffectiveBasis()
    body, basis, field = materialize_observation(first, lambda _: None)
    basis, uncertain = tracker.apply(first, body, basis, field, requests, native=True)
    assert not uncertain
    body, basis, field = materialize_observation(second, {first['id']: first}.get)
    assert digest(body) == second['content_hash']
    basis, uncertain = tracker.apply(second, body, basis, field, later_requests, native=True)
    assert not uncertain and all(value[0] == instant_ns(second['observed_at']) for value in basis.values())


@pytest.mark.parametrize('problem', ['rescue', 'month', 'import', 'subject', 'variant',
                                     'interface', 'category', 'window', 'count', 'future', 'duplicate'])
def test_equivalence_rejects_unreviewed_or_inconsistent_provenance(problem):
    data = response()
    row, requests = observation(data)
    native = True
    if problem == 'rescue': native = False
    elif problem == 'month': requests[0]['parameters']['range'] = 'month'
    elif problem == 'import': requests[0]['artifact_sha256'] = 'a' * 64
    elif problem == 'subject': requests[0]['parameters']['thscode'] = 'OTHER.OF'
    elif problem == 'variant': row['variant'] = 'other'
    elif problem == 'interface': requests[0]['interface'] = 'fund.performance.other'
    elif problem == 'category': requests[0]['parameters']['nav_type'] = 'unit'
    elif problem == 'window': data['observed_start'] = '2020-01-01'
    elif problem == 'count': row['row_count'] += 1
    elif problem == 'future': row['observed_at'] = datetime(2024, 1, 2, tzinfo=timezone.utc)
    elif problem == 'duplicate': data['item'][1] = deepcopy(data['item'][0])
    assert direct_nav_response(row, data, requests, native=native) is False


def test_dependency_corruption_is_rejected_before_equivalent_confirmation():
    data = response()
    first, _ = observation(data)
    second, _ = observation(data, WHEN + timedelta(days=1), previous=first)
    first['content_hash'] = '0' * 64
    with pytest.raises(NativeInputError):
        materialize_observation(second, {first['id']: first}.get)
