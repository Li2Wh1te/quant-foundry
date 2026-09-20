"""Create verified snapshot packages and restore ONLY to labelled empty sandboxes.

Run on the deployment host with Python's standard library. Passwords stay inside
existing Docker containers. The package excludes .env and host credentials.
No command enables an ingestion scheduler or clears the recovery gate. Scheduled
rotation can delete only packages recorded by this tool in its dedicated root.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone


class BackupError(ValueError):
    pass


def run(command, **kwargs):
    try:
        return subprocess.run(command, check=True, stderr=subprocess.PIPE, **kwargs)
    except subprocess.CalledProcessError:
        # External output may contain paths or connection settings. Keep it out
        # of ordinary diagnostics; the operator can inspect the isolated job.
        raise BackupError('备份或恢复命令执行失败；未标记成功，请核对运行环境。') from None


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    path.chmod(0o600)


def safe_file(root, name):
    if not isinstance(name,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]+',name) or name in ('.','..'):
        raise BackupError('备份文件标识不合法。')
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise BackupError('备份文件缺失或包含不受支持的链接。')
    return path


def verify_package(directory):
    root = Path(directory)
    manifest = json.loads(safe_file(root,'manifest.json').read_text())
    if manifest.get('format') != 'qf-foundation-backup-v1' or manifest.get('state') != 'complete':
        raise BackupError('备份未完整封存。')
    files = manifest.get('files', [])
    names = [item['name'] for item in files]
    if len(set(names)) != len(names) or not {'database.dump','evidence.json'} <= set(names):
        raise BackupError('备份缺少数据库或一致性证据。')
    for item in files:
        path = safe_file(root,item['name'])
        if path.stat().st_size != item['bytes'] or sha(path) != item['sha256']:
            raise BackupError('备份文件摘要或大小不一致。')
    for image in manifest.get('application_archives',[]):
        if image['name'] not in names or not re.fullmatch(r'sha256:[0-9a-f]{64}',image['image_digest']):
            raise BackupError('部署镜像归档不完整。')
    evidence = json.loads((root/'evidence.json').read_text())
    for archive in evidence['archives']:
        matching = [item for item in files if item['name'] == archive['key']]
        if len(matching)!=1 or matching[0]['sha256']!=archive['sha256'] or matching[0]['bytes']!=archive['bytes']:
            raise BackupError('数据库引用的运行归档未完整备份。')
    return manifest


def create_backup(project, destination, *, exporter=None):
    recovery_point_at = datetime.now(timezone.utc).isoformat()
    project, destination = Path(project).resolve(), Path(destination).absolute()
    if destination.exists():
        raise BackupError('备份目标已存在，请使用新的目录。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name+'.partial-'+uuid.uuid4().hex)
    temporary.mkdir(mode=0o700)
    compose = ['docker','compose','--project-directory',str(project)]
    coordinator = subprocess.Popen(compose+['exec','-T','backend']+(exporter or ['python','-m','app.data_foundation.recovery','export']),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        line = coordinator.stdout.readline()
        if not line:
            raise BackupError('一致备份快照未能建立。')
        response = json.loads(line)
        snapshot, evidence = response['snapshot'], response['evidence']
        if not re.fullmatch(r'[0-9A-Fa-f-]+',snapshot):
            raise BackupError('快照标识无效。')
        # Estimate before writing a dump on the same host as production. Use
        # uncompressed database/image sizes, double the database allowance, and
        # reserve an additional GiB; a compressed dump is normally much smaller.
        image_output=run(compose+['images','--format','json'],stdout=subprocess.PIPE,text=True).stdout.strip()
        images=json.loads(image_output) if image_output.startswith('[') else [json.loads(line) for line in image_output.splitlines()]
        identities=[]
        for image_id in sorted({row['ID'] for row in images}):
            metadata=json.loads(run(['docker','image','inspect',image_id],stdout=subprocess.PIPE,text=True).stdout)[0]
            identity=metadata['Id']
            if not re.fullmatch(r'sha256:[0-9a-f]{64}',identity):
                raise BackupError('部署镜像摘要不合法。')
            identities.append((identity,metadata['Size']))
        database_size=int(run(compose+['exec','-T','postgres','sh','-c',
            'exec psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT pg_database_size(current_database())"'],
            stdout=subprocess.PIPE,text=True).stdout.strip())
        estimated = sum(a['bytes'] for a in evidence['archives']) + sum(size for _,size in identities) + database_size*2
        if shutil.disk_usage(temporary).free < estimated + 1024**3:
            raise BackupError('备份目录可用空间不足。')
        dump = temporary/'database.dump'
        with dump.open('xb') as stream:
            run(compose+['exec','-T','postgres','sh','-c',
                'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --snapshot "$1"', 'qf-backup',snapshot], stdout=stream)
            stream.flush(); os.fsync(stream.fileno())
        dump.chmod(0o600)
        write_json(temporary/'evidence.json',evidence)
        # The database transaction remains open through copying immutable files;
        # missing or changed dependencies abort the package, never silently skip.
        for archive in evidence['archives']:
            original = safe_file(project/'data'/'foundation-runtime-archives',archive['key'])
            if original.stat().st_size!=archive['bytes'] or sha(original)!=archive['sha256']:
                raise BackupError('运行归档与数据库固定引用不一致。')
            copied=temporary/archive['key']
            shutil.copyfile(original,copied);copied.chmod(0o600)
            with copied.open('rb') as stream:os.fsync(stream.fileno())
        application_archives=[]
        for identity,_ in identities:
            name='application-'+identity.split(':')[1]+'.tar'
            with (temporary/name).open('xb') as stream:
                run(['docker','image','save',identity],stdout=stream)
                stream.flush();os.fsync(stream.fileno())
            (temporary/name).chmod(0o600)
            application_archives.append(dict(image_digest=identity,name=name))
        files=[dict(name=p.name,bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(temporary.iterdir())]
        # Store image identities only, not compose config/environment output.
        manifest=dict(format='qf-foundation-backup-v1',state='complete',created_at=datetime.now(timezone.utc).isoformat(),
            recovery_point_at=recovery_point_at,
            snapshot=snapshot,files=files,application_archives=application_archives,images=[{k:r.get(k) for k in ('Service','ID','Repository','Tag')} for r in images])
        coordinator.stdin.write('complete\n');coordinator.stdin.flush();coordinator.stdin.close()
        if coordinator.wait(timeout=30)!=0:
            raise BackupError('一致快照协调进程未正常完成。')
        write_json(temporary/'manifest.json',manifest)
        verify_package(temporary)
        os.rename(temporary,destination)
        descriptor=os.open(destination.parent,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
        return dict(status='complete',files=len(files),bytes=sum(f['bytes'] for f in files),
            manifest_sha256=sha(destination/'manifest.json'),message='底座一致备份及运行归档已封存并校验，尚未替代独立副本和恢复验证。')
    finally:
        if coordinator.poll() is None:
            coordinator.terminate()
            try:coordinator.wait(timeout=10)
            except subprocess.TimeoutExpired:coordinator.kill();coordinator.wait()
        # Failed partial packages are retained for explicit inspection/removal;
        # they can never pass verify_package without all dependencies present.


def restore_sandbox(directory, target_container):
    manifest=verify_package(directory)
    inspected=json.loads(run(['docker','inspect',target_container],stdout=subprocess.PIPE,text=True).stdout)[0]
    if inspected['Config'].get('Labels',{}).get('qf.foundation.recovery')!='isolated':
        raise BackupError('恢复目标没有隔离恢复标签，禁止写入。')
    if any(m.get('Type')=='bind' for m in inspected.get('Mounts',[])):
        raise BackupError('恢复目标挂载了主机目录，禁止写入。')
    if inspected.get('HostConfig',{}).get('Privileged'):
        raise BackupError('恢复目标不能是特权容器。')
    for mount in inspected.get('Mounts',[]):
        if mount.get('Type')=='volume':
            volume=json.loads(run(['docker','volume','inspect',mount['Name']],stdout=subprocess.PIPE,text=True).stdout)[0]
            if volume.get('Labels',{}).get('qf.foundation.recovery')!='isolated':
                raise BackupError('恢复目标的数据卷不是独立恢复卷，禁止写入。')
    networks=inspected.get('NetworkSettings',{}).get('Networks',{})
    for name in networks:
        network=json.loads(run(['docker','network','inspect',name],stdout=subprocess.PIPE,text=True).stdout)[0]
        if not network.get('Internal'):
            raise BackupError('恢复目标必须使用禁止外联的内部网络。')
    database='qf_restore_'+uuid.uuid4().hex
    sql=f'CREATE DATABASE "{database}";\nALTER DATABASE "{database}" SET qf.foundation_recovery_pending = \'on\';\n'
    run(['docker','exec','-i',target_container,'sh','-c','exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres'],input=sql.encode(),stdout=subprocess.DEVNULL)
    with (Path(directory)/'database.dump').open('rb') as stream:
        run(['docker','exec','-i',target_container,'sh','-c',
            'exec pg_restore --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d "$1"', 'qf-restore',database],stdin=stream,stdout=subprocess.DEVNULL)
    return dict(status='restored_pending_review',database=database,manifest_sha256=sha(Path(directory)/'manifest.json'),
        message='备份已恢复到新建隔离数据库；底座读取保持关闭，需核验数据、当前问题和权限后再决定开放。')


def retention_set(packages):
    """Retain the newest package from seven UTC days and four ISO weeks.

    A day/week is a bucket with a successful package, not elapsed wall time.
    Gaps therefore cannot erase the last available recovery points. The
    separately protected, restore-tested package never enters this deletion set.
    """
    days, weeks, keep = set(), set(), set()
    for package in sorted(packages, key=lambda item: item['created_at'], reverse=True):
        moment = datetime.fromisoformat(package['created_at']).astimezone(timezone.utc)
        day, week = moment.date(), moment.isocalendar()[:2]
        if day not in days and len(days) < 7:
            days.add(day); keep.add(package['name'])
        if week not in weeks and len(weeks) < 4:
            weeks.add(week); keep.add(package['name'])
    return keep


def scheduled_backup(project, root, protected_backup):
    """Serialize daily jobs and rotate only this tool's successfully owned files.

    The operator supplies a package already verified by an isolated restore.
    Its digest is checked on every run before any deletion. Missing or damaged
    protection stops rotation; an interrupted job leaves extra packages rather
    than deleting an unrecorded or last-known recoverable copy.
    """
    import fcntl
    root, protected = Path(root).absolute(), Path(protected_backup).resolve()
    if root.is_symlink():
        raise BackupError('定时备份目录不能是链接。')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    index_path = root / 'backup-index.json'
    if not index_path.exists() and any(root.iterdir()):
        raise BackupError('首次定时备份需要独立空目录，禁止接管其他文件。')
    root.chmod(0o700)
    with (root / '.backup.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError('上一轮备份仍在执行，本轮未重复启动。') from None
        verify_package(protected)
        protected_hash = sha(protected / 'manifest.json')
        state = json.loads(index_path.read_text()) if index_path.exists() else dict(
            format='qf-foundation-backup-index-v1', packages=[], protected_path=str(protected),
            protected_manifest=protected_hash)
        if (state.get('format') != 'qf-foundation-backup-index-v1'
                or state.get('protected_path') != str(protected)
                or state.get('protected_manifest') != protected_hash):
            raise BackupError('已验证恢复副本与备份登记不一致，禁止自动替换或轮转。')

        def save():
            temporary = root / ('index-' + uuid.uuid4().hex + '.partial')
            write_json(temporary, state)
            os.replace(temporary, index_path)
            descriptor = os.open(root, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)

        # Persist ownership before a potentially interrupted first backup.
        save()
        name = 'backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex
        result = create_backup(project, root / name)
        manifest = verify_package(root / name)
        state['packages'].append(dict(name=name, created_at=manifest['created_at'],
                                     manifest_sha256=sha(root / name / 'manifest.json')))
        save()
        keep = retention_set(state['packages'])
        removed = 0
        for item in list(state['packages']):
            if item['name'] in keep:
                continue
            if not re.fullmatch(r'backup-\d{8}T\d{6}Z-[0-9a-f]{32}', item['name']):
                raise BackupError('备份轮转目录标识无效，未删除该目录。')
            candidate = root / item['name']
            if candidate.is_symlink() or candidate.resolve() == protected:
                raise BackupError('备份轮转目标不符合独立目录约束。')
            # Recovery after a crash between removal and index replacement.
            if candidate.exists():
                verified = verify_package(candidate)
                if sha(candidate / 'manifest.json') != item['manifest_sha256']:
                    raise BackupError('备份登记与目录内容不一致，未删除该目录。')
                expected_files = {entry['name'] for entry in verified['files']} | {'manifest.json'}
                if {entry.name for entry in candidate.iterdir()} != expected_files:
                    raise BackupError('备份目录包含未登记文件，未删除该目录。')
                shutil.rmtree(candidate)
            state['packages'].remove(item); removed += 1
            save()
        return {**result, 'removed': removed, 'retained': len(state['packages']),
                'protected_manifest': protected_hash,
                'message': f'底座定时备份已封存并校验；保留{len(state["packages"])}份轮转副本，清理{removed}份旧副本，已验证恢复副本继续保留。'}


def main():
    parser=argparse.ArgumentParser(description='底座一致备份与隔离恢复；不会覆盖现有数据库。')
    sub=parser.add_subparsers(dest='command',required=True)
    create=sub.add_parser('create');create.add_argument('--project',required=True);create.add_argument('--destination',required=True)
    verify=sub.add_parser('verify');verify.add_argument('--directory',required=True)
    restore=sub.add_parser('restore-sandbox');restore.add_argument('--directory',required=True);restore.add_argument('--target-container',required=True)
    scheduled=sub.add_parser('scheduled');scheduled.add_argument('--project',required=True);scheduled.add_argument('--root',required=True);scheduled.add_argument('--protected-backup',required=True)
    args=parser.parse_args()
    if args.command=='create':result=create_backup(args.project,args.destination)
    elif args.command=='restore-sandbox':result=restore_sandbox(args.directory,args.target_container)
    elif args.command=='scheduled':result=scheduled_backup(args.project,args.root,args.protected_backup)
    else:
        verify_package(args.directory);result=dict(status='verified',message='备份文件及归档依赖校验通过。')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except (BackupError,OSError,ValueError,KeyError) as exc:
        print(json.dumps(dict(status='failed',message=str(exc) if isinstance(exc,BackupError) else '备份结构或运行环境不符合要求，未标记成功。'),ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1)
