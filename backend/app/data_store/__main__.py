"""Explicit current-store CLI; no command triggers a legacy reset or vendor fetch."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(description='Process existing local Quant Foundry sources only')
    parser.add_argument('command',choices=('describe','status','rebuild','update','retry',
                                           'cleanup','audit-export'))
    parser.add_argument('--entry',action='append',help='Static E01–E71 entry; repeatable. Omit to process all.')
    parser.add_argument('--root',type=Path,help='Existing trusted shared local current-store directory')
    parser.add_argument('--initialize',action='store_true',help='Explicit first store initialization; never migrates or resets')
    parser.add_argument('--partition',action='append',default=[],help='Exact bounded target partition; repeat at most eight times')
    parser.add_argument('--max-passes',type=int,default=256)
    parser.add_argument('--pass-seconds',type=int,default=300)
    parser.add_argument('--allow-incompatible-rebuild',action='store_true')
    parser.add_argument('--rescue',type=Path,action='append',help='Self-contained qf-local-rescue-v1 file; never a legacy formal snapshot')
    parser.add_argument('--output',type=Path,help='New result file (will not overwrite an existing file)')
    args=parser.parse_args(argv)
    from .adapters.registry import ENTRIES, BY_ID
    from .adapters.canonical import NativeInputError
    from .errors import DataStoreError
    try:
        selected=([BY_ID[v] for v in args.entry] if args.entry else
                  [e for e in ENTRIES if e.business] if args.command in ('cleanup','audit-export')
                  else list(ENTRIES))
    except KeyError:
        parser.error('Unknown entry; use describe to list E01–E71')
    if len(set(e.id for e in selected))!=len(selected): parser.error('Duplicate entry')
    if args.command in ('cleanup','audit-export') and any(not e.business for e in selected):
        parser.error('cleanup and audit-export accept business entries only')
    if args.command=='describe':
        result={'entries':[e.describe_capability() for e in selected],
                'source_entry_count':len(ENTRIES),'business_entries':sum(e.business for e in ENTRIES),
                'production_executed':False}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    if args.root is None or not args.root.is_absolute(): parser.error('--root must be an existing absolute trusted path')
    key=os.environ.get('QF_CURSOR_SIGNING_KEY','').encode()
    if len(key)<32: parser.error('Provide QF_CURSOR_SIGNING_KEY through the environment (at least 32 bytes)')
    # The configured PostgreSQL engine is opened only after an explicit command.
    # Source configuration enabled/disabled flags are neither read nor changed.
    from app.db.session import get_engine
    from .storage import CurrentStore
    from .local_sources import NativeSources,RescueSources,CombinedSources,SourceLimits
    from .pipeline import PipelineOptions,run_entry,read_entry_status
    cancelled=[False]
    def cancel(*_): cancelled[0]=True
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,cancel)
    output={'command':args.command,'entries':[],'complete':True,'supplier_network_used':False}
    try:
        options=PipelineOptions(mode=args.command if args.command!='status' else 'update',
            partitions=tuple(args.partition),maximum_passes=args.max_passes,pass_seconds=args.pass_seconds,
            allow_incompatible_rebuild=args.allow_incompatible_rebuild)
        engine=get_engine()
        with CurrentStore(engine,args.root,cursor_key=key,initialize=args.initialize) as store:
            source_limits=SourceLimits(pass_seconds=args.pass_seconds)
            native=NativeSources(engine,limits=source_limits,cancelled=lambda:cancelled[0])
            sources=(CombinedSources(native,RescueSources(args.rescue,limits=source_limits,
                     cancelled=lambda:cancelled[0])) if args.rescue else native)
            seen=set()
            queue=list(selected)
            for entry in queue:
                if entry.id in seen: continue
                seen.add(entry.id)
                if args.command=='status':
                    output['entries'].append(read_entry_status(store,entry.id));continue
                if args.command=='cleanup':
                    try:
                        result=store.cleanup(entry.spec.name)
                        output['entries'].append({'entry_id':entry.id,'dataset':entry.spec.name,
                                                  'complete':True,**result})
                    except DataStoreError as error:
                        output['entries'].append({'entry_id':entry.id,'dataset':entry.spec.name,
                                                  'complete':False,'reason':error.code})
                        output['complete']=False
                    continue
                if args.command=='audit-export':
                    try:
                        descriptor=store.describe_capability(entry.spec)
                    except DataStoreError as error:
                        if error.code!='DATASET_MISSING': raise
                        descriptor={'dataset':entry.spec.name,'status':'not_checked',
                                    'row_count':None,'generation':None,'issues':None}
                    output['entries'].append({'entry_id':entry.id,'complete':True,
                                              'descriptor':descriptor,
                                              'processing':read_entry_status(store,entry.id)})
                    continue
                try:
                    result=run_entry(store,entry,sources,options=options,cancelled=lambda:cancelled[0])
                except (NativeInputError,DataStoreError) as error:
                    result={'entry_id':entry.id,'state':'incomplete','complete':False,'reason':error.code}
                output['entries'].append(result)
                output['complete'] &= result.get('complete',False)
                if entry.disposition=='ingestion_channel' and entry.target not in seen:
                    queue.append(BY_ID[entry.target])
                if cancelled[0]: output['complete']=False;break
    except (NativeInputError,DataStoreError,ValueError,OSError) as error:
        output.update(complete=False,reason=getattr(error,'code','LOCAL_CONFIGURATION_OR_IO_ERROR'))
    except Exception:
        # Do not expose DSNs, SQL, provider payloads or authentication material.
        output.update(complete=False,reason='LOCAL_OPERATION_FAILED')
    encoded=json.dumps(output,ensure_ascii=False,indent=2)
    if args.output:
        with args.output.open('x',encoding='utf-8') as handle: handle.write(encoded+'\n')
    print(encoded)
    return 0 if output['complete'] and all(e.get('qualified',True) for e in output['entries']) else 2


if __name__=='__main__':
    sys.exit(main())
