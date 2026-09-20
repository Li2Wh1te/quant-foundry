"""Backup packages fail closed on missing, changed, or escaped dependencies."""
import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('foundation_backup',Path(__file__).resolve().parents[2]/'scripts/foundation_backup.py')
backup=importlib.util.module_from_spec(spec);spec.loader.exec_module(backup)


def package(root):
    (root/'database.dump').write_bytes(b'isolated archive fixture')
    (root/'runtime.tar').write_bytes(b'isolated runtime fixture')
    backup.write_json(root/'evidence.json',dict(archives=[dict(key='runtime.tar',bytes=(root/'runtime.tar').stat().st_size,sha256=backup.sha(root/'runtime.tar'))]))
    files=[dict(name=p.name,bytes=p.stat().st_size,sha256=backup.sha(p)) for p in root.iterdir()]
    backup.write_json(root/'manifest.json',dict(format='qf-foundation-backup-v1',state='complete',files=files))


def test_complete_package_and_tampered_archive(tmp_path):
    package(tmp_path)
    assert backup.verify_package(tmp_path)['state']=='complete'
    (tmp_path/'runtime.tar').write_bytes(b'changed')
    with pytest.raises(backup.BackupError,match='摘要'):backup.verify_package(tmp_path)


def test_missing_dependency_and_partial_package(tmp_path):
    package(tmp_path)
    (tmp_path/'runtime.tar').unlink()
    with pytest.raises(backup.BackupError,match='缺失'):backup.verify_package(tmp_path)
    value=json.loads((tmp_path/'manifest.json').read_text());value['state']='partial'
    (tmp_path/'manifest.json').write_text(json.dumps(value))
    with pytest.raises(backup.BackupError,match='封存'):backup.verify_package(tmp_path)


def test_path_traversal_and_symlink_rejected(tmp_path):
    with pytest.raises(backup.BackupError):backup.safe_file(tmp_path,'../database.dump')
    (tmp_path/'real').write_bytes(b'fixture');(tmp_path/'linked').symlink_to(tmp_path/'real')
    with pytest.raises(backup.BackupError):backup.safe_file(tmp_path,'linked')


def test_restore_refuses_unlabelled_target_before_any_database_write(tmp_path,monkeypatch):
    package(tmp_path);commands=[]
    def run(command,**kwargs):
        from types import SimpleNamespace
        commands.append(command)
        return SimpleNamespace(stdout=json.dumps([{'Config':{'Labels':{}},'Mounts':[]}]))
    monkeypatch.setattr(backup,'run',run)
    with pytest.raises(backup.BackupError,match='隔离恢复标签'):backup.restore_sandbox(tmp_path,'production')
    assert commands==[['docker','inspect','production']]


def test_retention_keeps_daily_weekly_and_survives_gaps():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    packages = [dict(name=str(day), created_at=(now-timedelta(days=day)).isoformat()) for day in range(50)]
    keep = backup.retention_set(packages)
    assert {str(day) for day in range(7)} <= keep
    weeks = {(now-timedelta(days=int(name))).isocalendar()[:2] for name in keep}
    assert len(weeks) == 4
    # An outage must not turn the absence of recent successes into deletion.
    assert {str(day) for day in range(40,47)} <= backup.retention_set(packages[40:])


def test_scheduled_backup_protects_tested_copy_and_unknown_files(tmp_path, monkeypatch):
    protected = tmp_path/'restore-tested'; protected.mkdir(); package(protected)
    root = tmp_path/'scheduled'
    def create(project, destination):
        destination.mkdir(); package(destination)
        value=json.loads((destination/'manifest.json').read_text())
        value['created_at']='2026-09-20T03:00:00+00:00'
        (destination/'manifest.json').write_text(json.dumps(value))
        return {'status':'complete'}
    monkeypatch.setattr(backup, 'create_backup', create)
    result=backup.scheduled_backup(tmp_path,root,protected)
    assert result['retained']==1 and protected.is_dir()
    index=json.loads((root/'backup-index.json').read_text())
    first=root/index['packages'][0]['name']
    (first/'operator-note.txt').write_text('Keep this file')
    # Force an expired, explicitly owned package to exercise the deletion guard.
    monkeypatch.setattr(backup, 'retention_set', lambda packages:{packages[-1]['name']})
    with pytest.raises(backup.BackupError, match='未登记文件'):
        backup.scheduled_backup(tmp_path,root,protected)
    assert (first/'operator-note.txt').exists() and protected.is_dir()


def test_scheduled_backup_refuses_damaged_protected_copy(tmp_path,monkeypatch):
    protected=tmp_path/'protected';protected.mkdir();package(protected)
    (protected/'runtime.tar').write_bytes(b'corrupted')
    monkeypatch.setattr(backup,'create_backup',lambda *a:pytest.fail('Backup invoked without valid protection'))
    with pytest.raises(backup.BackupError,match='摘要'):
        backup.scheduled_backup(tmp_path,tmp_path/'scheduled',protected)


@pytest.mark.parametrize('free_space',[100,10*1024**3])
def test_create_checks_space_before_dump_and_seals_only_declared_files(tmp_path,monkeypatch,free_space):
    import io
    from types import SimpleNamespace
    project=tmp_path/'deployment';archive_root=project/'data'/'foundation-runtime-archives'
    archive_root.mkdir(parents=True);(archive_root/'runtime.tar').write_bytes(b'runtime bytes')
    (project/'.env').write_text('PRIVATE_TOKEN=fixture-do-not-copy')
    evidence={'archives':[dict(key='runtime.tar',bytes=13,sha256=backup.sha(archive_root/'runtime.tar'))]}
    writes=[];commands=[]
    coordinator=SimpleNamespace(stdout=io.StringIO(json.dumps({'snapshot':'000A-000B-1','evidence':evidence})+'\n'),
        stdin=SimpleNamespace(write=writes.append,flush=lambda:None,close=lambda:None),
        poll=lambda:0,wait=lambda **kwargs:0)
    monkeypatch.setattr(backup.subprocess,'Popen',lambda *args,**kwargs:coordinator)
    monkeypatch.setattr(backup.shutil,'disk_usage',lambda path:SimpleNamespace(free=free_space))
    def run(command,**kwargs):
        commands.append(command)
        if 'images' in command:return SimpleNamespace(stdout=json.dumps([{'ID':'test-image','Service':'backend'}]))
        if command[:3]==['docker','image','inspect']:
            return SimpleNamespace(stdout=json.dumps([{'Id':'sha256:'+'a'*64,'Size':100}]))
        if any('pg_database_size' in part for part in command):return SimpleNamespace(stdout='1000\n')
        if any('pg_dump' in part for part in command):kwargs['stdout'].write(b'postgres fixture dump')
        elif command[:3]==['docker','image','save']:kwargs['stdout'].write(b'archived application fixture')
        else:pytest.fail('Unexpected backup command')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(backup,'run',run)
    destination=tmp_path/'complete'
    if free_space==100:
        with pytest.raises(backup.BackupError,match='空间不足'):backup.create_backup(project,destination)
        assert not any(any('pg_dump' in part for part in command) for command in commands)
        assert not destination.exists() and not writes
    else:
        result=backup.create_backup(project,destination)
        assert result['status']=='complete' and writes==['complete\n']
        manifest=backup.verify_package(destination)
        assert manifest['recovery_point_at']<=manifest['created_at']
        assert len(manifest['application_archives'])==1
        assert not (destination/'.env').exists()
        assert 'fixture-do-not-copy' not in (destination/'manifest.json').read_text()
