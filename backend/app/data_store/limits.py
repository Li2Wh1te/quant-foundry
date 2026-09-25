"""Finite resource policy; callers must measure usage at batch boundaries.

This policy is separate from the filesystem/process budget enforcers. Defining
limits alone is not an admission check or evidence of bounded runtime usage.
"""
from dataclasses import dataclass, fields

from .errors import DataStoreError

MiB = 1024**2
GiB = 1024**3


@dataclass(frozen=True, slots=True)
class StoreLimits:
    scratch_bytes: int = 4 * GiB       # staging + DuckDB spill, one shared budget
    minimum_free_bytes: int = GiB
    garbage_bytes: int = 512 * MiB
    batch_rows: int = 65_536
    batch_bytes: int = 64 * MiB
    query_rows: int = 10_000
    query_bytes: int = 16 * MiB
    query_timeout_ms: int = 30_000
    lock_timeout_ms: int = 5_000
    duckdb_memory_bytes: int = 512 * MiB
    duckdb_threads: int = 2
    parallel_writers: int = 1
    issue_count: int = 10_000
    issue_sample_bytes: int = 64 * MiB
    log_bytes: int = 256 * MiB
    log_days: int = 7
    completed_run_days: int = 30
    completed_runs_per_task: int = 1000

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 < value <= 2**63 - 1:
                raise DataStoreError('INVALID_CONFIGURATION')
        if (self.batch_bytes > self.scratch_bytes
                or self.query_bytes > self.duckdb_memory_bytes):
            raise DataStoreError('INVALID_CONFIGURATION')

    def check_write(self, *, staging_bytes: int, spill_bytes: int,
                    reserved_bytes: int, additional_bytes: int,
                    available_bytes: int, garbage_bytes: int) -> None:
        """Check a measured sample plus outstanding reservations, not quotas.

        The storage coordinator must hold the shared admission lock and reserve
        additional_bytes before releasing it. Both actual temporary usage and
        outstanding reservations count; rounding/reservation overhead is safe.
        """
        amounts = (staging_bytes, spill_bytes, reserved_bytes, additional_bytes,
                   available_bytes, garbage_bytes)
        if any(type(v) is not int or v < 0 for v in amounts):
            raise DataStoreError('INVALID_CONFIGURATION')
        if staging_bytes + spill_bytes + reserved_bytes + additional_bytes > self.scratch_bytes:
            raise DataStoreError('SCRATCH_BUDGET_EXCEEDED')
        if available_bytes - reserved_bytes - additional_bytes < self.minimum_free_bytes:
            raise DataStoreError('DISK_PRESSURE')
        if garbage_bytes >= self.garbage_bytes:
            raise DataStoreError('GARBAGE_BUDGET_EXCEEDED')
