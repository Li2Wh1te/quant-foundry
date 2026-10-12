"""Read-only bounded verification of one retained S3 result through its API."""

import argparse
from decimal import Decimal
import hashlib
import json
import os


def verify(client, run_id, *, max_pages=10000):
    status = client.status(run_id)
    run_id = status["run_id"]
    summary = client.result(run_id)
    if (
        status["engine_kind"] != "rust"
        or status["status"] != "succeeded"
        or summary.get("partial")
    ):
        raise ValueError(
            "complete successful S3 result required; partial and legacy results stay readable separately"
        )
    counts, hashes = {}, {}
    fees = Decimal(0)
    terminal = None
    for kind in ("equity", "orders", "trades", "positions", "records", "logs"):
        cursor, count, digest = None, 0, hashlib.sha256()
        for _ in range(max_pages):
            page = client.result(run_id, kind=kind, limit=100, cursor=cursor)
            if page["run_id"] != run_id or page.get("partial"):
                raise ValueError("result identity or completeness changed")
            for row in page["items"]:
                digest.update(
                    json.dumps(
                        row, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                )
                digest.update(b"\n")
                if kind == "trades":
                    fees += Decimal(row["fee"])
                if kind == "equity":
                    terminal = row["equity"]
            count += len(page["items"])
            following = page["next_cursor"]
            if following is None:
                break
            if following == cursor:
                raise ValueError("result cursor did not advance")
            cursor = following
        else:
            raise ValueError("verification page budget exhausted")
        if count != summary["counts"].get(kind, 0):
            raise ValueError("saved result count differs from retained pages")
        counts[kind], hashes[kind] = count, digest.hexdigest()
    performance = summary.get("performance")
    if (
        performance is None
        or terminal is None
        or Decimal(terminal) != Decimal(performance["terminal_equity"])
    ):
        raise ValueError("terminal equity is unavailable or differs from summary")
    if fees != Decimal(performance["trades"]["fill_fees"]):
        raise ValueError("exact fees differ from saved trade summary")
    return dict(
        run_id=run_id,
        verified=True,
        counts=counts,
        page_sha256=hashes,
        terminal_equity=terminal,
        fill_fees=str(fees),
        read_only=True,
    )


def main():
    from quantfoundry import BacktestClient, BacktestError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    args = parser.parse_args()
    try:
        client = BacktestClient(
            base_url=os.environ.get("QF_BASE_URL"),
            token=os.environ.get("QF_API_TOKEN", ""),
        )
        print(json.dumps(verify(client, args.run_id), ensure_ascii=False))
        return 0
    except (BacktestError, ValueError, KeyError, TypeError):
        print(
            '{"verified":false,"message":"结果未完成或核对失败；保留结果并检查状态和诊断"}'
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
