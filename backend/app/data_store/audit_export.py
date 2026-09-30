"""Bounded, read-only evidence export for the current data store.

The database connection runs in a PostgreSQL READ ONLY snapshot. This module
never constructs CurrentStore: that constructor binds a root and can create a
runtime row or a store identity. Catalog paths are used only through LocalFiles'
checked, relative, no-follow file opener. The only writes are new export files
outside the store root.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import time
from typing import Callable
from urllib import error as urlerror, parse as urlparse, request as urlrequest
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import text

from .adapters.registry import ENTRIES, Entry
from .errors import DataStoreError
from .filesystem import LocalFiles
from .locking import _scope_key
from .schema import DatasetSpec


@dataclass(frozen=True)
class AuditLimits:
    seconds: int = 300
    files: int = 10000
    bytes: int = 8 * 1024**3
    rows: int = 20_000_000
    api_samples: int = 8

    def __post_init__(self):
        if not (1 <= self.seconds <= 3600 and 1 <= self.files <= 100000
                and 1 <= self.bytes <= 1 << 42 and 1 <= self.rows <= 1_000_000_000
                and 0 <= self.api_samples <= 100):
            raise ValueError("Audit limits must be finite and positive")


class AuditIncomplete(Exception):
    """A finite budget or unavailable evidence prevented a complete check."""


def _json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _check(checks: list[dict], name: str, status: str, *, scope: str, expected=None,
           actual=None, code: str | None = None) -> None:
    record = {"check": name, "status": status, "scope": scope,
              "expected": expected, "actual": actual}
    if code:
        record["code"] = code
    checks.append(record)


def _root_token(files: LocalFiles) -> UUID:
    # Opening the existing identity is deliberately different from root_token(),
    # which creates it on a fresh root and is therefore unsuitable for audit.
    fd = os.open(".store-id", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=files.fd)
    try:
        files._regular(fd)
        raw = os.read(fd, 37)
        if len(raw) != 36 or os.read(fd, 1):
            raise AuditIncomplete("STORE_ID_INVALID")
        return UUID(raw.decode("ascii"))
    except (ValueError, UnicodeError):
        raise AuditIncomplete("STORE_ID_INVALID") from None
    finally:
        os.close(fd)


def _read_file(files: LocalFiles, row: dict, spec: DatasetSpec,
               check_time: Callable[[], None]) -> tuple[int, tuple | None]:
    """Verify one current file's exact bytes, contract and every business key."""
    old = DatasetSpec.from_descriptor(json.loads(row["contract_json"]))
    if (not spec.accepts(old) or row["schema_id"] != old.schema_id
            or row["rule"] != old.rule):
        raise DataStoreError("REBUILD_REQUIRED")
    if row["path"].split("/")[1:2] != [_scope_key(spec.name)]:
        raise DataStoreError("FILE_INVALID")
    with files.open_object(row["path"]) as fd:
        info = os.fstat(fd)
        if info.st_size != row["byte_count"]:
            raise DataStoreError("FILE_INVALID")
        digest = hashlib.sha256()
        while block := os.read(fd, 1024 * 1024):
            check_time()
            digest.update(block)
        if digest.hexdigest() != row["content_hash"]:
            raise DataStoreError("FILE_INVALID")
        os.lseek(fd, 0, os.SEEK_SET)
        # ParquetFile owns the duplicate descriptor. The catalog fd remains
        # pinned for the entire integrity check even if cleanup runs nearby.
        with os.fdopen(os.dup(fd), "rb") as handle:
            with pq.ParquetFile(handle, page_checksum_verification=True) as parquet:
                if not parquet.schema_arrow.equals(old.schema, check_metadata=True):
                    raise DataStoreError("REBUILD_REQUIRED")
                if parquet.metadata.num_rows != row["row_count"]:
                    raise DataStoreError("FILE_INVALID")
                seen = 0
                first = last = None
                sample = None
                for batch in parquet.iter_batches(batch_size=1024):
                    check_time()
                    keys = old.validate_batch(batch, max_rows=1024,
                                              max_bytes=128 * 1024 * 1024)
                    spec.check_partition(batch, row["partition_key"])
                    if keys and last is not None and keys[0] <= last:
                        raise DataStoreError("KEY_ORDER_INVALID")
                    if keys and first is None:
                        first = keys[0]
                        values = [batch.column(batch.schema.get_field_index(key))[0].as_py()
                                  for key in spec.key[:3]]
                        sample = tuple(str(value) for value in values)
                    if keys:
                        last = keys[-1]
                    seen += batch.num_rows
                if (seen != row["row_count"] or first != bytes(row["key_min"])
                        or last != bytes(row["key_max"])):
                    raise DataStoreError("FILE_INVALID")
                return seen, sample


