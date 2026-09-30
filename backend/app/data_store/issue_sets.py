"""Lossless current issue sets; physical records are not affected-object counts.

Every member retains its original key, token, exact target, resolution and
observation metadata. Compaction never validates a source or clears an issue.
"""
from collections import defaultdict
import hashlib
import json

from sqlalchemy import text

from .catalog import Issue, validate_issue_changes
from .errors import DataStoreError
from .adapters.contracts import digest, native_json

VERSION='object-key-set-v1'
COUNT_SQL="CASE WHEN target_json::jsonb->>'scope_version'='object-key-set-v1' THEN greatest(1,jsonb_array_length(target_json::jsonb->'members')) ELSE 1 END"


def logical_count(connection,dataset=None,*,cached=False):
    # Operational summaries use trigger-maintained member totals; an independent
    # audit keeps the default actual-record expansion and detects real gaps.
    if cached:
        from .issue_accounting import check_enabled
        check_enabled(connection)
        return int(connection.execute(text('SELECT coalesce(sum(affected_objects),0) FROM data_store_issue_totals '
            'WHERE (CAST(:d AS text) IS NULL OR dataset=:d)'),{'d':dataset}).scalar_one())
    return int(connection.execute(text('SELECT coalesce(sum('+COUNT_SQL+'),0) FROM data_store_issues '
                                   'WHERE (CAST(:d AS text) IS NULL OR dataset=:d)'),{'d':dataset}).scalar_one())


def members(row):
    """Return original issue records; malformed sets must fail closed."""
    target=json.loads(row['target_json'])
    if target.get('scope_version')!=VERSION:return [dict(row)]
    values=target.get('members')
    if (not isinstance(values,list) or not values or len(values)>4096 or
            target.get('affected_objects')!=len(values)):
        raise DataStoreError('FILE_INVALID')
    if row['evidence_token']!=digest(target):raise DataStoreError('FILE_INVALID')
    result=[];seen=set()
    for value in values:
        if not isinstance(value,dict):raise DataStoreError('FILE_INVALID')
        original=value.get('target')
        if (not isinstance(original,dict) or original.get('scope_version')!='object-key-v1'
                or original.get('prefix',[])[:2]!=target.get('prefix') or
                original.get('partition')!=target.get('partition') or
                original.get('group')!=target.get('group')):
            raise DataStoreError('FILE_INVALID')
        key=value.get('key')
        if not isinstance(key,str) or key in seen:raise DataStoreError('FILE_INVALID')
        seen.add(key)
        result.append({'dataset':row.get('dataset'),'scope_key':row['scope_key'],
            'issue_key':key,'_set_key':row['issue_key'],'reason':row['reason'],'evidence_token':value['token'],
            'target_json':native_json(original),'resolution_json':native_json(value['resolution']),
            **{k:value.get(k) for k in ('attempts','first_seen','last_seen')}})
    return result


def record(issue,previous=None):
    value={'scope_key':issue.scope,'issue_key':issue.key,'reason':issue.reason,
           'evidence_token':issue.evidence_token,'target_json':issue.target_json,
           'resolution_json':issue.resolution_json}
    if previous:
        value.update({k:previous.get(k) for k in ('dataset','attempts','first_seen','last_seen','_set_key')})
    return value


def _member(row):
    return {'key':row['issue_key'],'token':row['evidence_token'],
            'target':json.loads(row['target_json']),'resolution':json.loads(row['resolution_json']),
            **{k:str(row[k]) if row.get(k) is not None else None for k in ('attempts','first_seen','last_seen')}}


def pack(rows):
    """Group only identical source/subject/reason ranges; retain exact members."""
    groups=defaultdict(list);opaque=[]
    for row in rows:
        t=json.loads(row['target_json']);prefix=t.get('prefix',[])
        if t.get('scope_version')!='object-key-v1' or len(prefix)!=3:
            opaque.append(row);continue
        groups[(row['scope_key'],row['reason'],t.get('partition'),tuple(prefix[:2]),t.get('group'))].append(row)
    output=[]
    for identity,values in groups.items():
        values.sort(key=lambda r:r['issue_key'])
        scope,reason,partition,prefix,group=identity
        # One large subject can exceed a single JSON record. Split its exact
        # members into deterministic bounded sets rather than giving up and
        # retaining thousands of individual records. The logical denominator
        # and original metadata are unchanged in every chunk.
        header={'scope_version':VERSION,'partition':partition,'prefix':list(prefix),'group':group,
                'blocking':False,'affected_objects':0,'members':[]}
        allowance=65536-len(native_json(header).encode())-16
        chunks=[];chunk=[];size=0
        for row in values:
            item=_member(row);n=len(native_json(item).encode())+1
            if n>allowance:
                opaque.append(row);continue
            if chunk and (size+n>allowance or len(chunk)>=4096):
                chunks.append(chunk);chunk=[];size=0
            chunk.append((row,item));size+=n
        if chunk:chunks.append(chunk)
        for chunk in chunks:
            if len(chunk)<2 and not chunk[0][0].get('_set_key'):
                opaque.append(chunk[0][0]);continue
            target={**header,'blocking':any(item['target'].get('blocking',True) for _,item in chunk),
                    'affected_objects':len(chunk),'members':[item for _,item in chunk]}
            output.append(Issue('localset.'+digest([identity,chunk[0][0]['issue_key']]),scope,reason,digest(target),target,
                                {'retry':'same_local_pipeline','proof':'validate_each_exact_member','affected_objects':len(chunk)}))
    output.extend(Issue(r['issue_key'],r['scope_key'],r['reason'],r['evidence_token'],
                        json.loads(r['target_json']),json.loads(r['resolution_json'])) for r in opaque)
    validate_issue_changes(output,{})
    return tuple(output)


