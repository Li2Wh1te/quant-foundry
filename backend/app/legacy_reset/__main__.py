"""Operator CLI for explicit legacy maintenance, export and precise reset."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .catalog import ResetRefused
from .operations import (GROUPS, apply_database_group, apply_files, create_plan,
                         enter, finish_rebuild, read_plan, restore_tasks, status,
                         write_new_json)
from .rescue import export, verify


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m app.legacy_reset',
        description='LF-D03 显式维护工具；部署和一般迁移不会调用重置。')
    sub = parser.add_subparsers(dest='command',required=True)
    for command in ('status','enter','restore','plan','export','verify','apply','finish'):
        item = sub.add_parser(command)
        item.add_argument('--expect-database',required=True,help='显式核对目标数据库名；不得传连接串')
        if command in ('plan','apply'):
            item.add_argument('--archive-root',type=Path,help='旧专属归档目录的绝对路径')
        if command in ('export','verify','apply'):
            item.add_argument('--plan',type=Path,required=True)
        if command=='plan':
            item.add_argument('--out',type=Path,required=True)
        if command in ('export','verify','apply'):
            item.add_argument('--rescue',type=Path,required=True)
            item.add_argument('--manifest',type=Path,required=True)
        if command=='enter':
            item.add_argument('--pause-task-id',action='append',default=[],help='需临时暂停的共享任务 ID')
        if command=='restore':
            item.add_argument('--include-legacy',action='store_true',
                              help='仅在重置前放弃维护并恢复旧任务')
        if command=='apply':
            item.add_argument('--sha256',required=True,help='plan 输出的精确摘要')
    args = parser.parse_args(argv)
    try:
        if args.command in ('plan','apply') and args.archive_root is not None \
           and not args.archive_root.is_absolute():
            raise ResetRefused('ARCHIVE_ROOT_UNSAFE','归档目录必须为绝对路径。')
        from app.core.config import get_settings
        from app.db.session import get_engine
        settings = get_settings()
        if settings.database_name != args.expect_database:
            raise ResetRefused('WRONG_DATABASE','配置的数据库名与显式参数不一致。')
        engine = get_engine()
        if args.command=='status':
            result = status(engine,expect_database=args.expect_database)
        elif args.command=='enter':
            result = enter(engine,expect_database=args.expect_database,
                           pause_task_ids=tuple(args.pause_task_id))
        elif args.command=='restore':
            result = restore_tasks(engine,expect_database=args.expect_database,
                                   include_legacy=args.include_legacy)
        elif args.command=='plan':
            result = create_plan(engine,expect_database=args.expect_database,
                                 archive_root=args.archive_root)
            write_new_json(args.out,result)
            result = {'plan':str(args.out),'sha256':result['digest'],
                      'tables':len(result['tables']),'functions':len(result['functions']),
                      'triggers':len(result['triggers']),'blockers':result['blockers']}
        elif args.command=='finish':
            result = finish_rebuild(engine,expect_database=args.expect_database)
        else:
            plan = read_plan(args.plan)
            if plan['database']['database']!=args.expect_database:
                raise ResetRefused('WRONG_DATABASE','计划属于另一数据库。')
            if args.command=='export':
                result = export(engine,plan=plan,output=args.rescue,manifest_path=args.manifest)
            elif args.command=='verify':
                checked = verify(engine,plan=plan,rescue_path=args.rescue,manifest_path=args.manifest)
                result = {key:checked[key] for key in ('plan_hash','record_count','kinds','verified')}
            else:
                if args.sha256!=plan['digest']:
                    raise ResetRefused('PLAN_TAMPERED','显式摘要与计划文件不一致。')
                completed = set(status(engine,expect_database=args.expect_database)['completed_groups'])
                steps = []
                for group in GROUPS[:-1]:
                    checked = (verify(engine,plan=plan,rescue_path=args.rescue,
                                      manifest_path=args.manifest)
                               if group not in completed and group in ('derived','originals')
                                  and any(plan['originals'].values()) else None)
                    steps.append(apply_database_group(engine,plan=plan,group=group,
                        rescue_manifest=checked,archive_root=args.archive_root))
                steps.append(apply_files(engine,plan=plan,archive_root=args.archive_root))
                result = {'phase':'reset_done','steps':steps,'plan_hash':plan['digest']}
        print(json.dumps(result,ensure_ascii=False,sort_keys=True,default=str))
        return 2 if result.get('blockers') else 0
    except (ResetRefused,ValueError,OSError) as exc:
        print(json.dumps({'status':'refused','code':getattr(exc,'code','MAINTENANCE_IO_ERROR'),
                          'message':str(exc)[:500]},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=='__main__':
    sys.exit(main())
