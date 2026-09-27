#!/usr/bin/env python3
"""Compare one synthetic tick file with business-only and reduced envelopes.

This is a bounded artifact experiment, never a migration or formal-store write.
The selected file must use the synthetic tick schema and at most 100,000 rows.
All variants preserve row groups and the kernel's Parquet writer settings.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

import pyarrow.parquet as pq


def measure(path):
    with pq.ParquetFile(path,page_checksum_verification=True) as source:
        names=source.schema_arrow.names
        if not {'f0_sequence','f0_event_ns','f0_source_code','basis_state'} <= set(names):
            raise ValueError('Require a synthetic tick benchmark file')
        removed = {'value_hash', 'basis_valid'} & set(names)
        if removed and len(removed) != 2:
            raise ValueError('Mixed row layout is not a supported measurement')
        if not 1 <= source.metadata.num_rows <= 100000:
            raise ValueError('Require a bounded representative file')
        roles={}
        for name in names:
            roles[name]=('business_value' if name.startswith('f') else
                         'recomputable' if name in ('value_hash','basis_valid') else
                         'current_identity_order_or_quality')
        columns={name:0 for name in names}
        for group in range(source.num_row_groups):
            meta=source.metadata.row_group(group)
            for index,name in enumerate(names):
                columns[name]+=meta.column(index).total_compressed_size
        variants={
            'full_envelope':names,
            'without_recomputable':[n for n in names if roles[n]!='recomputable'],
            'business_only':[n for n in names if roles[n]=='business_value'],
        }
        sizes={}
        with tempfile.TemporaryDirectory(prefix='lf-tick-envelope-') as directory:
            for variant,selected in variants.items():
                target=Path(directory)/(variant+'.parquet')
                schema=source.read_row_group(0,columns=selected).schema
                with pq.ParquetWriter(target,schema,compression='zstd',version='2.6',
                                      use_dictionary=False,write_page_checksum=True) as writer:
                    for group in range(source.num_row_groups):
                        writer.write_table(source.read_row_group(group,columns=selected))
                sizes[variant]=target.stat().st_size
        rows=source.metadata.num_rows
    return {'synthetic':True,'production_executed':False,'rows':rows,
            'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'row_layout': 'typed-object-nodes-v1' if removed else 'typed-object-nodes-v2',
            'removed_columns_absent': not removed,
            'v1_reference_bytes': 7622792,
            'v2_budget_bytes': 4573675,
            'ratio_to_v1_reference': path.stat().st_size/7622792,
            'original_bytes':path.stat().st_size,'variant_bytes':sizes,
            'bytes_per_tick':{k:round(v/rows,6) for k,v in sizes.items()},
            'column_compressed_bytes':columns,'column_roles':roles,
            'limitations':['Single synthetic partition; not representative of all market tick feeds.',
                            'Business-only is a size baseline and cannot support current merge correctness.',
                            'Removing persisted fields requires an explicit schema/rebuild compatibility change.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=measure(args.file)
    with args.output.open('x') as handle:
        json.dump(result,handle,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
