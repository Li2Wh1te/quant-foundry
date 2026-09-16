"""Bounded operator commands. No implicit scans, schedules, or provider calls."""
import argparse
import hashlib
import io
import tarfile
from pathlib import Path
import platform
from sqlalchemy.orm import Session
from app.db.session import get_engine
from app.data_foundation.baselines import capture_s1, apply_s1
from app.data_foundation.canonical import encode, FoundationError
from app.data_foundation.catalog import register_execution
from app.data_foundation.contracts import register_initial_catalog
from app.data_foundation.source_refs import register_observation
from uuid import UUID


def local_execution(session, git_commit, image_digest=None):
    root = Path(__file__).resolve().parents[2]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for path in sorted(Path(__file__).parent.glob('*.py')):
            payload = path.read_bytes()
            info = tarfile.TarInfo('app/data_foundation/' + path.name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    code = buffer.getvalue()
    code_hash = hashlib.sha256(code).hexdigest()
    component = dict(key='foundation-source-v1', version='1', hash=code_hash)
    return register_execution(session, dict(schema_version=1, git_commit=git_commit, runtime_image_digest=image_digest,
        python_version=platform.python_version(), dependency_lock_hash=hashlib.sha256((root / 'uv.lock').read_bytes()).hexdigest(),
        parser=component, transform=component, quality=component, config={}, decoder_refs=[],
        archive=dict(key=None, sha256=None, verified=False)), code)


def main():
    parser = argparse.ArgumentParser(description='固定批准的 S1 来源；不启动采集或发布正式值。')
    parser.add_argument('command', choices=['s1-dry-run', 's1-apply', 'observation-register'])
    parser.add_argument('--expected-hash')
    parser.add_argument('--event-key')
    parser.add_argument('--git-commit')
    parser.add_argument('--image-digest')
    parser.add_argument('--observation-id', action='append', default=[])
    args = parser.parse_args()
    engine = get_engine()
    if args.command == 's1-dry-run':
        capture = capture_s1(engine)
        print(encode({'scope': capture['scope'], 'content_hash': capture['content_hash'], 'observed_at': capture['observed_at'],
            'counts': {k: len(v) for k, v in capture['content'].items()},
            'identities': [{'code': r['ts_code'], 'instrument_id': r['etf_id']} for r in capture['content']['directory']],
            'binding_status': 'unresolved'}))
        return
    if not args.git_commit:
        parser.error('--git-commit is required')
    if args.command == 's1-apply' and (not args.expected_hash or not args.event_key):
        parser.error('s1-apply requires --expected-hash and --event-key')
    if args.command == 'observation-register' and not args.observation_id:
        parser.error('explicit --observation-id is required; current state is never substituted')
    capture = capture_s1(engine) if args.command == 's1-apply' else None
    with Session(engine) as session, session.begin():
        register_initial_catalog(session)
        execution = local_execution(session, args.git_commit, args.image_digest)
        if capture:
            result = apply_s1(session, capture, expected_hash=args.expected_hash, decoder_id=execution.id, event_key=args.event_key)
        else:
            result = {'source_refs': [str(register_observation(session, UUID(value), execution.id).id) for value in args.observation_id]}
    print(encode({'status': 'registered', 'result': result, 'replay_status': 'dependency_missing'}))


if __name__ == '__main__':
    try:
        main()
    except FoundationError as exc:
        print(encode({'status': 'failed', 'code': exc.code, 'message': str(exc)}))
        raise SystemExit(1)