def _api_client(base_url: str, token: str, timeout: int):
    url = urlparse.urlsplit(base_url)
    if (url.scheme not in ("http", "https") or not url.netloc or url.username
            or url.password or url.query or url.fragment
            or (url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"))):
        raise ValueError("API URL must use HTTPS or local loopback HTTP")
    prefix = base_url.rstrip("/") + "/api/admin/data-store"
    class NoRedirect(urlrequest.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            # An API redirect must never forward the operator's bearer token.
            return None
    opener = urlrequest.build_opener(NoRedirect)

    def call(method: str, path: str, payload: dict | None = None):
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        req = urlrequest.Request(prefix + path, data=body, method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=timeout) as response:
                return response.status, json.load(response)
        except urlerror.HTTPError as response:
            try:
                return response.code, json.load(response)
            finally:
                response.close()

    return call


def _directory_bytes(files: LocalFiles, directory: str, *, cap: int,
                     check_time: Callable[[], None]) -> int:
    """Count existing scratch/log bytes without following links or creating files."""
    total = visited = 0
    with files.directory(directory) as root_fd:
        pending = [os.dup(root_fd)]
    try:
        while pending:
            parent = pending.pop()
            try:
                with os.scandir(parent) as items:
                    for item in items:
                        check_time()
                        visited += 1
                        if visited > cap:
                            raise AuditIncomplete("DIRECTORY_BUDGET_EXCEEDED")
                        info = item.stat(follow_symlinks=False)
                        if info.st_dev != files.identity[0] or stat.S_ISLNK(info.st_mode):
                            raise AuditIncomplete("UNSAFE_STORAGE_PATH")
                        if stat.S_ISDIR(info.st_mode):
                            child = os.open(item.name, os.O_RDONLY | os.O_DIRECTORY |
                                            os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                            pending.append(child)
                        elif stat.S_ISREG(info.st_mode):
                            total += info.st_size
                        else:
                            raise AuditIncomplete("UNSAFE_STORAGE_PATH")
            finally:
                os.close(parent)
    finally:
        for fd in pending:
            os.close(fd)
    return total


def export_audit(engine, root: Path, output_dir: Path, *, entries: tuple[Entry, ...] = ENTRIES,
                 limits: AuditLimits = AuditLimits(), api_base_url: str | None = None,
                 api_token: str | None = None, api_request=None, local_only: bool = False) -> dict:
    """Export finite evidence; any truncation or unchecked source is incomplete.

    The caller supplies an existing PostgreSQL engine. A snapshot anchors all
    catalog counts; a fresh final read detects generation changes before a pass
    can be reported. No untrusted business value, DSN or raw file path is saved.
    """
    root, output_dir = Path(root), Path(output_dir)
    if not root.is_absolute() or not output_dir.is_absolute() or output_dir.exists():
        raise ValueError("Existing absolute root and new absolute output directory required")
    if root.resolve() in output_dir.resolve(strict=False).parents:
        raise ValueError("Audit output must be outside the current store")
    output_dir.mkdir(mode=0o700)
    started = time.monotonic()
    deadline = started + limits.seconds
    checks: list[dict] = []
    domains: list[dict] = []
    selected = {entry.id for entry in entries}
    dispositions: list[dict] = [{
        "entry_id": entry.id, "classification": entry.disposition,
        "dataset": entry.spec.name if entry.business else None,
        "target_entry": entry.target, "selected": entry.id in selected,
        "latest_input_scope": None, "latest_input_complete": None,
        "latest_mode": None, "latest_state": None, "latest_reason": None,
        "latest_partitions": None,
        "source_rows_processed": None, "normalized_units_processed": None,
        "input_failures_processed": None, "passes": None,
        "current_key_count": None,
    } for entry in ENTRIES]
    disposition_by_id = {row["entry_id"]: row for row in dispositions}
    coverage_results = []
    status_fingerprints = {}
    current_generations: dict[str, int] = {}
    samples: list[tuple[Entry, tuple[str, str, str], int, int]] = []
    used_files = used_bytes = used_rows = 0
    attempted_files = attempted_bytes = attempted_rows = 0
    runtime: dict = {"format": "qf-current-audit-v1", "generated_at": datetime.now(timezone.utc).isoformat(),
                     "scope": "all_entries" if selected == {entry.id for entry in ENTRIES} else "selected_entries",
                     "selected_entries": sorted(selected),
                     "coverage_mode": "local_only" if local_only else "full_range",
                     "global_acceptance": False,
                     "production_state": "unasserted_by_audit",
                     "catalog_transaction": "repeatable_read_read_only",
                     "versions": {name: importlib.metadata.version(name)
                                  for name in ("pyarrow", "duckdb", "sqlalchemy")}}

    def time_check():
        if time.monotonic() > deadline:
            raise AuditIncomplete("AUDIT_TIMEOUT")

    try:
        with closing(LocalFiles(root)) as files:
            root_id = _root_token(files)
            runtime["root_identity_sha256"] = hashlib.sha256(str(root_id).encode()).hexdigest()
            with engine.connect() as connection:
                connection.exec_driver_sql("BEGIN")
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                connection.exec_driver_sql(f"SET LOCAL statement_timeout = {limits.seconds * 1000}")
                runtime["postgres_read_only"] = connection.execute(
                    text("SELECT current_setting('transaction_read_only')")).scalar_one() == "on"
                if not runtime["postgres_read_only"]:
                    raise AuditIncomplete("POSTGRES_NOT_READ_ONLY")
                runtime_row = connection.execute(text(
                    "SELECT root_token FROM data_store_runtime WHERE singleton=1")).scalar_one_or_none()
                if runtime_row != root_id:
                    raise AuditIncomplete("ROOT_CATALOG_MISMATCH")
                _check(checks, "root_binding", "pass", scope="store", expected="matching identity",
                       actual="matching identity")
                control_names = ["data_store_runtime", "data_store_datasets", "data_store_files",
                                 "data_store_scopes", "data_store_issues", "data_store_garbage",
                                 "data_store_entry_status", "data_store_legacy_maintenance",
                                 "data_store_legacy_restrictions"]
                runtime["control_db_bytes"] = int(connection.execute(text(
                    "SELECT coalesce(sum(pg_total_relation_size(oid)),0) FROM pg_class "
                    "WHERE relname=ANY(:names) AND relnamespace=current_schema()::regnamespace"),
                    {"names": control_names}).scalar_one())
                garbage = connection.execute(text(
                    "SELECT count(*) AS files,coalesce(sum(byte_count),0) AS bytes "
                    "FROM data_store_garbage")).one()
                runtime["garbage_queue_files"] = int(garbage[0])
                runtime["garbage_queue_bytes"] = int(garbage[1])
                runtime["logs_bytes"] = _directory_bytes(files, ".logs", cap=limits.files * 10,
                                                           check_time=time_check)
                runtime["scratch_current_bytes"] = _directory_bytes(
                    files, ".scratch", cap=limits.files * 10, check_time=time_check)
                for entry in ENTRIES:
                    time_check()
                    status_row = connection.execute(text(
                        "SELECT summary_json FROM data_store_entry_status WHERE entry_id=:id"),
                        {"id": entry.id}).scalar_one_or_none()
                    try:
                        status = json.loads(status_row) if status_row else None
                    except (ValueError, TypeError):
                        status = None
                        _check(checks, "entry_status", "fail", scope=entry.id,
                               code="STATUS_INVALID")
                    disposition = disposition_by_id[entry.id]
                    disposition.update(
                        latest_mode=status.get('mode') if status else None,
                        latest_state=status.get('state') if status else None,
                        latest_reason=status.get('reason') if status else None,
                        latest_partitions=json.dumps(status.get('partitions', [])) if status else None,
                        latest_input_scope=status.get("scope") if status else None,
                        latest_input_complete=status.get("complete") if status else None,
                        source_rows_processed=status.get("source_rows") if status else None,
                        normalized_units_processed=status.get("normalized_units") if status else None,
                        input_failures_processed=status.get("input_failures") if status else None,
                        passes=status.get("passes") if status else None)
                    if entry.id in selected:
                        counts = ("source_rows", "normalized_units", "input_failures", "passes")
                        if (status is None or status.get("complete") is not True or
                                any(type(status.get(name)) is not int or status[name] < 0
                                    for name in counts)):
                            _check(checks, "input_disposition", "incomplete", scope=entry.id,
                                   code="INPUT_STATUS_INCOMPLETE")
                        else:
                            _check(checks, "input_disposition", "pass", scope=entry.id,
                                   expected="complete latest processing scope",
                                   actual=status.get("scope", "entry"))
                    if entry.id not in selected or not entry.business:
                        continue
                    from .coverage import check_coverage
                    status_fingerprints[entry.id] = status_row
                    judgment = check_coverage(connection, entry, status)
                    coverage_results.append(dict(entry_id=entry.id, **judgment))
                    if not local_only:
                        _check(checks, "full_range_coverage", "pass" if judgment['satisfied'] else "incomplete",
                               scope=entry.id, code=judgment['reason'],
                               expected="exhausted sealed input range and no pending work",
                               actual=judgment['evidence'])
                    dataset = entry.spec.name
                    row = connection.execute(text(
                        "SELECT generation,row_count,byte_count,schema_id,rule,descriptor_json "
                        "FROM data_store_datasets WHERE name=:dataset"),
                        {"dataset": dataset}).mappings().first()
                    domain = {"entry_id": entry.id, "dataset": dataset,
                              "limitations": entry.describe_capability()["limitations"],
                              "generation": None, "current_key_count": None,
                              "current_file_count": None, "current_file_bytes": None,
                              "unresolved_issues": None, "source_scope_count": None,
                              "status": "not_checked", "api_sampled": False}
                    domains.append(domain)
                    if row is None:
                        _check(checks, "dataset_registered", "incomplete", scope=entry.id,
                               expected="registered current dataset", actual="missing",
                               code="DATASET_MISSING")
                        continue
                    current_generations[dataset] = row["generation"]
                    domain.update(generation=row["generation"], current_key_count=row["row_count"],
                                  current_file_bytes=row["byte_count"])
                    disposition["current_key_count"] = row["row_count"]
                    from .issue_sets import logical_count
                    issue_count = logical_count(connection,dataset)
                    scope_count = connection.execute(text(
                        "SELECT count(*) FROM data_store_scopes WHERE dataset=:dataset"),
                        {"dataset": dataset}).scalar_one()
                    legacy_count = connection.execute(text(
                        "SELECT count(*) FROM data_store_legacy_restrictions "
                        "WHERE dataset=:dataset OR dataset IS NULL"),
                        {"dataset": dataset}).scalar_one()
                    domain.update(unresolved_issues=issue_count, source_scope_count=scope_count,
                                  legacy_restrictions=legacy_count,
                                  status="restricted" if issue_count or legacy_count else
                                         "empty" if not row["row_count"] else "available")
                    if (row["schema_id"] != entry.spec.schema_id or row["rule"] != entry.spec.rule
                            or not entry.spec.accepts(DatasetSpec.from_descriptor(
                                json.loads(row["descriptor_json"])))):
                        domain["status"] = "rebuild_required"
                        _check(checks, "dataset_contract", "fail", scope=entry.id,
                               code="REBUILD_REQUIRED")
                        continue
                    file_stats = connection.execute(text(
                        "SELECT count(*) AS files,coalesce(sum(row_count),0) AS rows,"
                        "coalesce(sum(byte_count),0) AS bytes FROM data_store_files "
                        "WHERE dataset=:dataset"), {"dataset": dataset}).mappings().one()
                    # PostgreSQL SUM(bigint) is numeric. Convert these bounded
                    # counts to Python ints before comparing and serializing.
                    file_stats = {key: int(value) for key, value in file_stats.items()}
                    domain["current_file_count"] = file_stats["files"]
                    if (file_stats["rows"] != row["row_count"] or
                            file_stats["bytes"] != row["byte_count"]):
                        _check(checks, "catalog_totals", "fail", scope=entry.id,
                               expected={"rows": row["row_count"], "bytes": row["byte_count"]},
                               actual={"rows": file_stats["rows"], "bytes": file_stats["bytes"]})
                        continue
                    if (attempted_files + file_stats["files"] > limits.files or
                            attempted_bytes + file_stats["bytes"] > limits.bytes or
                            attempted_rows + file_stats["rows"] > limits.rows):
                        _check(checks, "full_file_scan", "incomplete", scope=entry.id,
                               code="AUDIT_BUDGET_EXCEEDED")
                        continue
                    verified_rows = 0
                    last_by_partition: dict[str, bytes] = {}
                    first_sample = None
                    try:
                        rows = connection.execute(text(
                            "SELECT partition_key,path,content_hash,row_count,byte_count,"
                            "schema_id,rule,contract_json,key_min,key_max "
                            "FROM data_store_files WHERE dataset=:dataset "
                            "ORDER BY partition_key,key_min"),
                            {"dataset": dataset}).mappings()
                        for file_row in rows:
                            time_check()
                            attempted_files += 1
                            attempted_bytes += file_row["byte_count"]
                            attempted_rows += file_row["row_count"]
                            actual_rows, sample = _read_file(files, dict(file_row), entry.spec, time_check)
                            first = bytes(file_row["key_min"])
                            previous = last_by_partition.get(file_row["partition_key"])
                            if previous is not None and first <= previous:
                                raise DataStoreError("KEY_ORDER_INVALID")
                            last_by_partition[file_row["partition_key"]] = bytes(file_row["key_max"])
                            verified_rows += actual_rows
                            if first_sample is None and sample is not None:
                                first_sample = sample
                        if verified_rows != row["row_count"]:
                            raise DataStoreError("FILE_INVALID")
                        used_files += file_stats["files"]
                        used_bytes += file_stats["bytes"]
                        used_rows += file_stats["rows"]
                        _check(checks, "full_file_scan", "pass", scope=entry.id,
                               expected={"rows": row["row_count"], "files": file_stats["files"]},
                               actual={"rows": verified_rows, "files": file_stats["files"]})
                        if first_sample and len(first_sample) >= 3:
                            samples.append((entry, first_sample[:3], row["generation"], row["row_count"]))
                    except (DataStoreError, OSError, ValueError, RuntimeError,
                            pa.ArrowException, AuditIncomplete) as exc:
                        code = str(exc) if isinstance(exc, AuditIncomplete) else getattr(exc, "code", "FILE_INVALID")
                        _check(checks, "full_file_scan", "incomplete" if isinstance(exc, AuditIncomplete)
                               else "fail", scope=entry.id, code=code)
                        if isinstance(exc, AuditIncomplete):
                            break
                connection.rollback()
            # The fresh transaction is intentionally outside the repeatable read
            # snapshot. A changed generation makes the assembled export stale.
            if current_generations:
                with engine.connect() as connection:
                    connection.exec_driver_sql("BEGIN READ ONLY")
                    connection.exec_driver_sql(f"SET LOCAL statement_timeout = {limits.seconds * 1000}")
                    changed = []
                    for dataset, generation in current_generations.items():
                        current = connection.execute(text(
                            "SELECT generation FROM data_store_datasets WHERE name=:dataset"),
                            {"dataset": dataset}).scalar_one_or_none()
                        if current != generation:
                            changed.append(dataset)
                    connection.rollback()
                _check(checks, "generation_stability", "fail" if changed else "pass",
                       scope="store", expected="unchanged", actual="changed" if changed else "unchanged",
                       code="DATA_CHANGED" if changed else None)
    except (DataStoreError, AuditIncomplete, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, AuditIncomplete) else getattr(exc, "code", "AUDIT_UNAVAILABLE")
        _check(checks, "audit_snapshot", "incomplete", scope="store", code=code)
    except Exception:
        # Neither SQL diagnostics nor provider payloads belong in the export.
        _check(checks, "audit_snapshot", "incomplete", scope="store", code="AUDIT_UNAVAILABLE")

    if api_request is None and api_base_url and api_token:
        try:
            api_request = _api_client(api_base_url, api_token, min(limits.seconds, 20))
        except ValueError:
            api_request = None
    if api_request is None:
        _check(checks, "api_sample", "incomplete", scope="selected", code="API_NOT_CONFIGURED")
    elif not samples:
        _check(checks, "api_sample", "incomplete", scope="selected", code="NO_CURRENT_SAMPLE")
    else:
        for entry, sample, generation, expected_rows in samples[:limits.api_samples]:
            try:
                time_check()
                status, detail = api_request("GET", "/datasets/" + urlparse.quote(entry.spec.name, safe=""))
                if (status != 200 or detail.get("generation") != generation
                        or detail.get("row_count") != expected_rows):
                    raise AuditIncomplete("API_DESCRIPTOR_MISMATCH")
                from .router import _frequency
                payload = {"dataset": entry.spec.name, "frequency": _frequency(entry),
                           "representation": sample[0], "subject": sample[1],
                           "from_key": sample[2], "to_key": sample[2], "page_size": 1}
                status, page = api_request("POST", "/query", payload)
                if status != 200 or page.get("generation") != generation or not page.get("rows"):
                    raise AuditIncomplete("API_QUERY_MISMATCH")
                if (tuple(str(page["rows"][0].get(key)) for key in
                          ("representation", "subject", "object_key")) != sample or
                        page.get("business_date_coverage_verified") is not False):
                    raise AuditIncomplete("API_KEY_MISMATCH")
                old_status, old_page = api_request("POST", "/query?release_id=retired", payload)
                if (old_status != 422 or old_page.get("detail", {}).get("code")
                        != "HISTORY_UNSUPPORTED"):
                    raise AuditIncomplete("API_HISTORY_GUARD_MISSING")
                sample_hash = hashlib.sha256(json.dumps(sample, ensure_ascii=False,
                    separators=(",", ":")).encode()).hexdigest()
                _check(checks, "api_sample", "pass", scope=entry.id,
                       expected={"generation": generation, "sample_key_sha256": sample_hash},
                       actual={"generation": page["generation"], "sample_key_sha256": sample_hash})
                next(domain for domain in domains if domain["entry_id"] == entry.id)["api_sampled"] = True
            except (AuditIncomplete, OSError, ValueError, KeyError, TypeError) as exc:
                _check(checks, "api_sample", "incomplete", scope=entry.id,
                       code=str(exc) if isinstance(exc, AuditIncomplete) else "API_UNAVAILABLE")
        if limits.api_samples == 0:
            _check(checks, "api_sample", "incomplete", scope="selected", code="API_SAMPLE_BUDGET_ZERO")

    if current_generations:
        try:
            time_check()
            with engine.connect() as connection:
                connection.exec_driver_sql("BEGIN")
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                connection.exec_driver_sql(f"SET LOCAL statement_timeout = {limits.seconds * 1000}")
                final = connection.execute(text(
                    "SELECT name,generation FROM data_store_datasets "
                    "WHERE name=ANY(:names)"),
                    {"names": list(current_generations)}).all()
                final_statuses = dict(connection.execute(text(
                    'SELECT entry_id,summary_json FROM data_store_entry_status WHERE entry_id=ANY(:ids)'),
                    {'ids': list(status_fingerprints)}).all())
                connection.rollback()
            observed = {name: generation for name, generation in final}
            changed = any(observed.get(name) != generation
                          for name, generation in current_generations.items())
            changed = changed or final_statuses != status_fingerprints
            _check(checks, "final_generation_stability", "fail" if changed else "pass",
                   scope="store", expected="unchanged after API sample",
                   actual="changed" if changed else "unchanged",
                   code="DATA_CHANGED" if changed else None)
        except Exception:
            _check(checks, "final_generation_stability", "incomplete", scope="store",
                   code="CATALOG_UNAVAILABLE")

    runtime["checked_files"] = used_files
    runtime["checked_bytes"] = used_bytes
    runtime["checked_rows"] = used_rows
    runtime["attempted_files"] = attempted_files
    runtime["attempted_bytes"] = attempted_bytes
    runtime["attempted_rows"] = attempted_rows
    runtime["official_active_bytes_checked"] = used_bytes
    runtime["duration_seconds"] = round(time.monotonic() - started, 3)
    runtime["api_scope"] = "sampled_current_keys_only"
    runtime["input_scope"] = "latest_entry_status_not_unique_historical_observations"
    runtime["raw_preserved_bytes"] = None
    runtime["staging_peak_bytes"] = None
    runtime["spill_peak_bytes"] = None
    runtime["other_system_bytes"] = None
    runtime["completeness"] = "complete" if checks and all(c["status"] == "pass" for c in checks) else "incomplete"
    runtime['global_acceptance'] = (not local_only and runtime['completeness']=='complete'
                                   and {e.id for e in ENTRIES if e.business} <= selected)
    _json(output_dir / "runtime.json", runtime)
    _json(output_dir / "full_coverage.json", coverage_results)
    _json(output_dir / "current_domains.json", domains)
    with (output_dir / "source_disposition.csv").open("x", newline="", encoding="utf-8") as handle:
        fields = list(dispositions[0]) if dispositions else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(dispositions)
    with (output_dir / "checks.jsonl").open("x", encoding="utf-8") as handle:
        for record in checks:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    (output_dir / "RESULT.md").write_text(
        "# Current data store audit\n\n"
        f"Status: {runtime['completeness']}\n\n"
        f"Coverage mode: {runtime['coverage_mode']}; scope: {runtime['scope']}.\n\n"
        f"Checked {used_files} current files and {used_rows} current rows. "
        "API checks cover sampled current keys only. Input counts describe the latest "
        "stored processing summary, not unique historical observations. "
        "Production reset and rebuild are not asserted by this export.\n",
        encoding="utf-8")
    sums = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in output_dir.iterdir() if path.is_file()}
    _json(output_dir / "SHA256SUMS.json", sums)
    return {"complete": runtime["completeness"] == "complete",
            "coverage_mode": runtime['coverage_mode'], "global_acceptance": runtime['global_acceptance'],
            "status": runtime["completeness"], "checks": len(checks),
            "checked_files": used_files, "checked_rows": used_rows,
            "output": str(output_dir)}
