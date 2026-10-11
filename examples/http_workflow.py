"""Run one complete config through HTTP; only the server runs the strategy.

Usage: python -I examples/http_workflow.py config.json unique-submission-key
QF_BASE_URL and QF_API_TOKEN come from the process environment/secret manager.
"""
import json
import os
from pathlib import Path
import sys
import time

from quantfoundry import BacktestClient, BacktestError, RunConfig


def workflow(config, *, idempotency_key, client):
    checked = client.preflight(config)
    if checked["errors"]:
        error = checked["errors"][0]
        raise BacktestError(error["code"], error["message"], operation="preflight")
    accepted = client.submit(config, idempotency_key=idempotency_key)
    deadline = time.monotonic()+120
    while time.monotonic() < deadline:
        status = client.status(accepted["run_id"])
        if status["status"] in ("succeeded", "failed", "cancelled"):
            return client.result(accepted["run_id"])
        time.sleep(.1)
    raise BacktestError("WAIT_TIMEOUT", "客户端等待超时；运行可能继续，请查询状态或显式取消")


def main():
    try:
        if len(sys.argv) != 3:
            raise BacktestError("INVALID_ARGUMENTS", "需要 config.json 和唯一提交键；凭据由环境提供")
        config = RunConfig.from_dict(json.loads(Path(sys.argv[1]).read_text()))
        client = BacktestClient(base_url=os.environ.get("QF_BASE_URL"), token=os.environ.get("QF_API_TOKEN", ""))
        result = workflow(config, idempotency_key=sys.argv[2], client=client)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("outcome", {}).get("status") == "succeeded" and not result.get("partial") else 2
    except BacktestError as error:
        print(json.dumps(error.diagnostic(), ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print('{"code":"INVALID_RUN_CONFIG","message":"配置或服务响应无效，请核对文件和环境"}', file=sys.stderr)
        return 2


if __name__ == "__main__": raise SystemExit(main())
