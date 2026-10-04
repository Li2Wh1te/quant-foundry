"""Verify preserved v2 against an actual complete native read, without rebuilding.

The temporary index stores only expected keys, order and content fingerprints.
No output rows, files, checkpoints or issue resolutions are written. A receipt
is installed only after exact source/current disposition and generation checks.
"""
import fcntl
import hashlib
from itertools import islice
import json
import sqlite3
import time

import pyarrow.parquet as pq
import pyarrow as pa
from sqlalchemy import text

from .adapters.contracts import digest
from .adapters.normalize import normalize
from .adapters.canonical import NativeInputError
from .errors import DataStoreError
from .issue_sets import members
from .locking import _deadline, _scope_key
from .schema import DatasetSpec, _ordered
from .merge import value_hash
from .pipeline import read_entry_status,_status


def _string_member_prefix(encoded):
    # This is used ONLY after full typed-key validation of four string fields.
    # Their unchanged _ordered encoding terminates with NUL/NUL and escapes
    # embedded NUL as NUL/FF. Consume terminators FROM THE START, without overlap:
    # a backward search could misread a field terminator plus the next field's
    # leading escaped NUL. No caller value is re-encoded or cached here.
    first=encoded.find(b'\x00\x00')+2
    second=encoded.find(b'\x00\x00',first)+2
    third=encoded.find(b'\x00\x00',second)+2
    return encoded[:third]


class _CurrentComparison:
    """Bounded comparison against the SAME temporary authoritative index.

    At most 128 physical rows are buffered, except one already bounded complete
    report which is compared immediately. Callers flush after each Arrow batch,
    so no extra decoded business rows survive into the next batch. Only lookup
    and seen-marking are batched; all provenance, value and issue checks remain.
    """
    def __init__(self,db,counts,failure_disposed,conflict_disposed,check):
        self.db=db;self.counts=counts;self.failure_disposed=failure_disposed;self.check=check
        self.conflict_disposed=conflict_disposed
        self.pending=[];self.physical_rows=0

    def offer(self,key,rows):
        if not rows:return
        if self.pending and self.physical_rows+len(rows)>128:self.flush()
        self.pending.append((key,rows));self.physical_rows+=len(rows)
        if self.physical_rows>=128:self.flush()

    def flush(self):
        if not self.pending:return
        self.check(sample=True)
        placeholders=','.join('?' for _ in self.pending)
        # Omit only metadata unused by comparison. The complete expected index
        # retains partition/object/token fields for source reduction and receipt
        # construction; no input, value, key or disposition is omitted.
        selected=self.db.execute('SELECT k,r,s,g,n,t,h,state,stable_order,seen,conflicted FROM expected '
            'WHERE k IN ('+placeholders+')',[key for key,_ in self.pending]).fetchall()
        expected_by_key={row['k']:row for row in selected}
        marked=set()
        for key,rows in self.pending:
            self.check(sample=True)
            first=rows[0]
            # These provenance columns have already passed the nonnullable
            # string/int Arrow contract. Comparing the first row to itself adds
            # no check for those reflexive types. Every OTHER report member is
            # still compared across all four fields; the first confirmation is
            # checked against the expected source below, exactly as before.
            if len(rows)>1:
                fields=('basis_group','basis_ns','basis_token','basis_state')
                basis=tuple(first.get(k) for k in fields)
                if any(tuple(row.get(k) for k in fields)!=basis for row in islice(rows,1,None)):
                    raise DataStoreError('FILE_INVALID')
            self.counts['current_objects']+=1
            expected=expected_by_key.get(key)
            if not expected:self.counts['unexpected_objects']+=1;continue
            # SQLite's seen field fences prior batches; this set fences repeated
            # keys inside the current batch before its single bounded UPDATE.
            if expected['seen'] or key in marked:raise DataStoreError('KEY_ORDER_INVALID')
            marked.add(key)
            if expected['state']=='invalid':
                if (self.failure_disposed(expected) and
                        (not expected['conflicted'] or self.conflict_disposed(expected))):
                    self.counts['disposed_failures']+=1
                else:self.counts['mismatched_objects']+=1
            elif (first['basis_group']!=expected['g'] or
                    (first['basis_ns']!=expected['n'] if expected['stable_order'] else not 0<first['basis_ns']<=expected['n']) or
                    first['basis_state']!=expected['state'] or
                    expected['state']=='valid' and value_hash(rows)!=expected['h'] or
                    expected['conflicted'] and first['basis_token']!=expected['t']):
                self.counts['mismatched_objects']+=1
            elif expected['conflicted']:
                # A retained row is still checked against its own original
                # fingerprint. The competing category is disposed separately,
                # never substituted as the expected current identity.
                if self.conflict_disposed(expected):self.counts['disposed_failures']+=1
                else:self.counts['mismatched_objects']+=1
        if marked:
            self.db.execute('UPDATE expected SET seen=1 WHERE k IN ('+
                            ','.join('?' for _ in marked)+')',list(marked))
        self.pending.clear();self.physical_rows=0


