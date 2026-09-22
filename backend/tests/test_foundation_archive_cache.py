"""Archive verification must observe mutations even after a cache hit."""
import hashlib
import os
from types import SimpleNamespace

from app.data_foundation.execution import _archive_available, _verify_archive_bytes


def archive_file(tmp_path):
    _verify_archive_bytes.cache_clear()
    path = tmp_path / 'runtime.tar'
    path.write_bytes(b'original')
    archive = SimpleNamespace(archive_key=path.name, byte_count=8,
        archive_hash=hashlib.sha256(b'original').hexdigest())
    return path, archive


def test_unchanged_archive_is_hashed_once(tmp_path, monkeypatch):
    path, archive = archive_file(tmp_path)
    original = hashlib.file_digest
    calls = []
    def tracked(stream, algorithm):
        calls.append(stream.name)
        return original(stream, algorithm)
    monkeypatch.setattr(hashlib, 'file_digest', tracked)
    assert _archive_available(archive, tmp_path)
    assert _archive_available(archive, tmp_path)
    assert calls == [str(path)]
    path.unlink()
    assert not _archive_available(archive, tmp_path)


def test_same_size_mutation_with_restored_mtime_is_rejected(tmp_path):
    path, archive = archive_file(tmp_path)
    assert _archive_available(archive, tmp_path)
    before = path.stat()
    path.write_bytes(b'corrupt!')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not _archive_available(archive, tmp_path)
    # A rejected checksum is not memoized; restoring the file allows retry.
    path.write_bytes(b'original')
    assert _archive_available(archive, tmp_path)


def test_replacement_with_restored_metadata_is_rejected(tmp_path):
    path, archive = archive_file(tmp_path)
    assert _archive_available(archive, tmp_path)
    before = path.stat()
    replacement = tmp_path / 'replacement.tar'
    replacement.write_bytes(b'corrupt!')
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert not _archive_available(archive, tmp_path)


def test_mutation_during_hash_does_not_seed_cache(tmp_path, monkeypatch):
    path, archive = archive_file(tmp_path)
    original = hashlib.file_digest
    def mutating(stream, algorithm):
        result = original(stream, algorithm)
        path.write_bytes(b'corrupt!')
        return result
    monkeypatch.setattr(hashlib, 'file_digest', mutating)
    assert not _archive_available(archive, tmp_path)
    assert _verify_archive_bytes.cache_info().currsize == 0


def test_expected_hash_change_and_directory_are_rejected(tmp_path):
    path, archive = archive_file(tmp_path)
    assert _archive_available(archive, tmp_path)
    archive.archive_hash = '0' * 64
    assert not _archive_available(archive, tmp_path)
    path.unlink()
    path.mkdir()
    archive.byte_count = path.stat().st_size
    assert not _archive_available(archive, tmp_path)