def fingerprint(rows):
    """Order independent evidence digest, including each original observation."""
    values=[digest([r['scope_key'],r['reason'],_member(r)]) for r in rows]
    return digest(sorted(values))


def compact_entry(store,entry,cancelled=None):
    """Atomic metadata-only regrouping; never touch files or source checkpoints."""
    import fcntl
    from .locking import _deadline
    removed=0;before_count=0;after_count=0;proof=hashlib.sha256();scopes=0
    with store.locks._hold(entry.spec.name,'pipeline',fcntl.LOCK_EX,_deadline(store.limits.lock_timeout_ms),cancelled):
        with store.catalog.transaction() as c:
            names=c.execute(text('SELECT DISTINCT scope_key FROM data_store_issues WHERE dataset=:d ORDER BY scope_key LIMIT 100001'),{'d':entry.spec.name}).scalars().all()
        if len(names)>100000:raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
        for scope in names:
            if cancelled and cancelled():raise DataStoreError('OPERATION_CANCELLED')
            with store.locks.writer(entry.spec.name,timeout_ms=store.limits.lock_timeout_ms) as guard:
                with guard.commit(timeout_ms=store.limits.lock_timeout_ms):
                    with store.catalog.transaction() as c:
                        size=c.execute(text('SELECT count(*),coalesce(sum(octet_length(target_json)+octet_length(resolution_json)),0) FROM data_store_issues WHERE dataset=:d AND scope_key=:s'),{'d':entry.spec.name,'s':scope}).one()
                        if size[0]>65536 or size[1]>16*1024*1024:raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
                        originals=c.execute(text('SELECT * FROM data_store_issues WHERE dataset=:d AND scope_key=:s ORDER BY issue_key FOR UPDATE'),{'d':entry.spec.name,'s':scope}).mappings().all()
                        logical=[v for r in originals for v in members(r)]
                        if len(logical)>65536:raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
                        packed=pack(logical)
                        if len(packed)>=len(originals):continue
                        # Opaque or already identical physical rows retain their
                        # attempts/first/last timestamps. Deleting and reinserting
                        # them would change evidence despite unchanged targets.
                        unchanged=set()
                        old_by_key={r['issue_key']:r for r in originals}
                        for issue in packed:
                            old=old_by_key.get(issue.key)
                            if old and (old['scope_key'],old['reason'],old['evidence_token'],
                                    json.loads(old['target_json']),json.loads(old['resolution_json'])) == (
                                    issue.scope,issue.reason,issue.evidence_token,dict(issue.target),dict(issue.resolution)):
                                unchanged.add(issue.key)
                        resolved={r['issue_key']:r['evidence_token'] for r in originals if r['issue_key'] not in unchanged}
                        additions=tuple(i for i in packed if i.key not in unchanged)
                        validate_issue_changes(additions,resolved)
                        before=fingerprint(logical)
                        store.catalog.change_issues(c,entry.spec.name,additions,resolved,check=lambda:None)
                        observed=c.execute(text('SELECT * FROM data_store_issues WHERE dataset=:d AND scope_key=:s ORDER BY issue_key'),{'d':entry.spec.name,'s':scope}).mappings().all()
                        expanded=[v for r in observed for v in members(r)]
                        if len(expanded)!=len(logical) or fingerprint(expanded)!=before:
                            raise DataStoreError('SOURCE_CONFLICT')
                        removed+=len(originals)-len(observed);before_count+=len(logical);after_count+=len(expanded)
                        proof.update(before.encode());scopes+=1
    return {'entry_id':entry.id,'complete':True,'compacted_scopes':scopes,
            'physical_records_released':removed,'affected_objects_before':before_count,
            'affected_objects_after':after_count,'lossless_evidence_sha256':proof.hexdigest(),
            'current_files_changed':False,'source_confirmation_changed':False}
