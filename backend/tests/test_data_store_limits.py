from dataclasses import fields

import pytest

from app.data_store.errors import DataStoreError
from app.data_store.limits import GiB, MiB, StoreLimits


@pytest.mark.parametrize('name', [f.name for f in fields(StoreLimits)])
@pytest.mark.parametrize('invalid', [0, -1, True, 1.5, None, 2**63])
def test_every_resource_has_a_finite_positive_integer_limit(name, invalid):
    with pytest.raises(DataStoreError) as error:
        StoreLimits(**{name: invalid})
    assert error.value.code == 'INVALID_CONFIGURATION'


def test_required_initial_defaults():
    limits = StoreLimits()
    assert (limits.scratch_bytes, limits.parallel_writers) == (4 * GiB, 1)
    assert (limits.log_bytes, limits.log_days) == (256 * MiB, 7)
    assert (limits.completed_run_days, limits.completed_runs_per_task) == (30, 1000)


def test_combined_scratch_and_outstanding_reservations_count():
    limits = StoreLimits()
    sample = dict(staging_bytes=GiB, spill_bytes=GiB, reserved_bytes=GiB,
                  additional_bytes=GiB, available_bytes=4 * GiB, garbage_bytes=0)
    limits.check_write(**sample)
    for name, expected in [('additional_bytes', 'SCRATCH_BUDGET_EXCEEDED'),
                           ('spill_bytes', 'SCRATCH_BUDGET_EXCEEDED')]:
        changed = {**sample, name: sample[name] + 1}
        with pytest.raises(DataStoreError) as error:
            limits.check_write(**changed)
        assert error.value.code == expected
    with pytest.raises(DataStoreError) as error:
        limits.check_write(**{**sample, 'available_bytes': 3 * GiB - 1})
    assert error.value.code == 'DISK_PRESSURE'
    with pytest.raises(DataStoreError) as error:
        limits.check_write(**{**sample, 'garbage_bytes': limits.garbage_bytes})
    assert error.value.code == 'GARBAGE_BUDGET_EXCEEDED'


def test_invalid_measured_sample_fails_closed():
    with pytest.raises(DataStoreError):
        StoreLimits().check_write(staging_bytes=-1, spill_bytes=0, reserved_bytes=0,
                                  additional_bytes=0, available_bytes=GiB, garbage_bytes=0)


@pytest.mark.parametrize('options', [{'batch_bytes': 5 * GiB}, {'query_bytes': GiB}])
def test_batch_and_query_cannot_exceed_their_parent_budget(options):
    with pytest.raises(DataStoreError):
        StoreLimits(**options)
