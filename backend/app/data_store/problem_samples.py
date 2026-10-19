"""One bounded current exception file per static entry, never business payloads."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from .errors import DataStoreError
from .adapters.contracts import native_json

MAX_FILE=4*1024*1024
MAX_TOTAL=64*1024*1024
NAME=re.compile(r'(e[0-9]{2}|b05(-m)?)\.jsonl\Z')


def write_overflow(store,entry,partition,spool):
    records=[];size=0;count=0;blocking=0;complete=True
    for row,restricted,fixed in spool.problem_records(partition):
        if fixed:continue
        count+=1;blocking+=restricted
        record={'key':hashlib.sha256(bytes(row['k'])).hexdigest(),'group':row['g'],
                'order':str(row['n']),'token':row['t'],'reason':row['reason'],'blocking':restricted}
        encoded=native_json(record)+'\n'
        if size+len(encoded.encode())<=MAX_FILE-4096:
            records.append(encoded);size+=len(encoded.encode())
        else:complete=False
    header=native_json({'format':'qf-current-errors-v1','entry':entry.id,'partition':partition,
                        'affected_objects':count,'blocking_objects':blocking,'complete_key_list':complete})+'\n'
    content=(header+''.join(records)).encode()
    name=entry.id.lower()+'.jsonl'
    if not NAME.fullmatch(name):raise DataStoreError('INVALID_VALUE')
    with store.files.directory('.problem_samples',create=True) as fd:
        total=0
        with os.scandir(fd) as files:
            for f in files:
                if f.name==name or f.name==name+'.pending':continue
                s=f.stat(follow_symlinks=False)
                if not NAME.fullmatch(f.name) or not stat.S_ISREG(s.st_mode):
                    raise DataStoreError('UNSAFE_STORAGE_PATH')
                total+=s.st_size
        if total+len(content)>MAX_TOTAL:raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
        # This file has its own finite retention budget and is outside scratch.
        # A read reservation has zero staging allowance; charging this control
        # file as read staging would always mask ISSUE_BUDGET_EXCEEDED with an
        # unrelated scratch error, even for a one-byte sample.
        spool.space.check()
        filesystem=os.fstatvfs(fd)
        if filesystem.f_bavail*filesystem.f_frsize-len(content)<store.limits.minimum_free_bytes:
            raise DataStoreError('DISK_PRESSURE')
        pending=name+'.pending'
        try:os.unlink(pending,dir_fd=fd)
        except FileNotFoundError:pass
        handle=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        try:
            with os.fdopen(handle,'wb',closefd=False) as f:f.write(content);f.flush();os.fsync(handle)
        finally:os.close(handle)
        os.replace(pending,name,src_dir_fd=fd,dst_dir_fd=fd);os.fsync(fd)
    return {'partition':partition,'file':name,'sha256':hashlib.sha256(content).hexdigest(),
            'affected_objects':count,'blocking_objects':blocking,'complete_key_list':complete}


def _resolved_overflow(store,entry,partition,spool,proof):
    """A small retry never clears an overflow by fixing unrelated objects."""
    if proof.get('partition')!=partition or not proof.get('complete_key_list'):return False
    name=proof.get('file','')
    if name!=entry.id.lower()+'.jsonl' or not NAME.fullmatch(name):return False
    with store.files.directory('.problem_samples') as fd:
        handle=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode) or os.fstat(handle).st_size>MAX_FILE:return False
            with os.fdopen(handle,'rb',closefd=False) as f:data=f.read(MAX_FILE+1)
        finally:os.close(handle)
    if hashlib.sha256(data).hexdigest()!=proof.get('sha256'):return False
    required={}
    for line in data.splitlines()[1:]:
        r=json.loads(line)
        if r['blocking']:required[r['key']]=r
    for winner in spool.db.execute("SELECT k,g,n,state,validated FROM objects WHERE p=?",(partition,)):
        key=hashlib.sha256(bytes(winner['k'])).hexdigest();r=required.get(key)
        if (r and winner['validated'] and winner['state']!='invalid' and winner['g']==r['group']
                and winner['n']>=int(r['order'])):required.pop(key)
    return not required


def resolved_overflow(store,entry,partition,spool,proof):
    try:
        return _resolved_overflow(store,entry,partition,spool,proof)
    except (OSError,ValueError,KeyError,TypeError,DataStoreError):
        # Missing/tampered evidence never clears a current restriction.
        return False


def overflow_restricts_query(store,spec,query,proof,check=lambda:None):
    """Narrow only a verified complete collection and one provable object.

    The existing format contains hashes of full pipeline object keys, not
    ordered plaintext bounds. It can prove disjointness for an exact object
    (including a member subrange), but cannot prove an arbitrary multi-object
    interval empty. Such intervals retain their original conservative gate.
    Every call reopens and verifies the current receipt; no file/quality result
    is cached across pages or the API's final quality fence.
    """
    lower,upper=query.lower,query.upper
    if (not lower or not upper or len(lower)<3 or len(upper)<3
            or lower[:2]!=upper[:2]
            or not all(type(v) is str for v in (*lower[:3],*upper[:3]))):
        return True
    if not ((upper[:3]==lower[:3] and len(upper)>3) or
            (len(upper)==3 and upper[2]==lower[2]+'\x00')):
        return True
    entry_id=spec.semantics.get('entry_id','')
    name=proof.get('file')
    if (type(entry_id) is not str or not re.fullmatch(r'E[0-9]{2}',entry_id)
            or name!=entry_id.lower()+'.jsonl' or not NAME.fullmatch(name)
            or proof.get('complete_key_list') is not True
            or type(proof.get('partition')) is not str or not proof['partition']
            or type(proof.get('sha256')) is not str
            or not re.fullmatch(r'[0-9a-f]{64}',proof['sha256'])
            or any(type(proof.get(k)) is not int or proof[k]<0
                   for k in ('affected_objects','blocking_objects'))):
        return True
    check()
    try:
        with store.files.directory('.problem_samples') as directory:
            handle=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            try:
                info=os.fstat(handle)
                if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_FILE:return True
                with os.fdopen(handle,'rb',closefd=False) as file:data=file.read(MAX_FILE+1)
            finally:os.close(handle)
        if len(data)>MAX_FILE or hashlib.sha256(data).hexdigest()!=proof['sha256']:return True
        lines=iter(data.splitlines());header=json.loads(next(lines))
        if (header.get('format')!='qf-current-errors-v1' or header.get('entry')!=entry_id
                or header.get('complete_key_list') is not True
                or any(type(header.get(k)) is not int for k in ('affected_objects','blocking_objects'))
                or any(header.get(k)!=proof.get(k) for k in
                       ('partition','affected_objects','blocking_objects'))):
            return True
        keys=set();blocked=set();count=0
        for line in lines:
            check()
            record=json.loads(line)
            if (set(record)!={'key','group','order','token','reason','blocking'}
                    or type(record['blocking']) is not bool
                    or any(type(record[k]) is not str or not re.fullmatch(r'[0-9a-f]{64}',record[k])
                           for k in ('key','group','token'))
                    or type(record['order']) is not str or not record['order'].isdecimal()
                    or len(record['order'])>20
                    or type(record['reason']) is not str or not record['reason']
                    or record['key'] in keys):
                return True
            keys.add(record['key']);count+=1
            if record['blocking']:blocked.add(record['key'])
        if count!=proof['affected_objects'] or len(blocked)!=proof['blocking_objects']:return True
        # A failed acquisition uses the same codec with object_key=unlocated;
        # it restricts this entire representation/subject, not a literal row
        # named "unlocated". Include broader prefix identities defensively.
        requested={hashlib.sha256(spec.key_bytes(tuple(key))).hexdigest()
                   for key in (lower[:1],lower[:2],lower[:3],(*lower[:2],'unlocated'))}
        requested.add(hashlib.sha256(b'').hexdigest())
        return bool(blocked & requested)
    except DataStoreError as error:
        # Filesystem failures cannot become an unproved empty collection, but
        # cancellation/resource failures must retain their original outcome.
        if error.code not in {'UNSAFE_STORAGE_PATH','STORAGE_UNAVAILABLE','FILE_INVALID','INVALID_VALUE'}:raise
        return True
    except (OSError,ValueError,KeyError,TypeError,AttributeError,StopIteration,RecursionError):
        return True
