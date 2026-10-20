"""Batched comparison must keep exact object, provenance and failure semantics."""
import sqlite3

import pytest

from app.data_store.errors import DataStoreError
from app.data_store.merge import value_hash
from app.data_store.verify_coverage import _CurrentComparison


@pytest.fixture
def index():
    # This reproduces the disposable verifier index, never a current data store.
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE expected(k BLOB PRIMARY KEY,r TEXT,s TEXT,g TEXT,n INTEGER,'
               'h TEXT,state TEXT,stable_order INTEGER,seen INTEGER DEFAULT 0)')
    yield db
    db.close()


def current(**changes):
    return dict(basis_group='group', basis_ns=10, basis_token='token',
                basis_state='valid', member_key='root', row_kind='root', f0_value='exact',
                **changes)


def expected(db, key, rows, *, state='valid', stable=True, order=10):
    db.execute('INSERT INTO expected(k,r,s,g,n,h,state,stable_order) VALUES (?,?,?,?,?,?,?,?)',
               (key, 'source', 'subject', 'group', order, value_hash(rows), state, int(stable)))


def comparison(db, *, disposed=lambda row: False, check=lambda **kwargs: None):
    counts = dict(current_objects=0, unexpected_objects=0, mismatched_objects=0,
                  disposed_failures=0)
    return _CurrentComparison(db, counts, disposed, check), counts


def test_batch_keeps_valid_mismatch_unexpected_and_disposed_failure_distinct(index):
    base = [current()]
    for key, state in ((b'match', 'valid'), (b'value', 'valid'), (b'group', 'valid'),
                       (b'order', 'valid'), (b'state', 'valid'), (b'bad', 'invalid'),
                       (b'disposed', 'invalid')):
        expected(index, key, base, state=state)
    checks = []
    batch, counts = comparison(index, disposed=lambda row: row['k'] == b'disposed',
                               check=lambda **kwargs: checks.append(kwargs))
    for key, changes in ((b'match', {}), (b'value', {'f0_value': 'different'}),
                         (b'group', {'basis_group': 'other'}), (b'order', {'basis_ns': 9}),
                         (b'state', {'basis_state': 'withdrawn'}), (b'bad', {}),
                         (b'disposed', {}), (b'extra', {})):
        row = {**current(), **changes}
        batch.offer(key, [row])
    batch.flush()
    assert counts == dict(current_objects=8, unexpected_objects=1,
                          mismatched_objects=5, disposed_failures=1)
    assert index.execute('SELECT count(*) FROM expected WHERE seen=1').fetchone()[0] == 7
    assert len(checks) == 9 and all(item == {'sample': True} for item in checks)


@pytest.mark.parametrize('split', [False, True])
def test_duplicate_expected_object_is_rejected_within_and_across_batches(index, split):
    rows = [current()]
    expected(index, b'key', rows)
    batch, _ = comparison(index)
    batch.offer(b'key', rows)
    if split:
        batch.flush()
    batch.offer(b'key', rows)
    with pytest.raises(DataStoreError) as caught:
        batch.flush()
    assert caught.value.code == 'KEY_ORDER_INVALID'


@pytest.mark.parametrize('order,mismatched', [(0, 1), (1, 0), (9, 0), (10, 0), (11, 1)])
def test_current_table_order_requires_positive_confirmation_no_later_than_snapshot(index, order, mismatched):
    expected(index, b'key', [current()], stable=False)
    batch, counts = comparison(index)
    batch.offer(b'key', [{**current(), 'basis_ns': order}])
    batch.flush()
    assert counts['mismatched_objects'] == mismatched


@pytest.mark.parametrize('field,value', [('basis_group', 'other'), ('basis_ns', 9),
                                        ('basis_token', 'other'), ('basis_state', 'withdrawn')])
def test_one_report_cannot_mix_confirmation_across_members(index, field, value):
    rows = [current(), {**current(), 'member_key': 'member', field: value}]
    expected(index, b'key', rows)
    batch, _ = comparison(index)
    batch.offer(b'key', rows)
    with pytest.raises(DataStoreError) as caught:
        batch.flush()
    assert caught.value.code == 'FILE_INVALID'


def test_bounded_buffer_and_cancel_before_comparison_leave_unseen_index(index):
    rows = [current()]
    for i in range(129):
        expected(index, str(i).encode(), rows)
    batch, counts = comparison(index)
    for i in range(128):
        batch.offer(str(i).encode(), rows)
    assert not batch.pending and batch.physical_rows == 0
    assert counts['current_objects'] == 128

    def cancelled(**kwargs):
        raise DataStoreError('OPERATION_CANCELLED')

    batch.check = cancelled
    batch.offer(b'128', rows)
    with pytest.raises(DataStoreError) as caught:
        batch.flush()
    assert caught.value.code == 'OPERATION_CANCELLED'
    assert index.execute('SELECT seen FROM expected WHERE k=?', (b'128',)).fetchone()[0] == 0
