"""Temporary bounded current-winner reduction, not a second formal database.

SQLite here is an external-memory working file inside a charged D01 spill slot.
It contains at most the selected current partitions, not source success/history
rows. It is deleted on context exit and recovered by the kernel after a crash.
The only durable successful facts are typed Parquet rows and current checkpoints.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
import sqlite3

import pyarrow as pa

from .errors import DataStoreError
from .adapters.contracts import Unit, digest, native_json, loads
from .adapters.canonical import NativeInputError


def restore_row(schema, row):
    out={}
    for field in schema:
        value=row.get(field.name)
        if value is not None:
            if pa.types.is_decimal(field.type): value=Decimal(value)
            elif pa.types.is_date(field.type) and type(value) is str: value=date.fromisoformat(value)
            elif pa.types.is_timestamp(field.type) and type(value) is str: value=datetime.fromisoformat(value)
            elif pa.types.is_integer(field.type) and type(value) is str: value=int(value)
        out[field.name]=value
    return out


def value_hash(rows):
    # Canonical model fields, excluding provenance/confirmation and null columns
    # from unrelated node types in the declared scalar union schema.
    from .adapters.canonical import encode
    clean=[{k:v for k,v in row.items() if (k in ('member_key','row_kind','quality_json') or k.startswith('f')) and v is not None}
           for row in rows]
    return hashlib.sha256(encode(clean).encode()).hexdigest()


class MergeSpool:
    def __init__(self, space, spec, *, after='', partitions=None, partition_count=1):
        self.space,self.spec=space,spec
        self.after,self.only,self.count=after,set(partitions) if partitions else None,partition_count
        self.parts=set(); self.discarded=False; self.offered=0; self.old_invalid=0
        path=space.spill/'lfd02-merge.sqlite'
        self.db=sqlite3.connect(path)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA temp_store=MEMORY')
        self.db.execute('PRAGMA cache_size=-4096')
        self.db.execute('PRAGMA mmap_size=0')
        self.db.execute('PRAGMA page_size=4096')
        self.db.execute('PRAGMA max_page_count='+str(max(16,space.spill_limit//8192)))
        # Reserve half the spill bound for the rollback journal. Inserts are
        # committed in small batches; no unbounded transaction/journal growth.
        self.db.executescript('''
          CREATE TABLE objects(k BLOB PRIMARY KEY,p TEXT NOT NULL,r TEXT NOT NULL,s TEXT NOT NULL,o TEXT NOT NULL,
            g TEXT NOT NULL,n INTEGER NOT NULL,t TEXT NOT NULL,state TEXT NOT NULL,h TEXT NOT NULL,body TEXT NOT NULL,
            validated INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID;
          CREATE INDEX objects_partition ON objects(p,k);
          CREATE TABLE problems(k BLOB PRIMARY KEY,p TEXT NOT NULL,r TEXT NOT NULL,s TEXT NOT NULL,o TEXT NOT NULL,
            g TEXT NOT NULL,n INTEGER NOT NULL,t TEXT NOT NULL,reason TEXT NOT NULL) WITHOUT ROWID;
          CREATE INDEX problems_partition ON problems(p,k);
          CREATE TABLE scopes(r TEXT NOT NULL,s TEXT NOT NULL,g TEXT NOT NULL,n INTEGER NOT NULL,t TEXT NOT NULL,
            PRIMARY KEY(r,s)) WITHOUT ROWID;
        ''')
        self.db.set_progress_handler(self._progress,10000)
        self._interrupt=None

    def _progress(self):
        try: self.space.check()
        except Exception as exc:
            self._interrupt=exc; return 1
        return 0

    def close(self):
        self.db.close()

    def check(self):
        if self._interrupt: raise self._interrupt
        self.space.check()

    def _select(self, partition):
        if partition<=self.after or self.only is not None and partition not in self.only:
            return False
        if partition in self.parts: return True
        if self.only is not None or len(self.parts)<self.count:
            self.parts.add(partition); return True
        largest=max(self.parts)
        self.discarded=True
        if partition>largest: return False
        self.db.execute('DELETE FROM objects WHERE p=?',(largest,))
        self.db.execute('DELETE FROM problems WHERE p=?',(largest,))
        self.parts.remove(largest); self.parts.add(partition)
        return True

    def note_problem(self, unit, partition):
        k=self.spec.key_bytes(unit.key)
        old=self.db.execute('SELECT n,g,t,reason FROM problems WHERE k=?',(k,)).fetchone()
        if old and old['g']==unit.group and old['n']>unit.order:
            return
        self.db.execute('INSERT OR REPLACE INTO problems VALUES (?,?,?,?,?,?,?,?,?)',
                        (k,partition,*unit.key,unit.group,unit.order,unit.token,unit.failure))

    def validate_scope(self, source):
        old=self.db.execute('SELECT g,n FROM scopes WHERE r=? AND s=?',
                            (source.representation_key,source.subject)).fetchone()
        if old is None or (old['g']==source.group and old['n']<=source.order_ns):
            self.db.execute('INSERT OR REPLACE INTO scopes VALUES (?,?,?,?,?)',
                            (source.representation_key,source.subject,source.group,source.order_ns,source.token))

    def offer(self, unit: Unit, *, current=False):
        if not unit.complete:
            unit=replace(unit,failure='REPORT_INCOMPLETE')
        k=self.spec.key_bytes(unit.key)
        partition=self.spec.partitioner((*unit.key,'root'))
        if not self._select(partition): return
        self.offered+=1
        old=self.db.execute('SELECT * FROM objects WHERE k=?',(k,)).fetchone()
        if unit.failure:
            if not current: self.note_problem(unit,partition)
            if unit.object_key=='unlocated':
                # A scope problem has no fabricated business row.
                return
        if old and old['g']!=unit.group:
            self.note_problem(replace(unit,failure='SOURCE_ORDER_UNCOMPARABLE'),partition)
            return
        if old and old['n']>unit.order:
            self.old_invalid+=bool(unit.failure)
            return
        state='invalid' if unit.failure else 'withdrawn' if unit.withdrawn else 'valid'
        rows=unit.rows
        hashed=value_hash(rows) if state=='valid' else digest(state)
        if old and old['n']==unit.order:
            if old['t']==unit.token:
                if current:
                    # A fresh successful rule re-evaluation of this very input
                    # can replace its former invalid current marker, not vice versa.
                    return
                if old['state']=='valid' and state=='valid' and old['h']!=hashed:
                    # Reprocessing under the registered rule is allowed; the
                    # pipeline never interprets incompatible old bytes as new.
                    pass
                elif old['state']==state and old['h']==hashed:
                    if not current:
                        self.db.execute('UPDATE objects SET validated=1 WHERE k=?',(k,))
                    return
            elif old['state']==state and old['h']==hashed:
                # No ordering claim from an ID/token: equal time/equal value is
                # equivalent. A deterministic token makes rescans idempotent.
                if old['t']>=unit.token: return
            else:
                # Equal-authority disagreement must not resolve by file/UUID order.
                token=digest(sorted([old['t'],unit.token]))
                unit=replace(unit,token=token,failure='SOURCE_ORDER_CONFLICT')
                self.note_problem(unit,partition)
                state='invalid'; hashed=digest(state)
        if state=='invalid' and old and old['body']!='[]':
            # Optional old display values stay explicitly INVALID and cannot be
            # returned by qualified business reads. Their old basis is not kept.
            body=old['body']
        else:
            body=native_json(list(rows))
        if len(body.encode())>self.spec.semantics.get('max_object_bytes',32*1024*1024):
            raise DataStoreError('BATCH_BUDGET_EXCEEDED')
        self.db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (k,partition,*unit.key,unit.group,unit.order,unit.token,state,hashed,body,
                         int(not current and state!='invalid')))
        if self.offered%200==0:
            self.db.commit(); self.check()

    def rows(self, partition):
        for record in self.db.execute('SELECT * FROM objects WHERE p=? ORDER BY k',(partition,)):
            payload=loads(record['body'])
            if not payload:
                payload=[{'member_key':'root','row_kind':-1}]
            for row in sorted(payload,key=lambda v:v['member_key']):
                yield restore_row(self.spec.schema, {**row,'representation':record['r'],'subject':record['s'],
                    'object_key':record['o'],'basis_ns':record['n'],'basis_group':record['g'],
                    'basis_token':record['t'],'basis_valid':record['state']=='valid',
                    'basis_state':record['state'],'value_hash':record['h']})
            self.check()

    def batches(self, partition, *, rows, nbytes):
        size=min(rows,self.spec.bounded_rows(rows,nbytes),1024)
        batch=[]
        for row in self.rows(partition):
            batch.append(row)
            if len(batch)>=size:
                yield pa.RecordBatch.from_pylist(batch,schema=self.spec.schema); batch=[]
        if batch: yield pa.RecordBatch.from_pylist(batch,schema=self.spec.schema)

    def partition_token(self, partition):
        h=hashlib.sha256()
        for r in self.db.execute('SELECT k,g,n,t,state,h FROM objects WHERE p=? ORDER BY k',(partition,)):
            h.update(bytes(r['k'])); h.update(native_json(list(r)[1:]).encode()); h.update(b'\0')
        for r in self.db.execute('SELECT k,g,n,t,reason FROM problems WHERE p=? ORDER BY k',(partition,)):
            h.update(bytes(r['k']));h.update(native_json(list(r)[1:]).encode());h.update(b'\0')
        self.check()
        return h.hexdigest()

    def problem_records(self, partition):
        self.db.commit()
        for r in self.db.execute('SELECT * FROM problems WHERE p=? ORDER BY k',(partition,)):
            winner=self.db.execute('SELECT * FROM objects WHERE k=?',(r['k'],)).fetchone()
            blocking=(winner is None or winner['g']!=r['g'] or winner['n']<=r['n'] and
                      (winner['state']=='invalid' or winner['t']!=r['t']))
            # A successfully revalidated identical input fixes its old conversion
            # error. A new, different input fixes only this complete object.
            resolved=bool(winner and winner['g']==r['g'] and winner['state']!='invalid' and
                          winner['n']>=r['n'] and winner['validated'])
            yield dict(r),blocking,resolved
