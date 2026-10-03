"""Field/byte oracle from the pre-optimization 5df325c implementation.

The corpus is synthetic and includes every business entry plus PostgreSQL-shaped
E69 scalar rows. Its immutable digest covers each source literal, identity, typed
node field, normalized byte string and value hash, not only the final row counts.
"""
from datetime import date, timedelta
from decimal import Decimal
import hashlib

from app.data_store.adapters.canonical import encode
from app.data_store.adapters.contracts import LocalInput, digest, native_json
from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID, ENTRIES
from app.data_store.merge import value_hash
from tests.test_data_store_domain_samples import NOW, sample


# Captured by running this corpus against immutable commit
# 5df325cb29bd1273f632d27c362574d12bf27a39, before the encoding/field-plan changes.
LEGACY_CORPUS_SHA256 = '0b05c95baa4acaa9af2dc807ac1baaf23f0bad2bb17a3677ecf5670d43d3df98'


def encoding_corpus():
    inputs=[(entry,sample(entry.id)) for entry in ENTRIES if entry.business]
    factors=('1.000000000000','1.2300','0.000000000001','1E+9',
             '100000000000000000000.123456789','-0.0','-1')
    entry=BY_ID['E69']
    for index in range(257):
        subject=f'{index//31:06}.SH'
        row={'source':'tushare','ts_code':subject,
             'trade_date':(date(2010,1,1)+timedelta(days=index)).isoformat(),
             'adj_factor':Decimal(factors[index%len(factors)]),
             'created_at':'2026-01-01T00:00:00+00:00',
             'updated_at':'2026-01-01T00:00:00+00:00'}
        inputs.append((entry,LocalInput('tushare',entry.native,subject,'default',NOW,
                                       row,digest(row),order_kind='current_table_snapshot',
                                       representation='native_table')))
    records=[]
    for entry,raw in inputs:
        units=[]
        for unit in normalize(entry,raw):
            units.append({'key':unit.key,'group':unit.group,'order':unit.order,
                          'token':unit.token,'complete':unit.complete,
                          'failure':unit.failure,'limitations':unit.limitations,
                          'withdrawn':unit.withdrawn,
                          'rows_native':native_json(unit.rows),
                          'rows_canonical':encode(unit.rows),
                          'value_hash':value_hash(unit.rows)})
        records.append({'entry':entry.id,'descriptor':entry.spec.descriptor(),
                        'source_literal':native_json(raw.content),
                        'source_token':raw.token,'source_group':raw.group,
                        'representation':raw.representation_key,
                        'order_ns':raw.order_ns,'units':units})
    return records


def test_every_business_layout_and_postgres_scalar_retains_legacy_field_bytes():
    payload=encode(encoding_corpus()).encode()
    assert hashlib.sha256(payload).hexdigest()==LEGACY_CORPUS_SHA256


if __name__=='__main__':
    # A local diagnostic may run this same corpus against an explicitly mounted
    # historical source tree. No database, supplier call or production original
    # is accessed, and the output is exclusively created rather than replaced.
    import argparse
    import json
    from pathlib import Path
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    records=encoding_corpus()
    payload=encode(records)
    with args.output.open('x',encoding='utf-8') as handle:handle.write(payload+'\n')
    print(json.dumps({'synthetic':True,'records':len(records),
                      'sha256':hashlib.sha256(payload.encode()).hexdigest()}))
