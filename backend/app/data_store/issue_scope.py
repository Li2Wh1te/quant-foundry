"""Code-owned D02 issue scope; opaque/older issues remain conservatively global."""
import json
from sqlalchemy import text
from .errors import DataStoreError
from .storage import prefix_end


def relevant_issue_count(store, spec, query, check=lambda:None):
    if spec.semantics.get('issue_scope')!='object-key-v1':
        with store.catalog.transaction() as c:
            from .issue_sets import logical_count
            return logical_count(c,spec.name)
    lower=spec.key_bytes(query.lower) if query.lower is not None else b''
    upper=spec.key_bytes(query.upper) if query.upper is not None else b'\xff'*2048
    count=0
    with store.catalog.transaction() as c:
        state=c.execute(text('SELECT summary_json FROM data_store_entry_status WHERE entry_id=:i'),
                        {'i':spec.semantics.get('entry_id')}).scalar_one_or_none()
        if state:
            restriction=json.loads(state).get('overflow_restriction',{})
            if restriction.get('blocking_objects') and restriction.get('partition') in query.partitions:
                count+=1
        result=c.execution_options(stream_results=True,max_row_buffer=1).execute(
            text("SELECT * FROM data_store_issues WHERE dataset=:d AND (scope_key=ANY(:scopes) OR target_json::jsonb->>'partition' IS NULL OR target_json::jsonb->>'scope_version' IS NULL OR target_json::jsonb->>'scope_version' NOT IN ('object-key-v1','object-key-set-v1')) ORDER BY issue_key LIMIT :n"),
            {'d':spec.name,'scopes':['local.partition.'+p for p in query.partitions],'n':store.limits.issue_count+1})
        from .issue_sets import members
        for i,row in enumerate(result.mappings()):
            check()
            if i>=store.limits.issue_count:raise DataStoreError('ISSUE_BUDGET_EXCEEDED')
            try:
                expanded=members(row)
            except (DataStoreError,TypeError,ValueError,KeyError):
                count+=1;continue
            for original in expanded:
                try:
                    target=json.loads(original['target_json'])
                    if target.get('scope_version')!='object-key-v1':count+=1;continue
                    if target.get('blocking') is False:continue
                    partition=target.get('partition')
                    if partition is not None and partition not in query.partitions:continue
                    prefix=target.get('prefix')
                    if not isinstance(prefix,list) or len(prefix) not in (0,2,3):count+=1;continue
                    if not prefix:count+=1;continue
                    begin=spec.key_bytes(tuple(prefix));end=prefix_end(begin)
                    if begin<upper and end>lower:count+=1
                except (TypeError,ValueError,KeyError):count+=1
    return count
