"""Verify preserved v2 against an actual complete native read, without rebuilding.

The temporary index stores only expected keys, order and content fingerprints.
No output rows, files, checkpoints or issue resolutions are written. A receipt
is installed only after exact source/current disposition and generation checks.
"""
import fcntl
import hashlib
import json
import sqlite3
import time

import pyarrow.parquet as pq
from sqlalchemy import text

from .adapters.contracts import digest
from .adapters.normalize import normalize
from .adapters.canonical import NativeInputError
from .errors import DataStoreError
from .issue_sets import members
from .locking import _deadline, _scope_key
from .schema import DatasetSpec
from .merge import value_hash
from .pipeline import read_entry_status,_status


def verify_existing(store,entry,sources,*,seconds=3600,cancelled=None):
    """A mismatch remains pending; reading existing files never proves input scope."""
    started=time.monotonic();counts={'source_rows':0,'expected_objects':0,'current_objects':0,
        'missing_objects':0,'mismatched_objects':0,'unexpected_objects':0,'disposed_failures':0}
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
                db.execute('PRAGMA journal_mode=OFF');db.execute('PRAGMA cache_size=-8192')
                db.execute('PRAGMA max_page_count='+str(space.quota//8192))
                db.executescript('CREATE TABLE queued(s TEXT,v TEXT,r TEXT,n INTEGER,PRIMARY KEY(s,v,r));CREATE TABLE expected(k BLOB PRIMARY KEY,p TEXT,r TEXT,s TEXT,o TEXT,g TEXT,n INTEGER,t TEXT,h TEXT,state TEXT,stable_order INTEGER,seen INTEGER DEFAULT 0);CREATE TABLE issues(k BLOB,g TEXT,n INTEGER,PRIMARY KEY(k,g));')
                def check():
                    if cancelled and cancelled():raise DataStoreError('OPERATION_CANCELLED')
                    if time.monotonic()-started>seconds:raise DataStoreError('QUERY_TIMEOUT')
                    space.check()
                try:
                    after=''
                    while True:
                        page=store.catalog.issues(entry.spec.name,limit=500,after=after)
                        for physical in page:
                            for issue in members(physical):
                                target=json.loads(issue['target_json']);prefix=target.get('prefix',[])
                                if target.get('scope_version')!='object-key-v1' or len(prefix) not in (2,3):continue
                                db.execute('INSERT INTO issues VALUES (?,?,?) ON CONFLICT(k,g) DO UPDATE SET n=max(n,excluded.n)',
                                    (entry.spec.key_bytes(tuple(prefix)),target.get('group'),int(target['order'])))
                            check()
                        if len(page)<500:break
                        after=page[-1]['issue_key']
                    capture=False
                    if entry.source=='tushare' and entry.id in ('E69','E70'):
                        with store.catalog.transaction() as c:
                            capture=c.execute(text("SELECT version>=3 FROM data_store_capture_version WHERE singleton=1")).scalar_one()
                            if capture:
                                pending=c.execution_options(stream_results=True,yield_per=500).execute(text(
                                    'SELECT subject,variant,range_key,change_revision FROM data_store_source_ranges '
                                    'WHERE source=:s AND dataset=:d AND active IS NULL'),{'s':entry.source,'d':entry.native})
                                for row in pending:check();db.execute('INSERT INTO queued VALUES (?,?,?,?)',tuple(row))
                    boundary=hashlib.sha256()
                    for raw in sources.iter_entry(entry):
                        check();counts['source_rows']+=1
                        boundary.update(digest([raw.source,raw.dataset,raw.subject,raw.variant,raw.token,raw.order_ns]).encode())
                        for unit in normalize(entry,raw):
                            state='invalid' if unit.failure or not unit.complete else 'withdrawn' if unit.withdrawn else 'valid'
                            h=value_hash(unit.rows) if state=='valid' else digest(state)
                            key=entry.spec.key_bytes(unit.key);part=entry.spec.partitioner((*unit.key,'root'))
                            old=db.execute('SELECT * FROM expected WHERE k=?',(key,)).fetchone()
                            if old:
                                if old['g']!=unit.group:
                                    state='invalid';h=digest(state)
                                elif old['n']>unit.order:continue
                                elif old['n']==unit.order:
                                    if old['h']!=h:state='invalid';h=digest(state)
                                    elif old['t']>=unit.token:continue
                            db.execute('INSERT OR REPLACE INTO expected(k,p,r,s,o,g,n,t,h,state,stable_order) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                (key,part,*unit.key,unit.group,unit.order,unit.token,h,state,int(raw.order_kind!='current_table_snapshot')))
                        db.commit()
                    if not sources.summary.get('complete'):raise NativeInputError('LOCAL_SCAN_INCOMPLETE','完整覆盖核验的本地读取未完成。')
                    counts['expected_objects']=db.execute('SELECT count(*) FROM expected').fetchone()[0]
                    with store.catalog.transaction() as c:
                        parts=c.execute(text('SELECT DISTINCT partition_key FROM data_store_files WHERE dataset=:d ORDER BY partition_key'),{'d':entry.spec.name}).scalars().all()
                    def compare(rows):
                        if not rows:return
                        first=rows[0]
                        if any(any(row.get(k)!=first.get(k) for k in ('basis_group','basis_ns','basis_token','basis_state')) for row in rows):raise DataStoreError('FILE_INVALID')
                        key=entry.spec.key_bytes(tuple(first[k] for k in entry.spec.key[:3]));counts['current_objects']+=1
                        expected=db.execute('SELECT * FROM expected WHERE k=?',(key,)).fetchone()
                        if not expected:counts['unexpected_objects']+=1;return
                        if expected['seen']:raise DataStoreError('KEY_ORDER_INVALID')
                        db.execute('UPDATE expected SET seen=1 WHERE k=?',(key,))
                        # Invalid originals are disposed only by explicit, current
                        # exact-key/scope evidence. They remain quality restrictions.
                        if expected['state']=='invalid':
                            issue=db.execute('SELECT 1 FROM issues WHERE k=? AND g=? AND n>=?',(key,expected['g'],expected['n'])).fetchone()
                            scope=entry.spec.key_bytes((expected['r'],expected['s']))
                            if not issue:issue=db.execute('SELECT 1 FROM issues WHERE k=? AND g=? AND n>=?',(scope,expected['g'],expected['n'])).fetchone()
                            if issue:counts['disposed_failures']+=1
                            else:counts['mismatched_objects']+=1
                        elif (first['basis_group']!=expected['g'] or
                              (first['basis_ns']!=expected['n'] if expected['stable_order'] else not 0<first['basis_ns']<=expected['n']) or
                              first['basis_state']!=expected['state'] or
                              expected['state']=='valid' and value_hash(rows)!=expected['h']):counts['mismatched_objects']+=1
                    for part in parts:
                        key=None;rows=[];last_key=None
                        for ref in store.catalog.files(entry.spec.name,part):
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
                                        for row in batch.to_pylist():
                                            current=tuple(row[k] for k in entry.spec.key[:3])
                                            if key is not None and current!=key:compare(rows);rows=[]
                                            key=current;rows.append(row)
                                            if len(rows)>100000:raise DataStoreError('BATCH_BUDGET_EXCEEDED')
                                    if (seen!=ref['row_count'] or first!=bytes(ref['key_min']) or
                                            file_last!=bytes(ref['key_max'])):raise DataStoreError('FILE_INVALID')
                        compare(rows);db.commit()
                    for row in db.execute('SELECT * FROM expected WHERE seen=0'):
                        if row['state']=='invalid':
                            evidence=db.execute('SELECT 1 FROM issues WHERE k=? AND g=? AND n>=?',(row['k'],row['g'],row['n'])).fetchone()
                            if evidence:counts['disposed_failures']+=1;continue
                        counts['missing_objects']+=1
                    check()
                    if store.catalog.dataset(entry.spec.name)['generation']!=generation:raise DataStoreError('DATA_CHANGED')
                    complete=not any(counts[k] for k in ('missing_objects','mismatched_objects','unexpected_objects'))
                    status=read_entry_status(store,entry.id)
                    if complete:
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
                            # Each captured range revision was read before the
                            # authoritative native snapshot. A late writer changes
                            # its revision even while pending was already true.
                            # Delete only unchanged, actually verified ranges;
                            # active work and unseen concurrent commits survive.
                            with sources.engine.begin() as c:
                                selected=db.execute('SELECT * FROM queued ORDER BY s,v,r')
                                delete=text('DELETE FROM data_store_source_ranges WHERE source=:s AND dataset=:d '
                                    'AND subject=:sub AND variant=:v AND range_key=:r AND change_revision=:n AND active IS NULL')
                                while True:
                                    batch=selected.fetchmany(500)
                                    if not batch:break
                                    check()
                                    c.execute(delete,[{'s':entry.source,'d':entry.native,'sub':row['s'],'v':row['v'],'r':row['r'],'n':row['n']} for row in batch])
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
                        'generation':generation,**counts,'seconds':round(time.monotonic()-started,3),'current_files_changed':False}
                except sqlite3.Error as error:
                    raise DataStoreError('SCRATCH_BUDGET_EXCEEDED' if getattr(error,'sqlite_errorcode',None)==sqlite3.SQLITE_FULL else 'FILE_INVALID') from None
                finally:db.close();space.discard=True
