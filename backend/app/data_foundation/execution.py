"""Verify closed execution dependencies; never substitute the newest transform."""
import hashlib
import json
from functools import lru_cache
import os
from pathlib import Path
import platform
import stat
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.models import Execution, Artifact
from app.data_foundation.work_models import ExecutionArchive, RuntimeArchive
from app.data_foundation.bars import DOMAIN_KEY, domain_hash


def _file_identity(info):
    # ctime detects in-place changes even when an operator restores mtime;
    # device/inode detect replacement of the deployment-owned archive.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode)


@lru_cache(maxsize=16)
def _verify_archive_bytes(path, identity, expected_hash):
    # Cache successful checks only. Check both the opened descriptor and the
    # path after hashing so replacement or mutation cannot seed a stale entry.
    with path.open('rb') as stream:
        if _file_identity(os.fstat(stream.fileno())) != identity:
            raise OSError('Archive changed before verification')
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if (_file_identity(os.fstat(stream.fileno())) != identity
                or _file_identity(path.stat()) != identity):
            raise OSError('Archive changed during verification')
    if actual != expected_hash:
        raise OSError('Archive checksum mismatch')


def _archive_available(archive, archive_root):
    if archive is None:
        return False
    path = Path(archive_root) / archive.archive_key
    try:
        info = path.stat()
        if (path.name != archive.archive_key or not stat.S_ISREG(info.st_mode)
                or info.st_size != archive.byte_count):
            return False
        identity = _file_identity(info)
        # Opening on every call also checks live access controls (including
        # ACL/mount changes not reliably represented by a cached stat tuple).
        # Cache hits avoid reading the archive, not proving it is readable.
        with path.open('rb') as stream:
            if _file_identity(os.fstat(stream.fileno())) != identity:
                return False
            _verify_archive_bytes(path, identity, archive.archive_hash)
        # Every cache hit still checks live metadata; deleted, modified, or
        # replaced files must never inherit a previous successful check.
        return _file_identity(path.stat()) == identity
    except OSError:
        return False


def installed_code_hash():
    # All foundation modules are part of one allow-listed implementation. A
    # helper change must invalidate old work just like an adapter change does.
    return digest('foundation-code', {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))})


def verify_execution(session, work, runtime_image_digest, archive_root):
    params = json.loads(work.parameters_json)
    execution = session.get(Execution, work.execution_id)
    manifest = json.loads(execution.manifest_json)
    link = session.get(ExecutionArchive, execution.id)
    artifact = session.get(Artifact, execution.artifact_id) if execution.artifact_id else None
    from app.data_foundation import tushare, holding_work, record_work
    domains = {DOMAIN_KEY: domain_hash, tushare.DOMAIN_KEY: tushare.domain_hash,
        holding_work.DOMAIN_KEY: holding_work.domain_hash, record_work.DOMAIN_KEY: record_work.domain_hash}
    resolver = domains.get(params['domain'])
    if resolver is None or params['domain_hash'] != resolver() or not link or not artifact:
        raise FoundationError('DEPENDENCY_MISSING', '工作固定执行依赖或运行归档不可用。')
    archive = session.get(RuntimeArchive, link.archive_id)
    lock_path = Path(__file__).resolve().parents[2] / 'uv.lock'
    if (not runtime_image_digest or runtime_image_digest != archive.image_digest or manifest['runtime_image_digest'] != runtime_image_digest
        or manifest['config'] != {'batch_rows': 200, 'budget_seconds': 30, 'lease_seconds': 60, 'heartbeat_seconds': 15}
        or manifest['python_version'] != platform.python_version()
        or manifest['dependency_lock_hash'] != hashlib.sha256(lock_path.read_bytes()).hexdigest()
        or any(manifest[part]['hash'] != installed_code_hash() for part in ('parser','transform','quality'))
        or hashlib.sha256(artifact.payload).hexdigest() != artifact.content_hash):
        raise FoundationError('DEPENDENCY_MISSING', '当前运行环境与固定执行清单不一致。')
    # Only a basename is stored; no caller-supplied path traversal or network
    # fallback is permitted. Archives are deployment-owned and mounted read-only.
    if not _archive_available(archive, archive_root):
        raise FoundationError('DEPENDENCY_MISSING', '固定运行镜像归档缺失、无法读取或校验失败。')


def replay_status(session, execution_id, archive_root='/app/data/foundation-runtime-archives'):
    link = session.get(ExecutionArchive, execution_id)
    if not link:
        return 'dependency_missing'
    archive = session.get(RuntimeArchive, link.archive_id)
    return 'ready' if _archive_available(archive, archive_root) else 'dependency_missing'


def register_archive(session, execution_id, evidence, archive_root):
    """Register a deployment verifier's evidence only after rehashing its file."""
    from app.data_foundation.catalog import lock_key, now
    from app.data_foundation.canonical import encode
    import re
    required = {'image_digest','archive_key','archive_hash','byte_count','verification'}
    if set(evidence) != required or not re.fullmatch(r'sha256:[0-9a-f]{64}', evidence['image_digest']):
        raise ValueError('Invalid runtime archive evidence')
    execution = session.get(Execution, execution_id)
    if execution is None:
        raise FoundationError('DEPENDENCY_MISSING', '固定执行清单不存在。')
    manifest = json.loads(execution.manifest_json)
    check = evidence['verification']
    if set(check) != {'restored_image_digest','network','python','lock_hash'} or check['network'] != 'none' or check['restored_image_digest'] != evidence['image_digest'] or check['python'] != manifest['python_version'] or check['lock_hash'] != manifest['dependency_lock_hash'] or manifest['runtime_image_digest'] != evidence['image_digest']:
        raise FoundationError('DEPENDENCY_MISSING', '运行镜像恢复证据与执行清单不一致。')
    if evidence['archive_key'] != evidence['image_digest'].split(':')[1]+'.tar':
        raise ValueError('Archive key must be its image digest')
    path = Path(archive_root) / evidence['archive_key']
    try:
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream,'sha256').hexdigest()
    except OSError:
        raise FoundationError('DEPENDENCY_MISSING', '固定运行镜像归档无法读取，未登记引用。') from None
    if path.stat().st_size != evidence['byte_count'] or actual != evidence['archive_hash']:
        raise FoundationError('DEPENDENCY_MISSING', '运行归档文件校验不一致。')
    lock_key(session,'runtime-archive',evidence['image_digest'])
    row = session.scalar(select(RuntimeArchive).where(RuntimeArchive.image_digest == evidence['image_digest']))
    if row is None:
        row = RuntimeArchive(image_digest=evidence['image_digest'],archive_key=evidence['archive_key'],archive_hash=actual,
            byte_count=evidence['byte_count'],verification_json=encode(check),created_at=now())
        session.add(row); session.flush()
    elif row.archive_hash != actual:
        raise FoundationError('ARCHIVE_CONFLICT', '已登记镜像归档不能被不同文件替换。')
    link = session.get(ExecutionArchive, execution_id)
    if link is None:
        session.add(ExecutionArchive(execution_id=execution_id,archive_id=row.id)); session.flush()
    elif link.archive_id != row.id:
        raise FoundationError('ARCHIVE_CONFLICT', '执行依赖不能更换运行归档。')
    return row
