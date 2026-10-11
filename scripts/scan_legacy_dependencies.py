#!/usr/bin/env python3
"""Read-only retirement inventory; findings NEVER authorize deletion.

AST imports, constant dynamic imports, entry points, container commands and
frontend requests are candidates. Startup/real-run tests cover computed imports.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import sys

SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', 'target', 'dist',
             '__pycache__', '.scratch', '.logs', '.mypy_cache', '.pytest_cache'}
SUFFIXES = {'.py', '.pyi', '.rs', '.ts', '.tsx', '.js', '.jsx', '.cjs', '.mjs',
            '.toml', '.json', '.yaml', '.yml', '.md', '.sh'}
SPECIAL = {'Makefile', 'Dockerfile', 'uv.lock', 'Cargo.lock'}
PROTECTED_SPLIT = {'models.py', 'result_records.py', 'result_models.py',
                   'result_repository.py', 'result_router.py', 'result_schemas.py',
                   'account_profiles.py', 'account_queries.py', 'account_resolution.py',
                   'fee_catalog.py', 'pagination.py', 'comparison.py', 'saved_comparisons.py',
                   'repository.py', 'router.py'}
PATTERNS = {
    'legacy_module_reference': re.compile(r'app\.(?:backtesting|strategy_protocol)(?:\.[A-Za-z_][\w]*)*'),
    'legacy_runner_class': re.compile(r'\b(?:RunnerSupervisor|ChunkBacktestViewFactory|EtfFactsAdapter)\b'),
    'legacy_write_or_result_route_review': re.compile(r'/api/admin/(?:backtest-runs|internal/backtest|backtests)[^\s\"\'<>]*'),
    'dynamic_import_review': re.compile(r'\b(?:import_module|__import__)\s*\('),
    'entrypoint_or_container_review': re.compile(r'(?:python(?:3)?\s+-m\s+app\.|entry_points|project\.scripts|CMD\s*\[|ENTRYPOINT\s*\[)'),
    'frontend_write_review': re.compile(r'(?:method:\s*[\"\'](?:POST|PUT|PATCH|DELETE)|\.(?:post|put|patch|delete)\s*\()'),
}


def _old(module: str) -> bool:
    return any(module == p or module.startswith(p + '.') for p in ('app.backtesting', 'app.strategy_protocol'))


def _package(rel: str) -> str:
    parts = list(Path(rel).with_suffix('').parts)
    if parts and parts[0] == 'backend':
        parts = parts[1:]
    return '.'.join(parts[:-1])


def scan(repo: Path, *, max_bytes: int = 2_000_000) -> dict:
    repo = repo.resolve(strict=True)
    if not (repo / 'backend' / 'app').is_dir():
        raise ValueError('Expected a repository with backend/app.')
    result = {'kind': 'read_only_source_inventory', 'deletion_authorized': False,
              'scope': 'source candidates; no database/runtime inspection; computed imports require startup tests',
              'complete': True, 'scanned_files': 0, 'candidates': [], 'references': [], 'skipped': []}
    for current, dirs, filenames in os.walk(repo, followlinks=False):
        filtered = []
        for d in sorted(dirs):
            q = Path(current) / d
            if q.is_symlink():
                result['skipped'].append({'path': q.relative_to(repo).as_posix(), 'reason': 'symlink_directory'})
                result['complete'] = False
            elif d not in SKIP_DIRS and not d.startswith('.env'):
                filtered.append(d)
        dirs[:] = filtered
        for name in sorted(filenames):
            path = Path(current) / name
            rel = path.relative_to(repo).as_posix()
            if name.startswith('.env') or (path.suffix not in SUFFIXES and name not in SPECIAL):
                continue
            reason = None
            try:
                if path.is_symlink():
                    reason = 'symlink_file'
                elif path.stat().st_size > max_bytes:
                    reason = 'file_budget'
                else:
                    source = path.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                reason = 'unreadable'
            if reason:
                result['skipped'].append({'path': rel, 'reason': reason})
                result['complete'] = False
                continue
            result['scanned_files'] += 1
            if rel.startswith(('backend/app/backtesting/', 'backend/app/strategy_protocol/')):
                category = ('split_preserve_business_data' if name in PROTECTED_SPLIT or name.startswith('account_')
                            else 'retirement_candidate_review_dependencies')
                result['candidates'].append({'path': rel, 'category': category, 'safe_to_delete': None})
            if path.suffix in {'.py', '.pyi'}:
                try:
                    tree = ast.parse(source, filename=rel)
                except SyntaxError:
                    result['skipped'].append({'path': rel, 'reason': 'ast_parse_error'})
                    result['complete'] = False
                else:
                    for node in ast.walk(tree):
                        modules = []
                        kind = 'python_import'
                        if isinstance(node, ast.Import):
                            modules = [n.name for n in node.names]
                        elif isinstance(node, ast.ImportFrom):
                            module = node.module or ''
                            if node.level:
                                package = _package(rel).split('.')
                                if node.level <= len(package):
                                    module = '.'.join(package[:len(package) - node.level + 1] + ([module] if module else []))
                            modules = [module]
                        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _old(node.value):
                            modules, kind = [node.value], 'python_module_string'
                        for module in modules:
                            if _old(module):
                                result['references'].append({'path': rel, 'line': node.lineno, 'kind': kind, 'symbol': module[:160]})
            for number, line in enumerate(source.splitlines(), 1):
                for label, pattern in PATTERNS.items():
                    match = pattern.search(line)
                    if match:
                        result['references'].append({'path': rel, 'line': number, 'kind': label, 'symbol': match.group(0)[:160]})
    result['candidates'].sort(key=lambda x: x['path'])
    result['references'].sort(key=lambda x: (x['path'], x['line'], x['kind']))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--max-bytes', type=int, default=2_000_000)
    args = parser.parse_args(argv)
    try:
        if args.max_bytes < 1:
            raise ValueError('file budget must be positive')
        result = scan(args.repo, max_bytes=args.max_bytes)
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            with args.output.open('x', encoding='utf-8') as output:
                output.write(encoded)
        else:
            sys.stdout.write(encoded)
        return 0 if result['complete'] else 2
    except (ValueError, OSError) as error:
        print(json.dumps({'complete': False, 'reason': type(error).__name__, 'deletion_authorized': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
