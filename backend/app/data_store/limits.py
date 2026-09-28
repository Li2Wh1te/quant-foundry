"""Finite resource policy; callers must measure usage at batch boundaries.

This policy is separate from the filesystem/process budget enforcers. Defining
limits alone is not an admission check or evidence of bounded runtime usage.
"""
from dataclasses import dataclass, fields, replace

from .errors import DataStoreError

MiB = 1024**2
GiB = 1024**3


def local_operation_limits(*, base=None, scratch_bytes=None, issue_count=None, pipeline_spill_bytes=None):
    """Explicit finite maintenance/scheduler overrides; defaults stay unchanged.

    Disk staging is independent of resident memory. A large native rescan may
    need more temporary disk and current error records without increasing the
    per-commit, process-memory or DuckDB-memory limits.
    """
    values = {}
    for name, value, maximum in (('scratch_bytes', scratch_bytes, 128*GiB),
                                  ('issue_count', issue_count, 1_000_000),
                                  ('pipeline_spill_bytes', pipeline_spill_bytes, 64*GiB)):
        if value is not None:
            if type(value) is not int or not 1 <= value <= maximum:
                raise DataStoreError('INVALID_CONFIGURATION')
            values[name] = value
    return replace(base or StoreLimits(), **values)


@dataclass(frozen=True, slots=True)
class StoreLimits:
    commit_rows: int = 1_000_000
    commit_bytes: int = 256 * MiB
    file_rows: int = 262_144
    file_bytes: int = 128 * MiB
    changed_files: int = 64
    query_scan_bytes: int = 512 * MiB
    query_files: int = 128
    query_partitions: int = 64
    operation_slots: int = 16
    process_memory_bytes: int = 2 * GiB
    write_timeout_ms: int = 300_000
    garbage_count: int = 2048
    cleanup_batch: int = 128
    orphan_grace_seconds: int = 60
    scratch_bytes: int = 4 * GiB       # staging + DuckDB spill, one shared budget
    pipeline_spill_bytes: int = 0      # zero preserves the legacy DuckDB-sized scan quota
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
            if field.name=='pipeline_spill_bytes' and type(value) is int and value==0:
                continue
            if type(value) is not int or not 0 < value <= 2**63 - 1:
                raise DataStoreError('INVALID_CONFIGURATION')
        if (self.batch_bytes > self.scratch_bytes
                or self.query_bytes > self.duckdb_memory_bytes
                or self.parallel_writers > self.operation_slots
                or self.operation_slots > 128):
            raise DataStoreError('INVALID_CONFIGURATION')
        if self.pipeline_spill_bytes and self.pipeline_spill_bytes + min(
                self.scratch_bytes,self.commit_bytes*3+self.duckdb_memory_bytes)>self.scratch_bytes:
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
