"""qf backtest: a thin HTTP client, never a local strategy executor."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import warnings

from . import ContractError
from .client import BacktestClient, BacktestError, RunConfig
from ._http import MAX_BYTES, _decode, redact


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error includes argv values, possibly credentials.
        self.exit(2, '{"code":"INVALID_ARGUMENTS","message":"命令参数无效；请使用 qf backtest --help"}\n')


def _parser():
    parser = _Parser(prog="qf", description="Quant Foundry HTTP 客户端")
    areas = parser.add_subparsers(dest="area", required=True, parser_class=_Parser)
    backtest = areas.add_parser("backtest", help="同一 S3 运行服务")
    backtest.add_argument("--base-url", default=os.environ.get("QF_BASE_URL"),
                          help="服务源站；默认 QF_BASE_URL，远端使用 HTTPS")
    backtest.add_argument("--timeout", type=float, default=30, help="网络超时秒数，默认 30")
    commands = backtest.add_subparsers(dest="command", required=True, parser_class=_Parser)
    commands.add_parser("capabilities", help="引擎能力与实际数据分别列出")
    for name in ("preflight", "submit", "run", "batch"):
        command = commands.add_parser(name, help="预检/提交；run 是 submit 别名" if name != "batch" else "显式参数数组")
        command.add_argument("config", help="完整 RunConfig JSON；batch 为 configs 数组对象")
        if name != "preflight":
            command.add_argument("--idempotency-key", required=True, help="持有并复用的提交键；不能使用 token")
    for name in ("status", "cancel", "result"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name == "result":
            command.add_argument("--kind", choices=("summary", "equity", "orders", "trades", "positions", "records", "logs"), default="summary")
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--cursor")
    for name in ("batch-status", "cancel-batch", "retry-failed"):
        command = commands.add_parser(name)
        command.add_argument("batch_id")
        if name == "batch-status":
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--cursor")
        elif name == "retry-failed":
            command.add_argument("--idempotency-key", required=True)
    commands.add_parser("compare").add_argument("run_ids", nargs="+")
    return parser


def _input(path):
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise BacktestError("RESOURCE_LIMIT", "配置文件超过 1MiB")
        return _decode(data)
    except (OSError, ValueError, RecursionError):
        raise BacktestError("INVALID_RUN_CONFIG", "配置文件必须为可读取、无重复字段的有限 JSON 对象") from None


def _token():
    token = os.environ.get("QF_API_TOKEN")
    if token:
        return token
    if sys.stdin.isatty():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                return getpass.getpass("API token (hidden): ")
        except (getpass.GetPassWarning, EOFError):
            pass
    raise BacktestError("UNAUTHENTICATED", "请通过 QF_API_TOKEN 环境或交互隐藏输入提供 token；不接受命令参数")


def main(argv=None) -> int:
    token = os.environ.get("QF_API_TOKEN", "")
    try:
        args = _parser().parse_args(argv)
        if not args.base_url:
            raise BacktestError("INVALID_CLIENT_CONFIG", "请设置 QF_BASE_URL 或 --base-url 服务源站")
        token = _token()
        client = BacktestClient(base_url=args.base_url, token=token, timeout=args.timeout)
        command = args.command
        if command == "capabilities":
            value = client.capabilities()
        elif command in ("preflight", "submit", "run"):
            config = RunConfig.from_dict(_input(args.config))
            value = client.preflight(config) if command == "preflight" else client.submit(config, idempotency_key=args.idempotency_key)
        elif command == "batch":
            payload = _input(args.config)
            if set(payload) != {"configs"} or not isinstance(payload["configs"], list) or not 1 <= len(payload["configs"]) <= 256:
                raise BacktestError("INVALID_RUN_CONFIG", "batch 文件需要 1..256 项的 configs 数组对象")
            value = client.batch([RunConfig.from_dict(item) for item in payload["configs"]], idempotency_key=args.idempotency_key)
        elif command == "status":
            value = client.status(args.run_id)
        elif command == "cancel":
            value = client.cancel(args.run_id)
        elif command == "result":
            value = client.result(args.run_id, kind=args.kind, limit=args.limit, cursor=args.cursor)
        elif command == "batch-status":
            value = client.batch_status(args.batch_id, limit=args.limit, cursor=args.cursor)
        elif command == "cancel-batch":
            value = client.cancel_batch(args.batch_id)
        elif command == "retry-failed":
            value = client.retry_failed(args.batch_id, idempotency_key=args.idempotency_key)
        else:
            value = client.compare(args.run_ids)
        print(json.dumps(redact(value, token), ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return 2 if command == "preflight" and value.get("errors") else 0
    except BacktestError as error:
        diagnostic = error.diagnostic()
    except ContractError as error:
        diagnostic = dict(code=getattr(error, "code", "INVALID_RUN_CONFIG"),
                          operation=getattr(error, "operation", "client"),
                          message="配置、数值或分页不符合 S3 契约，请核对参数类型和范围")
    except (ValueError, TypeError, RecursionError):
        diagnostic = dict(code="INVALID_RUN_CONFIG", message="配置或返回格式无效")
    except KeyboardInterrupt:
        diagnostic = dict(code="INTERRUPTED", message="客户端已中断；服务运行可能继续，请查询状态或显式取消")
    print(json.dumps(redact(diagnostic, token) if token else diagnostic,
                     ensure_ascii=False, allow_nan=False, separators=(",", ":")), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
