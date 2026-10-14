"""E15 preparation mechanics on synthetic data and isolated PostgreSQL only.

These tests cover the ordinary issue reducer; the private frozen operator uses
a separate exact-issue path.
The existing kernel fixture rejects remote databases and uses a random schema.
"""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import os
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import text

from app.data_store.adapters.contracts import LocalInput, digest
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID
from app.data_store.catalog import Issue, SourceUpdate
from app.data_store.issue_sets import compact_entry, fingerprint, members
from app.data_store.pipeline import PipelineOptions, _scope, run_entry
from tests.test_data_store_kernel import database, limits, store
from tests.test_data_store_local_pipeline import Inputs, ready


pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1',
                                reason='isolated PostgreSQL required')
ENTRY = BY_ID['E15']
DATES = ('2021-03-31', '2021-06-30', '2021-09-30', '2021-12-31',
         '2022-03-31', '2022-06-30', '2022-09-30', '2022-12-31')
OBSERVED = datetime(2023, 1, 1, tzinfo=timezone.utc)


def financial(day, period='Q1'):
    value = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    content = {'period': period, 'item': [{
        'period_end_ms': int(value.timestamp()) * 1000,
        'report_date_ms': int(value.timestamp()) * 1000,
        'period': period, 'currency': 'CNY', 'thscode': '000999.SZ',
        'fiscal_year': value.year, 'assets_total': Decimal('12345.67'),
    }]}
    return LocalInput('tonghuashun', 'stock_balance', '000999.SZ',
                      'synthetic', OBSERVED, content, digest(content))


def unit(day, period='Q1'):
    result = tuple(normalize(ENTRY, financial(day, period)))
    assert len(result) == 1 and result[0].failure is None
    return result[0]


def materialized(candidate, state='valid'):
    return [{**row, 'representation': candidate.representation,
             'subject': candidate.subject, 'object_key': candidate.object_key,
             'basis_group': candidate.group, 'basis_ns': candidate.order,
             'basis_token': candidate.token, 'basis_state': state}
            for row in candidate.rows]


def partition(candidate):
    return ENTRY.spec.partitioner((*candidate.key, 'root'))


def problem(candidate):
    part = partition(candidate)
    target = {'scope_version': 'object-key-v1', 'partition': part,
              'prefix': list(candidate.key), 'group': candidate.group,
              'order': str(candidate.order), 'blocking': True}
    return Issue('local.' + digest(target), _scope(part), 'CORE_VALUE_INVALID',
                 digest(['issue', candidate.token]), target, {'retry': 'same_local_pipeline'})


def native_rows(st):
    result = {}
    with st.catalog.engine.connect() as connection:
        parts = connection.execute(text('SELECT DISTINCT partition_key FROM data_store_files WHERE dataset=:name'),
                                   {'name': ENTRY.spec.name}).scalars().all()
    for part in parts:
        for ref in st.catalog.files(ENTRY.spec.name, part):
            with pq.ParquetFile(st.files.root / ref['path']) as parquet:
                for row in parquet.read().to_pylist():
                    result[tuple(row[name] for name in ENTRY.spec.key)] = row
    return result


def seed(st, days=DATES, siblings=True):
    st.register(ENTRY.spec)
    candidates = []
    for index, day in enumerate(days):
        chosen = [unit(day, f'Q{index % 4 + 1}')]
        if siblings:
            chosen.append(unit(day, 'SIBLING'))
        part = partition(chosen[0])
        rows = sorted((row for candidate in chosen for row in materialized(candidate, 'invalid')),
                      key=lambda row: ENTRY.spec.key_bytes(tuple(row[name] for name in ENTRY.spec.key)))
        source = SourceUpdate(_scope(part), digest(['seed', part]), digest('seed-context'), 0,
                              qualified=False,
                              expected_generation=st.catalog.dataset(ENTRY.spec.name)['generation'])
        st.replace_partition(ENTRY.spec, part, [pa.RecordBatch.from_pylist(rows, schema=ENTRY.spec.schema)],
                             source, complete=True, source_check=lambda current: current is None,
                             issues=tuple(problem(candidate) for candidate in chosen))
        candidates.append(chosen[0])
    return candidates


