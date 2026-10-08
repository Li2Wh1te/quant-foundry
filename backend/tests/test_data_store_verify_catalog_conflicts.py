"""Coverage of actual retained directory conflicts, without resolving identity."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import text

from app.data_store.adapters.contracts import digest
from app.data_store.coverage import check_coverage
from app.data_store.catalog_membership import CatalogMemberships
from app.data_store.local_sources import NativeSources
from app.data_store.pipeline import read_entry_status, run_entry
from app.data_store.verify_coverage import verify_existing
from tests.test_data_store_kernel import database, limits, store
from tests.test_data_store_domain_samples import ready
from tests.test_data_store_local_pipeline import Inputs
from tests.test_data_store_catalog_membership import (
    ENTRY, directory, native_append, native_reset, target_rows,
)
from tests.test_data_store_verify_existing import files


UNKNOWN = '180307.SZ'
ETF_CODES = ('158022.SZ', '158036.SZ', '158038.SZ', '158041.SZ', '158050.SZ',
             '158061.SZ', '158072.SZ', '515940.SH', '516040.SH', '561610.SH',
             '562110.SH', '589510.SH')
OTC_CODES = ('158030.SZ', '158039.SZ', '158046.SZ', '158059.SZ')


def assert_restricted(ready):
    with ready.catalog.transaction() as connection:
        judgment = check_coverage(connection, ENTRY, read_entry_status(ready, ENTRY.id))
    assert not judgment['satisfied']
    assert judgment['reason'] == 'CURRENT_QUALITY_UNRESOLVED'


def unresolved_inputs():
    return Inputs(directory('fund-otc', [UNKNOWN], 1, version=90)[0],
                  directory('fund-reits', [UNKNOWN], 2, version=95)[0],
                  directory('fund-reits', [UNKNOWN], 3, version=96)[0])


@pytest.mark.parametrize('later_group', ['fund-reits', 'fund-otc'])
def test_same_group_refresh_does_not_erase_another_group_conflict(ready, later_group):
    old, _ = directory('fund-otc', [UNKNOWN], 1, version=90)
    other, _ = directory('fund-reits', [UNKNOWN], 2, version=95)
    later, _ = directory(later_group, [UNKNOWN], 3, version=96)
    inputs = Inputs(old, other, later)
    built = run_entry(ready, ENTRY, inputs)
    assert built['complete'] and not built['qualified']
    winner = later if later_group == old.subject else old
    current = target_rows(ready, old, UNKNOWN)
    assert len(current) == 1
    assert (current[0]['basis_group'], int(current[0]['basis_ns']), current[0]['basis_token']) == (
        winner.group, winner.order_ns, winner.token)
    before_files = files(ready, ENTRY)
    before_dataset = ready.catalog.dataset(ENTRY.spec.name)
    before_issues = ready.catalog.issues(ENTRY.spec.name)

    # These are real MergeSpool commits and a real file/native comparison. A
    # newer assertion in either original group cannot order the two groups or
    # manufacture a successor. Coverage only accounts for the retained issue.
    verified = verify_existing(ready, ENTRY, inputs)
    assert verified['complete'] and verified['disposed_failures'] == 1
    assert verified['mismatched_objects'] == 0
    assert ready.catalog.dataset(ENTRY.spec.name) == before_dataset
    assert ready.catalog.issues(ENTRY.spec.name) == before_issues
    assert files(ready, ENTRY) == before_files
    assert not ready.budget.pending_keys()
    assert_restricted(ready)


@pytest.mark.parametrize('defect', ['no_issue', 'other_group', 'older_order', 'other_token',
                                  'other_reason', 'other_key', 'scope_only'])
def test_conflict_requires_actual_exact_object_group_order_and_token_disposition(ready, defect):
    inputs = unresolved_inputs()
    assert not run_entry(ready, ENTRY, inputs)['qualified']
    with ready.catalog.engine.begin() as connection:
        issue = connection.execute(text('SELECT * FROM data_store_issues WHERE dataset=:d'),
                                   {'d': ENTRY.spec.name}).mappings().one()
        target = json.loads(issue['target_json'])
        token, reason = issue['evidence_token'], issue['reason']
        if defect == 'no_issue':
            connection.execute(text('DELETE FROM data_store_issues WHERE dataset=:d'), {'d': ENTRY.spec.name})
        else:
            if defect == 'other_group':target['group'] = '0' * 64
            elif defect == 'older_order':target['order'] = str(int(target['order']) - 1)
            elif defect == 'other_token':token = '0' * 64
            elif defect == 'other_reason':reason = 'SOURCE_REFRESH_FAILED'
            elif defect == 'other_key':target['prefix'][1] = 'OTHER-FICTION.SZ'
            else:target['prefix'] = target['prefix'][:2]
            connection.execute(text('UPDATE data_store_issues SET target_json=:j,evidence_token=:t,reason=:r '
                                    'WHERE dataset=:d'),
                               {'j': json.dumps(target), 't': token, 'r': reason, 'd': ENTRY.spec.name})
    before_files = files(ready, ENTRY)
    before_dataset = ready.catalog.dataset(ENTRY.spec.name)
    before_issues = ready.catalog.issues(ENTRY.spec.name)

    verified = verify_existing(ready, ENTRY, inputs)
    assert not verified['complete'] and verified['mismatched_objects'] == 1
    assert verified['disposed_failures'] == 0
    assert files(ready, ENTRY) == before_files
    assert ready.catalog.dataset(ENTRY.spec.name) == before_dataset
    assert ready.catalog.issues(ENTRY.spec.name) == before_issues
    assert read_entry_status(ready, ENTRY.id)['coverage_pending']
    assert not ready.budget.pending_keys()


def test_unprocessed_new_competing_claim_cannot_borrow_an_older_issue(ready):
    old = directory('fund-otc', [UNKNOWN], 1, version=90)[0]
    other = directory('fund-reits', [UNKNOWN], 2, version=95)[0]
    assert not run_entry(ready, ENTRY, Inputs(old, other))['qualified']
    later = directory('fund-reits', [UNKNOWN], 3, version=96)[0]
    verified = verify_existing(ready, ENTRY, Inputs(old, other, later))
    assert not verified['complete'] and verified['mismatched_objects'] == 1
    assert verified['disposed_failures'] == 0
    assert not ready.budget.pending_keys()


@pytest.mark.parametrize('unprocessed_first', [True, False])
def test_each_competing_group_requires_its_own_actual_disposition(ready, unprocessed_first):
    old = directory('fund-otc', [UNKNOWN], 1, version=90)[0]
    unprocessed = directory('fund-etf', [UNKNOWN], 2, version=94)[0]
    disposed = directory('fund-reits', [UNKNOWN], 3, version=95)[0]
    # Only OTC and REIT have actually passed through MergeSpool. The ETF
    # assertion is new native input, so a genuine REIT issue cannot account
    # for it regardless of the order of the complete verification scan.
    built = run_entry(ready, ENTRY, Inputs(old, disposed))
    assert built['complete'] and not built['qualified']
    before_files = files(ready, ENTRY)
    before_dataset = ready.catalog.dataset(ENTRY.spec.name)
    before_issues = ready.catalog.issues(ENTRY.spec.name)
    assert len(before_issues) == 1
    assert before_issues[0]['evidence_token'] == disposed.token
    candidates = (unprocessed, disposed) if unprocessed_first else (disposed, unprocessed)

    verified = verify_existing(ready, ENTRY, Inputs(old, *candidates))

    assert not verified['complete'] and verified['mismatched_objects'] == 1
    assert verified['disposed_failures'] == 0
    current = target_rows(ready, old, UNKNOWN)
    assert len(current) == 1
    assert (current[0]['basis_group'], int(current[0]['basis_ns']), current[0]['basis_token']) == (
        old.group, old.order_ns, old.token)
    assert files(ready, ENTRY) == before_files
    assert ready.catalog.dataset(ENTRY.spec.name) == before_dataset
    assert ready.catalog.issues(ENTRY.spec.name) == before_issues
    assert read_entry_status(ready, ENTRY.id)['coverage_pending']
    assert not ready.budget.pending_keys()
    with ready.catalog.transaction() as connection:
        judgment = check_coverage(connection, ENTRY, read_entry_status(ready, ENTRY.id))
    assert not judgment['satisfied'] and judgment['reason'] == 'CURRENT_INPUT_PENDING'


def test_membership_proof_for_two_groups_does_not_clear_a_third_group(ready):
    old = directory('fund-otc', [UNKNOWN], 1, version=90)[0]
    unprocessed = directory('fund-etf', [UNKNOWN], 2, version=94)[0]
    prior = directory('fund-reits', [UNKNOWN], 3, version=95)[0]
    positive = directory('fund-reits', [UNKNOWN], 4)
    negative = directory('fund-otc', ['OTHER.SH'], 5)
    context = CatalogMemberships([positive, negative])
    positive_raw = replace(positive[0], catalog_memberships=context)
    negative_raw = replace(negative[0], catalog_memberships=context)
    # The actual current row has a complete OTC-to-REIT transition. The same
    # provider snapshot supplies no ETF directory and therefore no authority
    # to erase an unrelated competing ETF assertion from the native scan.
    built = run_entry(ready, ENTRY, Inputs(old, prior, positive_raw, negative_raw))
    assert built['complete'] and built['qualified']
    before_files = files(ready, ENTRY)
    before_dataset = ready.catalog.dataset(ENTRY.spec.name)
    assert not ready.catalog.issues(ENTRY.spec.name)

    verified = verify_existing(ready, ENTRY,
        Inputs(old, unprocessed, prior, positive_raw, negative_raw))

    assert not verified['complete'] and verified['mismatched_objects'] == 1
    assert verified['disposed_failures'] == 0
    current = target_rows(ready, old, UNKNOWN)
    assert len(current) == 1 and current[0]['basis_group'] == positive_raw.group
    assert files(ready, ENTRY) == before_files
    assert ready.catalog.dataset(ENTRY.spec.name) == before_dataset
    assert not ready.catalog.issues(ENTRY.spec.name)
    assert read_entry_status(ready, ENTRY.id)['coverage_pending']
    assert not ready.budget.pending_keys()


def test_complete_memberships_cover_each_of_three_competing_groups(ready):
    old = directory('fund-otc', [UNKNOWN], 1, version=90)[0]
    etf = directory('fund-etf', [UNKNOWN], 2, version=94)[0]
    reit = directory('fund-reits', [UNKNOWN], 3, version=95)[0]
    positive = directory('fund-reits', [UNKNOWN], 4)
    negative_otc = directory('fund-otc', ['OTHER.SH'], 5)
    negative_etf = directory('fund-etf', ['OTHER-ETF.SH'], 6)
    context = CatalogMemberships([positive, negative_otc, negative_etf])
    # Each original competing category has its own complete absence at the
    # same provider snapshot as the real positive REIT head. This is genuine
    # per-group authority, rather than borrowing another category's issue.
    inputs = Inputs(old, etf, reit, *[
        replace(pair[0], catalog_memberships=context)
        for pair in (positive, negative_otc, negative_etf)])
    built = run_entry(ready, ENTRY, inputs)
    assert built['complete'] and built['qualified']
    assert not ready.catalog.issues(ENTRY.spec.name)
    before_files = files(ready, ENTRY)
    before_dataset = ready.catalog.dataset(ENTRY.spec.name)
    verified = verify_existing(ready, ENTRY, inputs)
    assert verified['complete'] and verified['disposed_failures'] == 0
    assert verified['missing_objects'] == verified['mismatched_objects'] == verified['unexpected_objects'] == 0
    current = target_rows(ready, old, UNKNOWN)
    assert len(current) == 1 and current[0]['basis_group'] == positive[0].group
    assert files(ready, ENTRY) == before_files
    assert ready.catalog.dataset(ENTRY.spec.name) == before_dataset
    assert not ready.catalog.issues(ENTRY.spec.name)
    assert not ready.budget.pending_keys()


@pytest.mark.parametrize('defect', ['newer_value', 'other_basis_token'])
def test_an_issue_does_not_hide_a_changed_retained_winner(ready, defect):
    expected = unresolved_inputs()
    old, other, later = expected.rows
    if defect == 'newer_value':
        changed = directory('fund-otc', [UNKNOWN], 4, version=99)[0]
        changed = replace(changed, content={**changed.content, 'item': [
            {**changed.content['item'][0], 'name': 'A different retained name'}]},
            token=digest(['changed retained name', changed.token]))
    else:
        changed = replace(old, token='f' * 64)
    assert not run_entry(ready, ENTRY, Inputs(changed, other, later))['qualified']
    verified = verify_existing(ready, ENTRY, expected)
    assert not verified['complete'] and verified['mismatched_objects'] == 1
    assert verified['disposed_failures'] == 0
    assert not ready.budget.pending_keys()


@pytest.mark.parametrize('defect', ['different_version', 'both_absent', 'late_claim'])
def test_a_retained_conflict_is_not_resolved_by_an_inapplicable_membership_proof(ready, defect):
    old = directory('fund-otc', [UNKNOWN], 1, version=90)[0]
    positive = directory('fund-reits', [UNKNOWN], 3)
    negative = directory('fund-otc', ['OTHER.SH'], 4)
    if defect == 'different_version':negative = directory('fund-otc', ['OTHER.SH'], 4, version=101)
    elif defect == 'both_absent':positive = directory('fund-reits', ['ELSE.SH'], 3)
    else:old = directory('fund-otc', [UNKNOWN], 5, version=101)[0]
    context = CatalogMemberships([positive, negative])
    sources = [replace(raw, catalog_memberships=context) for raw in
               (old, directory('fund-reits', [UNKNOWN], 2, version=95)[0], positive[0], negative[0])]
    inputs = Inputs(*sources)
    built = run_entry(ready, ENTRY, inputs)
    assert built['complete'] and not built['qualified']
    verified = verify_existing(ready, ENTRY, inputs)
    assert verified['complete'] and verified['disposed_failures'] == 1
    assert verified['mismatched_objects'] == 0
    assert_restricted(ready)


def test_real_complete_membership_proof_can_resolve_a_prior_competing_claim(ready):
    old = directory('fund-otc', [UNKNOWN], 1, version=90)[0]
    prior = directory('fund-reits', [UNKNOWN], 2, version=95)[0]
    positive = directory('fund-reits', [UNKNOWN], 3)
    negative = directory('fund-otc', ['OTHER.SH'], 4)
    context = CatalogMemberships([positive, negative])
    # A real full membership context is the only new authority in this track.
    # Mere repetition without it is exercised by the unresolved refresh tests.
    inputs = Inputs(old, prior, replace(positive[0], catalog_memberships=context),
                    replace(negative[0], catalog_memberships=context))
    built = run_entry(ready, ENTRY, inputs)
    assert built['complete'] and built['qualified']
    assert not ready.catalog.issues(ENTRY.spec.name)
    verified = verify_existing(ready, ENTRY, inputs)
    assert verified['complete'] and verified['disposed_failures'] == 0
    winner = target_rows(ready, old, UNKNOWN)[0]
    assert (winner['basis_group'], int(winner['basis_ns']), winner['basis_token']) == (
        positive[0].group, positive[0].order_ns, positive[0].token)
    assert not ready.budget.pending_keys()


def test_original_sixteen_native_transitions_and_unknown_share_one_complete_verification(ready):
    native_reset(ready)
    observations = [directory('fund-etf', OTC_CODES, 0, version=90),
                    directory('fund-otc', (*ETF_CODES, UNKNOWN), 1, version=90),
                    directory('fund-reits', [UNKNOWN], 2, version=95),
                    directory('fund-reits', [UNKNOWN], 3, version=96),
                    directory('fund-etf', ETF_CODES, 4),
                    directory('fund-otc', OTC_CODES, 5),
                    directory('fund-reits', ['ELSE.SH'], 6)]
    for raw, requests in observations:native_append(ready, raw, requests)
    with ready.catalog.engine.begin() as connection:
        connection.execute(text("""INSERT INTO tonghuashun_collection_states
            (dataset,status,subject,variant,attempted_at,error_kind)
            VALUES ('tickers','failed',:subject,'default',:at,'invalid_data')"""),
            [{'subject': category, 'at': directory(category, [], 7)[0].observed_at}
             for category in ('fund-etf', 'fund-otc')])
    built = run_entry(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert built['complete'] and not built['qualified'] and built['unresolved_issues'] == 3
    actual = list(NativeSources(ready.catalog.engine).iter_entry(ENTRY))
    for category, codes in [('fund-etf', ETF_CODES), ('fund-otc', OTC_CODES)]:
        source = next(raw for raw in actual if raw.subject == category and
                      not raw.failure and raw.content['timestamp'] == 100)
        for code in codes:
            row = target_rows(ready, source, code)[0]
            assert row['f0_asset_type'] == category
            assert (row['basis_group'], int(row['basis_ns']), row['basis_token']) == (
                source.group, source.order_ns, source.token)
    unknown = target_rows(ready, observations[1][0], UNKNOWN)[0]
    assert unknown['f0_asset_type'] == 'fund-otc'
    before_files = files(ready, ENTRY)
    before_dataset = ready.catalog.dataset(ENTRY.spec.name)
    before_issues = ready.catalog.issues(ENTRY.spec.name)
    verified = verify_existing(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert verified['complete'] and verified['disposed_failures'] == 3
    assert verified['missing_objects'] == verified['mismatched_objects'] == verified['unexpected_objects'] == 0
    assert files(ready, ENTRY) == before_files
    assert ready.catalog.dataset(ENTRY.spec.name) == before_dataset
    assert ready.catalog.issues(ENTRY.spec.name) == before_issues
    assert_restricted(ready)
    assert not ready.budget.pending_keys()


def test_native_otc_reit_reit_and_both_head_absences_remain_unknown(ready):
    native_reset(ready)
    observations = [directory('fund-otc', [UNKNOWN], 1, version=90),
                    directory('fund-reits', [UNKNOWN], 2, version=95),
                    directory('fund-reits', [UNKNOWN], 3, version=96),
                    directory('fund-otc', ['OTHER.SH'], 4),
                    directory('fund-reits', ['ELSE.SH'], 5)]
    for raw, requests in observations:
        native_append(ready, raw, requests)
    built = run_entry(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert built['complete'] and not built['qualified']
    current = target_rows(ready, observations[0][0], UNKNOWN)
    actual = list(NativeSources(ready.catalog.engine).iter_entry(ENTRY))
    old = next(raw for raw in actual if raw.subject == 'fund-otc' and raw.content['timestamp'] == 90)
    conflicting = next(raw for raw in actual if raw.subject == 'fund-reits' and raw.content['timestamp'] == 96)
    issue = ready.catalog.issues(ENTRY.spec.name)
    assert len(issue) == 1 and issue[0]['reason'] == 'SOURCE_ORDER_UNCOMPARABLE'
    assert issue[0]['evidence_token'] == conflicting.token
    assert (current[0]['basis_group'], int(current[0]['basis_ns']), current[0]['basis_token']) == (
        old.group, old.order_ns, old.token)
    before_files = files(ready, ENTRY)
    before_dataset = ready.catalog.dataset(ENTRY.spec.name)

    verified = verify_existing(ready, ENTRY, NativeSources(ready.catalog.engine))
    assert verified['complete'] and verified['disposed_failures'] == 1
    assert verified['missing_objects'] == verified['mismatched_objects'] == verified['unexpected_objects'] == 0
    assert ready.catalog.dataset(ENTRY.spec.name) == before_dataset
    assert ready.catalog.issues(ENTRY.spec.name) == issue
    assert files(ready, ENTRY) == before_files
    assert not ready.budget.pending_keys()
    assert_restricted(ready)
