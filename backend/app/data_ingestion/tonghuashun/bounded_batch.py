"""At most three once-only diagnostics under one absolute 540-second wall.

Run this official entrypoint once for an approved plan. Its wall deadline covers
scope supervision, result validation and all configured inter-scope gaps. It
uses the existing collectors and storage; a batch file is private operational
evidence, never an observation or an alternative data foundation.
"""
import argparse
from datetime import date
import json
import math
from pathlib import Path
import signal
import threading
import time
from uuid import uuid4

from . import bounded


MAX_SECONDS = 540.0
MAX_SCOPES = 3
OUTCOME_NAMES = {
    'completed': '诊断已完成', 'cancelled': '已取消', 'timed_out': '达到墙钟截止',
    'verification_required': '退出或发布结果需要核验', 'supervisor_error': '监督执行失败',
    'worker_error': '子进程执行失败', 'request_budget_violation': '请求计数超过批准预算',
}


def run(scopes, output, *, gap_seconds=0, wall_seconds=MAX_SECONDS, _supervise=None):
    started = time.monotonic()
    if (not math.isfinite(wall_seconds) or not 0 < wall_seconds <= MAX_SECONDS or
            not math.isfinite(gap_seconds) or not 0 <= gap_seconds < MAX_SECONDS):
        raise ValueError('invalid_batch_budget')
    wall_deadline = started + wall_seconds
    # Reserve final evidence writing and the owned worker's <=5s server-side
    # statement lifetime inside the whole-batch wall, including gap expiration.
    shutdown_reserve = min(5.0, wall_seconds * .05)
    deadline = wall_deadline - shutdown_reserve
    scopes = tuple(scopes)
    if not 1 <= len(scopes) <= MAX_SCOPES or len({tuple(s.as_dict().values()) for s in scopes}) != len(scopes):
        raise ValueError('invalid_batch_scope')
    for scope in scopes:
        scope.parameters()
    output = Path(output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    record = {
        'protocol': 'ths-bounded-batch@1', 'operation_id': str(uuid4()),
        'scopes': [s.as_dict() for s in scopes], 'results': [],
        'budget': {'wall_seconds': wall_seconds, 'scope_seconds': bounded.MAX_SECONDS,
                   'logical_requests': len(scopes), 'http_attempts': len(scopes) * 3,
                   'gap_seconds': gap_seconds, 'gaps_included': True,
                   'shutdown_reserve_seconds': shutdown_reserve},
        'started_monotonic': started, 'deadline_monotonic': deadline,
        'wall_deadline_monotonic': wall_deadline,
        'outcome': 'running', 'resources_verified': False, 'attempts_started': 0,
    }
    interrupted = threading.Event()
    old_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        old_handlers[signum] = signal.signal(signum, lambda *_: interrupted.set())
    supervise = _supervise or bounded.supervise
    try:
        for index, scope in enumerate(scopes):
            if interrupted.is_set():
                record['outcome'] = 'cancelled'
                break
            if time.monotonic() >= deadline:
                record['outcome'] = 'timed_out'
                break
            # The scope's TERM/KILL/reap reserve fits inside this same absolute
            # deadline even when the remaining batch allowance is under 180s.
            record['attempts_started'] += 1
            result = supervise(scope, output / f'scope-{index + 1:02d}', _deadline=deadline)
            record['results'].append({key: result[key] for key in (
                'operation_id', 'outcome', 'process_exited', 'unknown_publication',
                'counts', 'counts_complete', 'elapsed_seconds', 'exit_code')})
            bounded._write(output / 'batch.json', record)
            if (not result['process_exited'] or result['unknown_publication'] or
                    not result['counts_complete']):
                record['outcome'] = 'verification_required'
                break
            if result['outcome'] in ('timed_out', 'cancelled', 'supervisor_error', 'worker_error'):
                record['outcome'] = result['outcome']
                break
            counts = result['counts']
            if counts['logical_requests'] > 1 or counts['http_attempts'] > 3:
                record['outcome'] = 'request_budget_violation'
                break
            if index + 1 < len(scopes):
                gap_end = min(deadline, time.monotonic() + gap_seconds)
                while not interrupted.is_set() and time.monotonic() < gap_end:
                    interrupted.wait(min(.05, max(0, gap_end - time.monotonic())))
        else:
            record['outcome'] = 'completed'
        if time.monotonic() >= deadline and record['outcome'] in ('running', 'completed'):
            record['outcome'] = 'timed_out'
    except Exception:
        # Individual supervision owns cancellation/reaping. Its unknown exit
        # or publication remains unverified; never infer zero work or retry.
        record['outcome'] = 'supervisor_error'
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        record.update(elapsed_seconds=round(time.monotonic() - started, 6),
                      unattempted_scopes=len(scopes) - record['attempts_started'])
        record['process_wall_budget_met'] = (
            time.monotonic() <= wall_deadline and len(record['results']) == record['attempts_started'] and
            all(result['process_exited'] for result in record['results']))
        counts_known = (len(record['results']) == record['attempts_started'] and
                        all(result['counts_complete'] for result in record['results']))
        record['counts_complete'] = counts_known
        record['counts'] = {key: sum(result['counts'][key] for result in record['results']) if counts_known else None
                            for key in bounded.COUNT_KEYS}
        logical, http = (record['counts'][key] if record['counts'][key] is not None else '未知'
                         for key in ('logical_requests', 'http_attempts'))
        data_types = '、'.join(sorted({'ETF日线' if scope.dataset == 'etf_daily' else '指数日线' for scope in scopes}))
        start, end = min(s.start_date for s in scopes), max(s.end_date for s in scopes)
        record['message'] = (f"同花顺{data_types}{start}至{end}固定批次诊断结束："
                             f"已处理{len(record['results'])}/{len(scopes)}个范围，"
                             f"逻辑请求{logical}次、HTTP尝试{http}次，{OUTCOME_NAMES.get(record['outcome'], '执行未完成')}，"
                             f"整批墙钟含间隔{record['elapsed_seconds']}秒；"
                             "未建立CurrentStore覆盖凭据，未推进底座检查点。")
        bounded._write(output / 'batch.json', record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description='同花顺固定批次诊断，整批墙钟包含间隔且最多540秒')
    parser.add_argument('--plan', required=True, type=Path,
                        help='Newly approved JSON list of 1–3 exact dataset/subject/start_date/end_date scopes')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--gap-seconds', type=float, default=0)
    parser.add_argument('--wall-seconds', type=float, default=MAX_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.plan.is_symlink() or args.plan.stat().st_size > 4096:
            raise ValueError('invalid_plan')
        plan = json.loads(args.plan.read_text())
        if not isinstance(plan, list) or not 1 <= len(plan) <= MAX_SCOPES:
            raise ValueError('invalid_plan')
        scopes = []
        for value in plan:
            if not isinstance(value, dict) or set(value) != {'dataset', 'subject', 'start_date', 'end_date'}:
                raise ValueError('invalid_plan')
            scopes.append(bounded.Scope(value['dataset'], value['subject'],
                date.fromisoformat(value['start_date']), date.fromisoformat(value['end_date'])))
        result = run(scopes, args.output, gap_seconds=args.gap_seconds, wall_seconds=args.wall_seconds)
        print(json.dumps({'outcome': result['outcome'], 'message': result['message']}, ensure_ascii=False))
        return 0 if result['outcome'] == 'completed' else 2
    except Exception:
        print('{"outcome":"blocked","problem":"invalid_batch_input_or_output"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