def test_eight_exact_revalidations_preserve_every_sibling_row_and_issue(ready):
    chosen = seed(ready)
    with ready.catalog.engine.begin() as connection:
        connection.execute(text("UPDATE data_store_issues SET attempts=5, "
            "first_seen='2023-01-01T00:00:00Z', last_seen='2023-01-02T00:00:00Z' WHERE dataset=:name"),
            {'name': ENTRY.spec.name})
    before_rows = native_rows(ready)
    before_issues = {row['issue_key']: row for row in ready.catalog.issues(ENTRY.spec.name)}
    generation = ready.catalog.dataset(ENTRY.spec.name)['generation']
    parts = tuple(partition(candidate) for candidate in chosen)
    result = run_entry(ready, ENTRY, Inputs(*(financial(day, f'Q{i % 4 + 1}')
                                            for i, day in enumerate(DATES))),
                       options=PipelineOptions(mode='retry', partitions=parts))
    assert result['complete'] and result['resolved'] == 8
    assert ready.catalog.dataset(ENTRY.spec.name)['generation'] == generation + 8
    target_keys = {(*candidate.key, row['member_key']) for candidate in chosen for row in candidate.rows}
    after_rows = native_rows(ready)
    assert set(after_rows) == set(before_rows)
    for key, old in before_rows.items():
        assert after_rows[key] == ({**old, 'basis_state': 'valid'} if key in target_keys else old)
    remaining = {row['issue_key']: row for row in ready.catalog.issues(ENTRY.spec.name)}
    assert set(remaining) == set(before_issues) - {problem(candidate).key for candidate in chosen}
    assert all(row == before_issues[key] for key, row in remaining.items())
    assert all(row['attempts'] == 5 for row in remaining.values())
    assert not result['qualified'] and result['unresolved_issues'] == 8


def test_repeated_real_failure_retains_first_seen_and_records_a_new_attempt(ready, monkeypatch):
    from app.data_store import pipeline
    original = pipeline.normalize
    def fail(entry, source):
        for candidate in original(entry, source):
            yield replace(candidate, rows=(), failure='CORE_VALUE_INVALID')
    monkeypatch.setattr(pipeline, 'normalize', fail)
    raw = financial(DATES[0])
    options = PipelineOptions(mode='retry')
    assert not run_entry(ready, ENTRY, Inputs(raw), options=options)['qualified']
    before = ready.catalog.issues(ENTRY.spec.name)[0]
    assert not run_entry(ready, ENTRY, Inputs(raw), options=options)['qualified']
    after = ready.catalog.issues(ENTRY.spec.name)[0]
    assert after['first_seen'] == before['first_seen']
    assert after['attempts'] == before['attempts'] + 1
    assert after['last_seen'] >= before['last_seen']


def test_unobserved_compacted_issues_keep_metadata_and_scope_unqualified(ready):
    chosen = seed(ready, DATES[:1])[0]
    assert compact_entry(ready, ENTRY)['physical_records_released'] == 1
    with ready.catalog.engine.begin() as connection:
        connection.execute(text('UPDATE data_store_issues SET attempts=5 WHERE dataset=:name'),
                           {'name': ENTRY.spec.name})
    before = ready.catalog.issues(ENTRY.spec.name)
    assert len(before) == 1
    part = partition(chosen)
    result = run_entry(ready, ENTRY, Inputs(financial(DATES[0], 'UNRELATED')),
                       options=PipelineOptions(mode='retry', partitions=(part,)))
    assert result['resolved'] == 0 and not result['qualified']
    assert ready.catalog.issues(ENTRY.spec.name) == before
    assert ready.source_state(ENTRY.spec.name, _scope(part))['qualified'] is False


def test_targeted_revalidation_preserves_compacted_sibling_member_evidence(ready):
    chosen = seed(ready, DATES[:1])[0]
    assert compact_entry(ready, ENTRY)['physical_records_released'] == 1
    original = [member for row in ready.catalog.issues(ENTRY.spec.name) for member in members(row)]
    expected = [member for member in original if member['issue_key'] != problem(chosen).key]
    result = run_entry(ready, ENTRY, Inputs(financial(DATES[0])),
                       options=PipelineOptions(mode='retry', partitions=(partition(chosen),)))
    remaining = [member for row in ready.catalog.issues(ENTRY.spec.name) for member in members(row)]
    assert result['resolved'] == 1 and not result['qualified']
    assert len(remaining) == len(expected) == 1
    assert fingerprint(remaining) == fingerprint(expected)
