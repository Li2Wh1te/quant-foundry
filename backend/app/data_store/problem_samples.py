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
        spool.space.check(additional=len(content))
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
