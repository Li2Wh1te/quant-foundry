"""Archive and restore-check an explicit local image without touching secrets.

Run on the deployment host. The digest and archive hash are then registered
inside the backend, so an existing release tag is never moved or recreated.
"""
import argparse
import grp
import pwd
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


def run(args):
    return subprocess.check_output(args, text=True).strip()


def grant_container_group_read(path, reader_gid):
    """Share only with the owner's private group, added inside the container.

    The host's app UID may belong to an unrelated account. A supplementary
    group assigned only to the container avoids granting that host account any
    access. Refuse shared host groups rather than broadening their permissions.
    """
    metadata = path.stat()
    if type(reader_gid) is not int or reader_gid <= 0 or metadata.st_gid != reader_gid:
        raise ValueError('Reader group must match the archive owner group')
    owner = pwd.getpwuid(metadata.st_uid).pw_name
    members = set(grp.getgrgid(reader_gid).gr_mem)
    members.update(user.pw_name for user in pwd.getpwall() if user.pw_gid == reader_gid)
    if members - {owner}:
        raise ValueError('Archive owner group is shared with unrelated host users')
    path.chmod(0o640)


def archive_image(image, directory, reader_gid=None):
    digest = run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'])
    if not digest.startswith('sha256:') or len(digest) != 71:
        raise ValueError('Invalid Docker image digest')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    size = int(run(['docker', 'image', 'inspect', digest, '--format', '{{.Size}}']))
    if shutil.disk_usage(directory).free < size * 2:
        raise ValueError('Insufficient space for a bounded archive and verification')
    target = directory / (digest.split(':')[1] + '.tar')
    if not target.exists():
        temporary = target.with_suffix('.partial')
        try:
            subprocess.run(['docker', 'image', 'save', '--output', str(temporary), digest], check=True)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    # Other host users retain no access. The deployment adds this verified
    # private owner group only to the application container's supplementary GIDs.
    if reader_gid is not None:
        grant_container_group_read(target, reader_gid)
    with tarfile.open(target, 'r') as saved:
        manifest = json.load(saved.extractfile('manifest.json'))
        configurations = [hashlib.sha256(saved.extractfile(item['Config']).read()).hexdigest() for item in manifest]
        image_hash = digest.split(':')[1]
        blob = 'blobs/sha256/' + image_hash
        if image_hash not in configurations and (blob not in saved.getnames() or hashlib.sha256(saved.extractfile(blob).read()).hexdigest() != image_hash):
            raise ValueError('Archive does not contain the requested image identity')
    # A successful load checks archive structure/content; an isolated container
    # additionally exercises the archived Python/dependency runtime offline.
    subprocess.run(['docker', 'image', 'load', '--input', str(target)], check=True, stdout=subprocess.DEVNULL)
    restored = run(['docker', 'image', 'inspect', digest, '--format', '{{.Id}}'])
    probe = json.loads(run(['docker','run','--rm','--network','none','--entrypoint','python',digest,'-c',
        'import json,platform,hashlib,pathlib; import psycopg,sqlalchemy; print(json.dumps({"python":platform.python_version(),"lock_hash":hashlib.sha256(pathlib.Path("/app/uv.lock").read_bytes()).hexdigest()}))']))
    if restored != digest:
        raise ValueError('Restored image digest mismatch')
    with target.open('rb') as stream:
        archive_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    return dict(image_digest=digest, archive_key=target.name, archive_hash=archive_hash,
        byte_count=target.stat().st_size, verification={'restored_image_digest': restored, 'network':'none', **probe})


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--reader-gid', type=int, help='Private archive owner group added inside the container')
    args=parser.parse_args()
    result=archive_image(args.image,args.directory,args.reader_gid)
    Path(args.output).write_text(json.dumps(result,sort_keys=True)+'\n')
    print(json.dumps({'status':'verified','image_digest':result['image_digest'],'archive_key':result['archive_key'],'byte_count':result['byte_count']}))