def verify_existing(store,entry,sources,*,seconds=3600,cancelled=None):
    """A mismatch remains pending; reading existing files never proves input scope."""
    started=time.monotonic();counts={'source_rows':0,'expected_objects':0,'current_objects':0,
        'missing_objects':0,'mismatched_objects':0,'unexpected_objects':0,'disposed_failures':0,
        'queued_ranges':0,'scanned_objects':0,'current_files':0}
    phase='setup';phase_started=started;phase_seconds={}
    def enter_phase(next_phase):
        # Constant-size phase timings explain which full scan consumes the
        # existing deadline. They never alter its budget or receipt counters.
        nonlocal phase,phase_started
        now=time.monotonic()
        phase_seconds[phase]=phase_seconds.get(phase,0)+now-phase_started
        phase=next_phase;phase_started=now
    def measured_phases():
        now=time.monotonic()
        measured=dict(phase_seconds)
        measured[phase]=measured.get(phase,0)+now-phase_started
        return {key:round(value,6) for key,value in measured.items()}
    with store.locks._hold(entry.spec.name,'pipeline',fcntl.LOCK_EX,_deadline(store.limits.lock_timeout_ms),cancelled):
        with store.locks.read(entry.spec.name,timeout_ms=store.limits.lock_timeout_ms,cancelled=cancelled):
            dataset=store.catalog.dataset(entry.spec.name)
            store._spec_current(entry.spec,dataset)
            generation=dataset['generation']
            status=read_entry_status(store,entry.id)
            status['coverage_pending']=sorted(set(status.get('coverage_pending',[]))|{'native_current_verification'})
            _status(store,entry,status)
            with store.budget.reserve('read',pending='verify.'+entry.id,quota_bytes=store.limits.pipeline_spill_bytes,cancelled=cancelled) as space:
                space.deadline=started+seconds
                import os,stat
                path=space.spill/'coverage.sqlite'
                if path.exists() or path.is_symlink():
                    info=path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():raise DataStoreError('UNSAFE_STORAGE_PATH')
                    path.unlink()  # Only this verification's unsealed fingerprint index.
                fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_WRONLY,0o600);os.close(fd)
                db=sqlite3.connect(path);db.row_factory=sqlite3.Row
                # This index is never a continuation or a coverage receipt. A crash
                # discards it and repeats the authoritative snapshot; syncing it
                # once per native point adds no correctness and dominates scans.
                db.execute('PRAGMA journal_mode=OFF');db.execute('PRAGMA synchronous=OFF')
                db.execute('PRAGMA page_size=8192');db.execute('PRAGMA cache_size=-8192')
                db.execute('PRAGMA max_page_count='+str(space.quota//8192))
                db.executescript('CREATE TABLE queued(s TEXT,v TEXT,r TEXT,n INTEGER,PRIMARY KEY(s,v,r));CREATE TABLE expected(k BLOB PRIMARY KEY,p TEXT,r TEXT,s TEXT,o TEXT,g TEXT,n INTEGER,t TEXT,h TEXT,state TEXT,stable_order INTEGER,seen INTEGER DEFAULT 0,conflicted INTEGER DEFAULT 0);CREATE TABLE issues(k BLOB,g TEXT,n INTEGER,t TEXT,reason TEXT,PRIMARY KEY(k,g));CREATE TABLE conflicts(k BLOB PRIMARY KEY,g TEXT,n INTEGER,t TEXT,proof TEXT);')
                resource_due=started;resource_steps=0
                def check(*,sample=False):
                    nonlocal resource_due,resource_steps
                    if cancelled and cancelled():raise DataStoreError('OPERATION_CANCELLED')
                    now=time.monotonic()
                    if now-started>seconds:raise DataStoreError('QUERY_TIMEOUT')
                    resource_steps+=1
                    # Fingerprint writes have a hard SQLite max_page_count. On
                    # the scalar hot path, sample measured RSS/root/disk usage
                    # at most 128 checks or 50 ms apart, rather than reopening
                    # every filesystem ancestor for each point. Cancellation
                    # and time limits still run on EVERY source/object, including
                    # inside one large historical observation. File batches and
                    # phase boundaries retain their unconditional measurements.
                    if not sample or resource_steps>=128 or now>=resource_due:
                        space.check();resource_due=now+.05;resource_steps=0
                enter_phase('issues')
                try:
                    after=''
                    while True:
                        page=store.catalog.issues(entry.spec.name,limit=500,after=after)
                        for physical in page:
                            for issue in members(physical):
                                target=json.loads(issue['target_json']);prefix=target.get('prefix',[])
                                if target.get('scope_version')!='object-key-v1' or len(prefix) not in (2,3):continue
                                db.execute('INSERT INTO issues VALUES (?,?,?,?,?) ON CONFLICT(k,g) DO UPDATE '
                                    'SET n=excluded.n,t=excluded.t,reason=excluded.reason WHERE excluded.n>=issues.n',
                                    (entry.spec.key_bytes(tuple(prefix)),target.get('group'),int(target['order']),
                                     issue['evidence_token'],issue['reason']))
                            check()
                        if len(page)<500:break
                        after=page[-1]['issue_key']
                    enter_phase('capture_ranges');capture=False
                    if entry.source=='tushare' and entry.id in ('E69','E70'):
                        with store.catalog.transaction() as c:
                            capture=c.execute(text("SELECT version>=3 FROM data_store_capture_version WHERE singleton=1")).scalar_one()
                            if capture:
                                pending=c.execution_options(stream_results=True,yield_per=500).execute(text(
                                    'SELECT subject,variant,range_key,change_revision FROM data_store_source_ranges '
                                    'WHERE source=:s AND dataset=:d AND active IS NULL'),{'s':entry.source,'d':entry.native})
                                while True:
                                    rows=pending.fetchmany(500)
                                    if not rows:break
                                    check();db.executemany('INSERT INTO queued VALUES (?,?,?,?)',[tuple(row) for row in rows])
                                    counts['queued_ranges']+=len(rows)
                                db.commit()
                    enter_phase('native_snapshot');boundary=hashlib.sha256();memberships=None
                    for raw in sources.iter_entry(entry):
                        check(sample=True);counts['source_rows']+=1
                        if raw.catalog_memberships is not None:memberships=raw.catalog_memberships
                        boundary.update(digest([raw.source,raw.dataset,raw.subject,raw.variant,raw.token,raw.order_ns]).encode())
                        for unit in normalize(entry,raw):
                            check(sample=True)
                            counts['scanned_objects']+=1
                            state='invalid' if unit.failure or not unit.complete else 'withdrawn' if unit.withdrawn else 'valid'
                            h=value_hash(unit.rows) if state=='valid' else digest(state)
                            key=entry.spec.key_bytes(unit.key);part=entry.spec.partitioner((*unit.key,'root'))
                            old=db.execute('SELECT * FROM expected WHERE k=?',(key,)).fetchone()
                            if old:
                                if old['g']!=unit.group:
                                    proof = (unit.catalog_memberships.relation(unit.key,
                                             (old['g'], old['n'], old['t']),
                                             (unit.group, unit.order, unit.token))
                                             if unit.catalog_memberships is not None and
                                             state == 'valid' and old['state'] == 'valid' else None)
                                    if proof is not None and proof['winner']['group'] == old['g']:
                                        if old['conflicted']:
                                            db.execute('UPDATE conflicts SET proof=? WHERE k=?',(json.dumps(proof),key))
                                        continue
                                    if proof is None:
                                        if entry.id == 'E40':
                                            # Match MergeSpool: retain the first
                                            # original category winner and the
                                            # current per-object problem. Only
                                            # claims within the SAME competing
                                            # group have an order comparison.
                                            # Another group or a later valid
                                            # input cannot erase the conflict.
                                            conflict=db.execute('SELECT g,n FROM conflicts WHERE k=?',(key,)).fetchone()
                                            if conflict is None or conflict['g']!=unit.group or conflict['n']<=unit.order:
                                                db.execute('INSERT OR REPLACE INTO conflicts VALUES (?,?,?,?,NULL)',
                                                    (key,unit.group,unit.order,unit.token))
                                            db.execute('UPDATE expected SET conflicted=1 WHERE k=?',(key,))
                                            continue
                                        state='invalid';h=digest(state)
                                    elif old['conflicted']:
                                        db.execute('UPDATE conflicts SET proof=? WHERE k=?',(json.dumps(proof),key))
                                elif old['n']>unit.order:continue
                                elif old['n']==unit.order:
                                    if old['h']!=h:state='invalid';h=digest(state)
                                    elif old['t']>=unit.token:continue
                            db.execute('INSERT OR REPLACE INTO expected(k,p,r,s,o,g,n,t,h,state,stable_order,conflicted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                (key,part,*unit.key,unit.group,unit.order,unit.token,h,state,
                                 int(raw.order_kind!='current_table_snapshot'),old['conflicted'] if old else 0))
                        if counts['source_rows']%128==0:db.commit()
                    db.commit();check()
                    if not sources.summary.get('complete'):raise NativeInputError('LOCAL_SCAN_INCOMPLETE','完整覆盖核验的本地读取未完成。')
                    # Only blocked objects have an additional fingerprint here;
                    # this table shares the existing charged temporary index and
                    # is discarded with it. There is no success ledger or second
                    # store. Resolve after the complete native scan reaches the
                    # real positive head, preserving its original group/order/
                    # token. Both-category absence cannot supply this proof.
                    for pending in db.execute('SELECT e.*,c.g AS cg,c.n AS cn,c.t AS ct,c.proof '
                                              'FROM expected e JOIN conflicts c ON c.k=e.k'):
                        check(sample=True)
                        if pending['state']!='valid':continue
                        proof=json.loads(pending['proof']) if pending['proof'] else (
                            memberships.relation((pending['r'],pending['s'],pending['o']),
                                (pending['g'],pending['n'],pending['t']),
                                (pending['cg'],pending['cn'],pending['ct'])) if memberships is not None else None)
                        if proof is None:continue
                        winner,former=proof['winner'],proof['former']
                        if (pending['g'],pending['n'],pending['t'])!=(winner['group'],winner['order'],winner['token']):continue
                        covered=(pending['cg']==former['group'] and pending['cn']<former['before'] or
                            pending['cg']==winner['group'] and (pending['cn']<winner['order'] or
                                pending['cn']==winner['order'] and pending['ct']==winner['token']))
                        if covered:
                            db.execute('UPDATE expected SET conflicted=0 WHERE k=?',(pending['k'],))
                            db.execute('DELETE FROM conflicts WHERE k=?',(pending['k'],))
                    db.commit()
                    counts['expected_objects']=db.execute('SELECT count(*) FROM expected').fetchone()[0]
                    with store.catalog.transaction() as c:
                        parts=c.execute(text('SELECT DISTINCT partition_key FROM data_store_files WHERE dataset=:d ORDER BY partition_key'),{'d':entry.spec.name}).scalars().all()
                    enter_phase('current_files')
                    def failure_disposed(expected):
                        # A source-wide failure has no fabricated output object.
                        # Apply the same exact-key/scope, group and order fence
                        # whether a current row exists or not. This proves only
                        # disposition; check_coverage still rejects quality issues.
                        scope=entry.spec.key_bytes((expected['r'],expected['s']))
                        return db.execute('SELECT 1 FROM issues WHERE k IN (?,?) AND g=? AND n>=? LIMIT 1',
                            (expected['k'],scope,expected['g'],expected['n'])).fetchone() is not None
                    def conflict_disposed(expected):
                        # An unrelated scope refresh failure cannot stand in for
                        # this exact competing identity. Equal-order evidence
                        # must name the actual token; a newer issue is ordered
                        # only inside that same original competing group.
                        return db.execute('SELECT 1 FROM conflicts c JOIN issues i '
                            'ON i.k=c.k AND i.g=c.g WHERE c.k=? '
                            "AND i.reason='SOURCE_ORDER_UNCOMPARABLE' "
                            'AND (i.n>c.n OR i.n=c.n AND i.t=c.t) LIMIT 1',
                            (expected['k'],)).fetchone() is not None
                    comparison=_CurrentComparison(db,counts,failure_disposed,conflict_disposed,check)
                    suffix_fields=entry.spec.key[3:]
                    suffix_types=entry.spec._key_types[3:]
                    string_member=(len(entry.spec.key)==4 and
                        all(pa.types.is_string(kind) for kind in entry.spec._key_types))
                    for part in parts:
                        key=None;rows=[];last_key=None
                        for ref in store.catalog.files(entry.spec.name,part):
                            counts['current_files']+=1
                            contract=DatasetSpec.from_descriptor(json.loads(ref['contract_json']))
                            if (not entry.spec.accepts(contract) or ref['schema_id']!=contract.schema_id or
                                    ref['rule']!=contract.rule):raise DataStoreError('REBUILD_REQUIRED')
                            if ref['path'].split('/')[1:2]!=[_scope_key(entry.spec.name)]:raise DataStoreError('FILE_INVALID')
                            with store.files.open_object(ref['path']) as fd:
                                if os.fstat(fd).st_size!=ref['byte_count']:raise DataStoreError('FILE_INVALID')
                                hashed=hashlib.sha256()
                                while block:=os.read(fd,1024*1024):check();hashed.update(block)
                                if hashed.hexdigest()!=ref['content_hash']:raise DataStoreError('FILE_INVALID')
                                os.lseek(fd,0,os.SEEK_SET)
                                with os.fdopen(os.dup(fd),'rb') as handle,pq.ParquetFile(handle,page_checksum_verification=True) as file:
                                    if not file.schema_arrow.equals(contract.schema,check_metadata=True):raise DataStoreError('REBUILD_REQUIRED')
                                    if file.metadata.num_rows!=ref['row_count']:raise DataStoreError('FILE_INVALID')
                                    seen=0;first=None;file_last=None
                                    for batch in file.iter_batches(batch_size=1024):
                                        check()
                                        keys=contract.validate_batch(batch,max_rows=1024,max_bytes=128*1024*1024)
                                        entry.spec.check_partition(batch,part)
                                        if keys and last_key is not None and keys[0]<=last_key:raise DataStoreError('KEY_ORDER_INVALID')
                                        if keys:
                                            if first is None:first=keys[0]
                                            last_key=file_last=keys[-1]
                                        seen+=batch.num_rows
                                        for encoded,row in zip(keys,batch.to_pylist()):
                                            # Full typed keys were validated above.
                                            # Remove their EXACT ordered suffix to
                                            # reuse the three-field object prefix;
                                            # NUL escaping and temporal/Decimal
                                            # encoding still use _ordered unchanged.
                                            if string_member:current=_string_member_prefix(encoded)
                                            else:
                                                suffix=b''.join(_ordered(row[name],kind)
                                                    for name,kind in zip(suffix_fields,suffix_types))
                                                current=encoded[:-len(suffix)] if suffix else encoded
                                            if key is not None and current!=key:comparison.offer(key,rows);rows=[]
                                            key=current;rows.append(row)
                                            if len(rows)>100000:raise DataStoreError('BATCH_BUDGET_EXCEEDED')
                                        comparison.flush()
                                    if (seen!=ref['row_count'] or first!=bytes(ref['key_min']) or
                                            file_last!=bytes(ref['key_max'])):raise DataStoreError('FILE_INVALID')
                        comparison.offer(key,rows);comparison.flush();db.commit()
                    enter_phase('missing_objects')
                    for row in db.execute('SELECT * FROM expected WHERE seen=0'):
                        if (row['state']=='invalid' and failure_disposed(row) and
                                (not row['conflicted'] or conflict_disposed(row))):
                            counts['disposed_failures']+=1;continue
                        counts['missing_objects']+=1
                    check()
                    enter_phase('final_fences')
                    if store.catalog.dataset(entry.spec.name)['generation']!=generation:raise DataStoreError('DATA_CHANGED')
                    complete=not any(counts[k] for k in ('missing_objects','mismatched_objects','unexpected_objects'))
                    status=read_entry_status(store,entry.id)
                    if complete:
                        enter_phase('receipt')
                        expected_parts=[r[0] for r in db.execute('SELECT DISTINCT p FROM expected ORDER BY p')]
                        h=hashlib.sha256()
                        for part in expected_parts:h.update(digest(part).encode())
                        proof={'version':1,'entry_id':entry.id,'contract':digest(entry.spec.descriptor()),
                            'source_selection':sources.selection_key() if hasattr(sources,'selection_key') else None,'source_kind':digest([type(sources).__module__,type(sources).__qualname__]),
                            'boundary':boundary.hexdigest(),'boundary_kind':'verified_native_current_fingerprints',
                            'verification':dict(counts),'range_digest':h.hexdigest(),'expected':len(expected_parts),
                            'end':expected_parts[-1] if expected_parts else '', 'after':expected_parts[-1] if expected_parts else '',
                            'scan_complete':True,'complete':True,'remaining':0,'generation':generation}
                        if capture:
                            enter_phase('acknowledge')
                            # Each captured range revision was read before the
                            # authoritative native snapshot. A late writer changes
                            # its revision even while pending was already true.
                            # Delete only unchanged, actually verified ranges;
                            # active work and unseen concurrent commits survive.
                            with sources.engine.begin() as c:
                                selected=db.execute('SELECT * FROM queued ORDER BY s,v,r')
                                # Bounded set deletion retains every exact
                                # revision predicate. It shares the transaction
                                # with the final receipt; late source commits or
                                # active work cannot be acknowledged accidentally.
                                delete=text('DELETE FROM data_store_source_ranges q USING '
                                    'jsonb_to_recordset(CAST(:batch AS jsonb)) AS b(s text,v text,r text,n bigint) '
                                    'WHERE q.source=:s AND q.dataset=:d AND q.subject=b.s '
                                    'AND q.variant=b.v AND q.range_key=b.r AND q.change_revision=b.n AND q.active IS NULL')
                                while True:
                                    batch=selected.fetchmany(128)
                                    if not batch:break
                                    check()
                                    from .values import control_json
                                    c.execute(delete,{'s':entry.source,'d':entry.native,
                                        'batch':control_json([dict(row) for row in batch])})
                                remaining=c.execute(text('SELECT count(*) FROM data_store_source_ranges WHERE source=:s AND dataset=:d'),{'s':entry.source,'d':entry.native}).scalar_one()
                                status['incremental']=dict(status.get('incremental',{}),version=1,contract=digest(entry.spec.descriptor()),
                                    remaining_ranges=remaining,bootstrap_complete=not remaining,
                                    bootstrap_remaining_ranges=remaining,bootstrap_boundary=proof['boundary'])
                                proof['captured_ranges']=db.execute('SELECT count(*) FROM queued').fetchone()[0]
                                status.update(full_coverage=proof,last_complete_coverage=proof,coverage_pending=[])
                                from .values import control_json
                                c.execute(text('UPDATE data_store_entry_status SET summary_json=:j,updated_at=clock_timestamp() WHERE entry_id=:i'),
                                          {'j':control_json(status),'i':entry.id})
                        else:
                            status.update(full_coverage=proof,last_complete_coverage=proof,coverage_pending=[])
                            _status(store,entry,status)
                        # check_coverage still rejects any pending native queue or
                        # unresolved quality. No complete/qualified flag is changed.
                    return {'entry_id':entry.id,'complete':complete,'reason':None if complete else 'CURRENT_SOURCE_MISMATCH',
                        'generation':generation,**counts,'seconds':round(time.monotonic()-started,3),
                        'phase_seconds':measured_phases(),'current_files_changed':False}
                except BaseException as error:
                    # Bounded counters identify the failed phase without leaking
                    # source keys, payloads, SQL, paths or exception messages.
                    if isinstance(error,sqlite3.Error):
                        error=DataStoreError('SCRATCH_BUDGET_EXCEEDED' if getattr(error,'sqlite_errorcode',None)==sqlite3.SQLITE_FULL else 'FILE_INVALID')
                        error.verification=dict(counts,phase=phase,seconds=round(time.monotonic()-started,3),phase_seconds=measured_phases())
                        raise error from None
                    error.verification=dict(counts,phase=phase,seconds=round(time.monotonic()-started,3),phase_seconds=measured_phases())
                    raise
                finally:db.close();space.discard=True
