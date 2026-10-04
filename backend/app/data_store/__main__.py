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
    parser.add_argument('command',choices=('describe','status','plan-active','rebuild','update','retry',
                                           'cleanup','cancel-sealed','verify-coverage','compact-issues','audit-export','configure-resources'))
    parser.add_argument('--entry',action='append',help='Static E01–E71 entry; repeatable. Omit to process all.')
    parser.add_argument('--root',type=Path,help='Existing trusted shared local current-store directory')
    parser.add_argument('--initialize',action='store_true',help='Explicit first store initialization; never migrates or resets')
    parser.add_argument('--partition',action='append',default=[],help='Exact bounded target partition; repeat at most eight times')
    parser.add_argument('--max-passes',type=int,default=256)
    parser.add_argument('--max-claim-batches',type=int,
                        help='Independent native claim-batch limit; defaults to --max-passes')
    parser.add_argument('--max-partition-passes',type=int,
                        help='Independent partition-pass limit; defaults to --max-passes')
    parser.add_argument('--resume-sealed-only',action='store_true',
                        help='Resume exactly one existing sealed batch; never claim new native work')
    parser.add_argument('--admit-active-only',action='store_true',
                        help='Atomically admit one existing E23/E44 batch using exact plan-active fences')
    parser.add_argument('--expect-input-identity',
                        help='Exact sealed input identity SHA256 for --resume-sealed-only')
    parser.add_argument('--expect-source-selection',
                        help='Exact sealed source-selection SHA256 for --resume-sealed-only')
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
    parser.add_argument('--audit-api-entry',action='append',
                        help='Audit only: required business API entry; repeat to require every exact member')
    parser.add_argument('--audit-local-only',action='store_true',
                        help='Explicit local file/API audit; never a full-range acceptance result')
    args=parser.parse_args(argv)
    if args.resume_sealed_only and args.admit_active_only:
        parser.error('Choose one input admission mode')
    if args.resume_sealed_only or args.admit_active_only:
        if (args.command!='update' or not args.entry or len(args.entry)!=1 or
                args.initialize or args.rescue or args.partition or args.allow_incompatible_rebuild or
                args.max_claim_batches not in (None,1) or
                any(not fence or not re.fullmatch(r'[0-9a-f]{64}',fence) for fence in
                    (args.expect_input_identity,args.expect_source_selection))):
            parser.error('--resume-sealed-only requires one existing update entry and both exact fences')
    elif args.command == 'cancel-sealed':
        if (not args.entry or len(args.entry) != 1 or args.initialize or args.rescue or
                args.partition or args.allow_incompatible_rebuild or
                any(not fence or not re.fullmatch(r'[0-9a-f]{64}', fence) for fence in
                    (args.expect_input_identity, args.expect_source_selection))):
            parser.error('cancel-sealed requires one existing business entry and both original fences')
    elif args.expect_input_identity is not None or args.expect_source_selection is not None:
        parser.error('Input fences require an explicit admission mode')
    if args.command=='plan-active' or args.admit_active_only:
        if (not args.entry or len(args.entry)!=1 or args.entry[0] not in ('E23','E44') or
                args.initialize or args.rescue or args.partition or args.allow_incompatible_rebuild):
            parser.error('Active input planning/admission requires one existing E23/E44 entry')
    if any(value is not None for value in (args.max_claim_batches,args.max_partition_passes)):
        if args.command not in ('rebuild','update','retry') or any(
                value is not None and not 1<=value<=4096
                for value in (args.max_claim_batches,args.max_partition_passes)):
            parser.error('Independent pass limits require a bounded processing command')
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
    if args.resume_sealed_only and not selected[0].business:
        parser.error('Sealed continuation requires exactly one business entry')
    if args.command=='cleanup' and any(not e.business for e in selected):
        parser.error('cleanup accepts business entries only')
    if args.command in ('compact-issues','verify-coverage') and (args.initialize or args.rescue or args.partition or any(not e.business for e in selected)):
        parser.error('current metadata checks require existing business entries and native sources')
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
                                local_only=args.audit_local_only,
                                api_entries=tuple(args.audit_api_entry) if args.audit_api_entry else None,
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
    from .pipeline import PipelineOptions,run_local,read_entry_status
    cancelled=[False]
    def cancel(*_): cancelled[0]=True
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,cancel)
    output={'command':args.command,'entries':[],'complete':True,'supplier_network_used':False}
    try:
        options=PipelineOptions(mode=args.command if args.command in ('rebuild','update','retry') else 'update',
            partitions=tuple(args.partition),maximum_passes=args.max_passes,pass_seconds=args.pass_seconds,
            allow_incompatible_rebuild=args.allow_incompatible_rebuild,
            maximum_claim_batches=args.max_claim_batches,maximum_partition_passes=args.max_partition_passes,
            resume_sealed_only=args.resume_sealed_only,expected_input_identity=args.expect_input_identity,
            expected_source_selection=args.expect_source_selection,admit_active_only=args.admit_active_only)
        engine=get_engine()
        if args.command=='plan-active':
            from .active_input import planning_store
            # A plan never uses the mutating root-binding/scratch constructor.
            # Enforce read-only for all native metadata connections as well as
            # the catalog view, even if the caller omitted libpq PGOPTIONS.
            engine=engine.execution_options(postgresql_readonly=True)
            context=planning_store(engine,args.root)
        else:
            context=CurrentStore(engine,args.root,cursor_key=key,initialize=args.initialize)
        with context as store:
            if args.command=='configure-resources':
                output.update(store.configure_resources(scratch_bytes=args.scratch_bytes,
                    pipeline_spill_bytes=args.pipeline_spill_bytes,issue_count=args.issue_count))
            source_limits=SourceLimits(pass_seconds=args.pass_seconds)
            native=NativeSources(engine,limits=source_limits,cancelled=lambda:cancelled[0])
            sources=(CombinedSources(native,RescueSources(args.rescue,limits=source_limits,
                     cancelled=lambda:cancelled[0])) if args.rescue else native)
            if args.command=='plan-active':
                from .active_input import active_input_plan
                output['entries']=[active_input_plan(store,selected[0],native,
                    cancelled=lambda:cancelled[0]).plan]
                output['plan_ready']=True
            elif args.command == 'cancel-sealed':
                from .cancel_sealed import cancel_sealed
                output['entries'] = [cancel_sealed(store, selected[0],
                    expected_identity=args.expect_input_identity,
                    expected_selection=args.expect_source_selection,
                    cancelled=lambda: cancelled[0])]
                output['complete'] = output['entries'][0]['scratch_released']
            elif args.command in ('rebuild','update','retry'):
                output['entries']=run_local(store,sources,entries=selected,options=options,
                                            cancelled=lambda:cancelled[0])
                output['complete']=all(row.get('complete') and row.get('qualified',True)
                                       for row in output['entries'])
            elif args.command=='verify-coverage':
                if args.initialize or args.rescue or args.partition or any(not e.business for e in selected):
                    parser.error('verify-coverage requires existing business entries and native inputs')
                from .verify_coverage import verify_existing
                for e in selected:
                    try:result=verify_existing(store,e,native,seconds=args.pass_seconds,cancelled=lambda:cancelled[0])
                    except (NativeInputError,DataStoreError) as error:result={'entry_id':e.id,'complete':False,'reason':error.code,**getattr(error,'verification',{})}
                    output['entries'].append(result)
                output['complete']=all(r['complete'] for r in output['entries'])
            elif args.command=='compact-issues':
                if args.initialize or args.rescue or args.partition or any(not e.business for e in selected):
                    parser.error('compact-issues accepts business entries and forbids source writes/initialization')
                from .issue_sets import compact_entry
                output['entries']=[compact_entry(store,e,cancelled=lambda:cancelled[0]) for e in selected]
            elif args.command=='status':
                output['entries']=[read_entry_status(store,e.id) for e in selected]
            elif args.command=='cleanup':
                for entry in selected:
                    try:
                        result=store.cleanup(entry.spec.name)
                        output['entries'].append({'entry_id':entry.id,'dataset':entry.spec.name,
                                                  'complete':True,**result})
                    except DataStoreError as error:
                        output['entries'].append({'entry_id':entry.id,'dataset':entry.spec.name,
                                                  'complete':False,'reason':error.code})
                        output['complete']=False
    except (NativeInputError,DataStoreError,ValueError,OSError) as error:
        output.update(complete=False,reason=getattr(error,'code','LOCAL_CONFIGURATION_OR_IO_ERROR'))
        if hasattr(error,'results'):output['entries']=error.results
    except Exception as error:
        # Do not expose DSNs, SQL, provider payloads or authentication material.
        output.update(complete=False,reason='LOCAL_OPERATION_FAILED',error_type=type(error).__name__)
        if hasattr(error,'results'):output['entries']=error.results
    encoded=json.dumps(output,ensure_ascii=False,indent=2)
    if args.output:
        with args.output.open('x',encoding='utf-8') as handle: handle.write(encoded+'\n')
    print(encoded)
    if cancelled[0]:return 130
    return 0 if output['complete'] and all(e.get('qualified',True) for e in output['entries']) else 2


if __name__=='__main__':
    sys.exit(main())
