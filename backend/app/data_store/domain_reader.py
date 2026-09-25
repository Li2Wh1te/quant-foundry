"""D04 internal business-object reader, using only declared registry contracts."""
from .adapters.registry import BY_ID
from .errors import DataStoreError
from .readers import Query
from .merge import restore_row
from .adapters.contracts import native_json, loads


def read_object(store, entry_id, representation, subject, object_key, *, cancelled=None):
    entry=BY_ID[entry_id]
    if not entry.business: raise DataStoreError('INVALID_VALUE')
    prefix=(representation,subject,object_key)
    partition=entry.spec.partitioner((*prefix,'root'))
    # Prefix upper successor in the final supplied string component. Empty or
    # user SQL are never evaluated; the kernel validates every typed bound.
    query=Query(partitions=(partition,),lower=prefix,
                upper=(*prefix[:2],prefix[2]+'\x00'),page_size=min(store.limits.query_rows,1000))
    rows=[];generation=None;size=0
    while True:
        if cancelled and cancelled(): raise DataStoreError('OPERATION_CANCELLED')
        page=store.read(entry.spec,query)
        if generation is None:generation=page.generation
        elif generation!=page.generation:raise DataStoreError('DATA_CHANGED')
        size+=sum(len(native_json(r).encode()) for r in page.rows)
        if size>min(store.limits.query_bytes,32*1024*1024):raise DataStoreError('QUERY_BUDGET_EXCEEDED')
        rows.extend(page.rows)
        if len(rows)>min(store.limits.commit_rows,100000):raise DataStoreError('QUERY_BUDGET_EXCEEDED')
        if not page.next_cursor:break
        from dataclasses import replace
        query=replace(query,cursor=page.next_cursor)
    if not rows:return {'found':False,'generation':generation,'data':None,'capability':entry.describe_capability()}
    return {'found':True,'generation':generation,'data':entry.layout.decode([restore_row(entry.spec.schema,r) for r in rows]),
            'field_quality':loads(rows[0]['quality_json']) if rows[0].get('quality_json') else {},
            'confirmation':{'order':str(rows[0]['basis_ns']),'token':rows[0]['basis_token']},
            'capability':entry.describe_capability()}
