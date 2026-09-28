"""Explicit current-store CLI; no command triggers a legacy reset or vendor fetch."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(description='Process existing local Quant Foundry sources only')
    parser.add_argument('command',choices=('describe','status','rebuild','update','retry',
                                           'cleanup','audit-export','configure-resources'))
    parser.add_argument('--entry',action='append',help='Static E01–E71 entry; repeatable. Omit to process all.')
    parser.add_argument('--root',type=Path,help='Existing trusted shared local current-store directory')
    parser.add_argument('--initialize',action='store_true',help='Explicit first store initialization; never migrates or resets')
    parser.add_argument('--partition',action='append',default=[],help='Exact bounded target partition; repeat at most eight times')
    parser.add_argument('--max-passes',type=int,default=256)
    parser.add_argument('--pass-seconds',type=int,default=300)
    parser.add_argument('--pipeline-spill-bytes',type=int,help='Explicit finite disk quota for a complete native scan; does not increase RAM')
    parser.add_argument('--scratch-bytes',type=int,help='Explicit shared staging/spill disk budget, at most 128 GiB')
    parser.add_argument('--issue-count',type=int,help='Explicit current unresolved-issue cap, at most 1000000')
    parser.add_argument('--allow-incompatible-rebuild',action='store_true')
    parser.add_argument('--rescue',type=Path,action='append',help='Self-contained qf-local-rescue-v1 file; never a legacy formal snapshot')
    parser.add_argument('--output',type=Path,help='New result file; for audit-export, a new evidence directory')
    parser.add_argument('--api-base-url',help='Audit only: actual API base URL; HTTP is allowed on loopback only')
    parser.add_argument('--api-token-env',default='QF_AUDIT_API_TOKEN',help='Audit only: name of environment variable holding API bearer token')
    parser.add_argument('--audit-seconds',type=int,default=300)
    parser.add_argument('--audit-files',type=int,default=10000)
    parser.add_argument('--audit-bytes',type=int,default=8*1024**3)
    parser.add_argument('--audit-rows',type=int,default=20_000_000)
    parser.add_argument('--audit-api-samples',type=int,default=8)
    args=parser.parse_args(argv)
    resource_change=any(value is not None for value in
                        (args.pipeline_spill_bytes,args.scratch_bytes,args.issue_count))
    if resource_change and args.command!='configure-resources':
        parser.error('Resource changes require the explicit configure-resources command')
    if args.command=='configure-resources' and (not resource_change or args.initialize or args.entry or args.rescue or args.partition):
        parser.error('configure-resources requires explicit capacities and forbids source selection/initialization')
    from .adapters.registry import ENTRIES, BY_ID
    from .adapters.canonical import NativeInputError
    from .errors import DataStoreError
    try:
        selected=([BY_ID[v] for v in args.entry] if args.entry else
                  [e for e in ENTRIES if e.business] if args.command=='cleanup'
                  else list(ENTRIES))
    except KeyError:
        parser.error('Unknown entry; use describe to list E01–E71')
    if len(set(e.id for e in selected))!=len(selected): parser.error('Duplicate entry')
    if args.command=='cleanup' and any(not e.business for e in selected):
        parser.error('cleanup accepts business entries only')
    if args.command=='describe':
        result={'entries':[e.describe_capability() for e in selected],
                'source_entry_count':len(ENTRIES),'business_entries':sum(e.business for e in ENTRIES),
                'production_executed':False}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    if args.root is None or not args.root.is_absolute(): parser.error('--root must be an existing absolute trusted path')
    if args.command=='audit-export':
        if args.initialize or args.rescue or args.partition or not args.output:
            parser.error('audit-export requires --output and forbids initialization, rescue and partition writes')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',args.api_token_env):
            parser.error('--api-token-env must name one environment variable')
        from app.db.session import get_engine
        from .audit_export import AuditLimits,export_audit
        try:
            limits=AuditLimits(seconds=args.audit_seconds,files=args.audit_files,
                               bytes=args.audit_bytes,rows=args.audit_rows,
                               api_samples=args.audit_api_samples)
            result=export_audit(get_engine(),args.root,args.output,entries=tuple(selected),
                                limits=limits,api_base_url=args.api_base_url,
                                api_token=os.environ.get(args.api_token_env))
        except (ValueError,OSError) as error:
            result={'complete':False,'reason':'AUDIT_CONFIGURATION_OR_IO_ERROR'}
        except Exception:
            # Database exceptions can contain a DSN, SQL or source payload.
            result={'complete':False,'reason':'AUDIT_UNAVAILABLE'}
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result['complete'] else 2
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
        options=PipelineOptions(mode=args.command if args.command in ('rebuild','update','retry') else 'update',
            partitions=tuple(args.partition),maximum_passes=args.max_passes,pass_seconds=args.pass_seconds,
            allow_incompatible_rebuild=args.allow_incompatible_rebuild)
        engine=get_engine()
        with CurrentStore(engine,args.root,cursor_key=key,initialize=args.initialize) as store:
            if args.command=='configure-resources':
                output.update(store.configure_resources(scratch_bytes=args.scratch_bytes,
                    pipeline_spill_bytes=args.pipeline_spill_bytes,issue_count=args.issue_count))
            source_limits=SourceLimits(pass_seconds=args.pass_seconds)
            native=NativeSources(engine,limits=source_limits,cancelled=lambda:cancelled[0])
            sources=(CombinedSources(native,RescueSources(args.rescue,limits=source_limits,
                     cancelled=lambda:cancelled[0])) if args.rescue else native)
            seen=set()
            queue=[] if args.command=='configure-resources' else list(selected)
            # Drain durable scans before charging another complete acquisition.
            # Import targets still run exactly once through the same queue.
            pending=store.budget.pending_keys() if args.command in ('rebuild','update','retry') else set()
            queue.sort(key=lambda entry:'pipeline.'+entry.id not in pending)
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
                try:
                    result=run_entry(store,entry,sources,options=options,cancelled=lambda:cancelled[0])
                except (NativeInputError,DataStoreError) as error:
                    # Retain actual attempted-source and committed-partition
                    # counts instead of replacing useful failure evidence.
                    result={**read_entry_status(store,entry.id),'entry_id':entry.id,
                            'state':'incomplete','complete':False,'qualified':False,'reason':error.code}
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
