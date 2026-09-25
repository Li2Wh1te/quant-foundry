"""Bounded source-native key receipts, no publication or adapter dependency."""
import json

_FIELDS = ('date_ms','nav_date','period_end_ms','end_date_ms','report','report_key','id','ex_date_ms')

def returned_keys(data):
    rows=data.get('item') if isinstance(data,dict) else None
    if not isinstance(rows,list) or len(rows)>100000:
        return {}
    if not rows:
        return {'key_receipt':'actual_returned_keys_v1',
                'returned_keys':{field:[] for field in _FIELDS}}
    keys={}
    for field in _FIELDS:
        values=[r.get(field) if isinstance(r,dict) else None for r in rows]
        if any(type(v) not in (str,int) for v in values):continue
        if any(isinstance(v,str) and len(v.encode())>256 for v in values):continue
        # Duplicate keys remain in this receipt; domain validation must reject
        # them rather than interpret the proof as deduplication permission.
        keys[field]=values
    value={'key_receipt':'actual_returned_keys_v1','returned_keys':keys}
    if len(json.dumps(value,ensure_ascii=False).encode())>262144:
        return {'key_receipt':'unavailable_budget'}
    return value if keys else {}
