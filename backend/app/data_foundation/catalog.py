"""Immutable definitions and executable dependency registration."""
from datetime import datetime, timezone
import hashlib
import re
from sqlalchemy import select, text
from app.data_foundation.canonical import FoundationError, digest, encode
from app.data_foundation.models import Artifact, Definition, Execution, DependencyManifest, DependencyEntry, SourceRef, Binding


def now():
    return datetime.now(timezone.utc)


def lock_key(session, namespace, identity):
    """Serialize competing registrations, including an initially absent row.

    PostgreSQL transaction advisory locks protect the lookup/insert boundary;
    all durable uniqueness constraints remain the final integrity defense.
    """
    if session.bind.dialect.name == 'postgresql':
        key = int(digest(namespace, identity)[:16], 16)
        session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key if key < 2**63 else key - 2**64})


def register_definition(session, *, kind, name, version, definition):
    if kind not in {'contract', 'series', 'policy', 'support'} or not name.strip() or not version.strip():
        raise ValueError('Invalid definition identity')
    if len(name) > 128 or len(version) > 128 or definition.get('schema_version') != 1:
        raise ValueError('Invalid definition schema')
    required = {
        'contract': {'subject_kind', 'key_fields', 'core_fields', 'optional_fields', 'time_semantics', 'atomic_unit', 'supported_filters'},
        'series': {'currency', 'interval', 'session_scope', 'price_basis', 'method_status', 'anchor_status'},
        'policy': {'dataset', 'major', 'profile', 'series', 'input_admission', 'core_fields', 'source_order', 'comparison', 'fallback', 'atomicity'},
        'support': {'read_status', 'update_status', 'replay_status', 'notice_days', 'approval_required'},
    }[kind]
    if not required <= definition.keys():
        raise ValueError('Incomplete definition')
    content_hash = digest('definition', {'kind': kind, 'name': name, 'version': version, 'definition': definition})
    lock_key(session, 'definition', [kind, name, version])
    old = session.scalar(select(Definition).where(Definition.kind == kind, Definition.name == name, Definition.version == version))
    if old:
        if old.content_hash != content_hash:
            raise FoundationError('DEFINITION_CONFLICT', '同一底座定义版本的内容不能变更。')
        return old
    row = Definition(kind=kind, name=name, version=version, content_hash=content_hash, definition_json=encode(definition), created_at=now())
    session.add(row)
    session.flush()
    return row


def register_execution(session, manifest, artifact: bytes | None = None):
    required = {'schema_version', 'git_commit', 'runtime_image_digest', 'python_version', 'dependency_lock_hash', 'parser', 'transform', 'quality', 'config', 'decoder_refs', 'archive'}
    if set(manifest) != required or manifest['schema_version'] != 1:
        raise ValueError('Invalid execution manifest')
    # Do not accept arbitrary environment dumps. Configuration is a strict
    # allow-list; no token, password, URL, local path, or executable module path.
    if set(manifest['config']) - {'batch_rows', 'budget_seconds', 'lease_seconds', 'heartbeat_seconds'}:
        raise ValueError('Unsupported execution configuration')
    if not re.fullmatch(r'[0-9a-f]{40}', manifest['git_commit']):
        raise ValueError('Execution requires a full commit')
    for part in ('parser', 'transform', 'quality'):
        if set(manifest[part]) != {'key', 'version', 'hash'} or not re.fullmatch(r'[0-9a-f]{64}', manifest[part]['hash']):
            raise ValueError('Invalid executable component')
    if not re.fullmatch(r'[0-9a-f]{64}', manifest['dependency_lock_hash']):
        raise ValueError('Invalid dependency lock digest')
    image = manifest['runtime_image_digest']
    if image is not None and not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('Invalid immutable image digest')
    archive = manifest['archive']
    if set(archive) != {'key', 'sha256', 'verified'} or archive['verified'] is not False:
        # Archive restoration is deliberately a separate capability. Until a
        # controlled verifier is implemented, callers cannot self-assert ready.
        raise ValueError('Unverified archive required; replay remains dependency_missing')
    if artifact is not None and len(artifact) > 16 * 1024 * 1024:
        raise ValueError('Artifact exceeds 16 MiB')
    mh = digest('execution', {'manifest': manifest, 'artifact_hash': hashlib.sha256(artifact).hexdigest() if artifact is not None else None})
    lock_key(session, 'execution', mh)
    old = session.scalar(select(Execution).where(Execution.manifest_hash == mh))
    if old:
        return old
    artifact_row = None
    if artifact is not None:
        ah = hashlib.sha256(artifact).hexdigest()
        lock_key(session, 'artifact', ah)
        artifact_row = session.scalar(select(Artifact).where(Artifact.content_hash == ah))
        if artifact_row is None:
            artifact_row = Artifact(content_hash=ah, payload=artifact, created_at=now())
            session.add(artifact_row)
            session.flush()
    row = Execution(manifest_hash=mh, manifest_json=encode(manifest), artifact_id=artifact_row.id if artifact_row else None,
                    replay_status='dependency_missing', created_at=now())
    session.add(row)
    session.flush()
    return row


def register_dependencies(session, entries):
    """Seal ordered typed dependencies with their actual persisted digests."""
    kinds = {'source_ref_id': (SourceRef, 'content_hash'), 'binding_id': (Binding, None),
             'execution_id': (Execution, 'manifest_hash'), 'definition_id': (Definition, 'content_hash')}
    resolved = []
    for entry in entries:
        refs = set(entry) & kinds.keys()
        if len(refs) != 1 or set(entry) != refs | {'purpose'} or not entry['purpose']:
            raise ValueError('Invalid typed dependency')
        key = refs.pop()
        model, hash_field = kinds[key]
        row = session.get(model, entry[key])
        if row is None:
            raise FoundationError('DEPENDENCY_MISSING', '固定依赖不存在，不能封存清单。')
        h = getattr(row, hash_field) if hash_field else digest('binding', {c.name: getattr(row, c.name) for c in model.__table__.columns})
        resolved.append({**entry, 'content_hash': h})
    mh = digest('dependencies', resolved)
    lock_key(session, 'dependencies', mh)
    old = session.scalar(select(DependencyManifest).where(DependencyManifest.manifest_hash == mh))
    if old:
        return old
    row = DependencyManifest(manifest_hash=mh, created_at=now())
    session.add(row)
    session.flush()
    session.add_all([DependencyEntry(manifest_id=row.id, ordinal=i, **entry) for i, entry in enumerate(resolved)])
    session.flush()
    return row
