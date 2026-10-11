"""Authorized D11 shard pages and legacy saved-value comparison, no recompute."""
import json
from sqlalchemy import select
from sqlalchemy.orm import Session

from .cursors import decode, encode
from .history.repository import BacktestResultRepository, ResultRepositoryError
from .history.result_records import BacktestAnalysisSummaryRecord, BacktestMetricRecord
from .history import result_schemas
from .history.runs import legacy_root, legacy_summary
from .models import Run
from .repository import identity
from .runtime_contracts import RuntimeFault, TERMINAL
from .serialization import encoded

KINDS = frozenset(("summary", "equity", "orders", "trades", "positions", "records", "logs"))
LEGACY = {"equity": ("equity_curve", result_schemas.BacktestEquityCurveItem),
          "orders": ("orders", result_schemas.BacktestOrderItem),
          "trades": ("fills", result_schemas.BacktestFillItem),
          "positions": ("positions", result_schemas.BacktestPositionItem)}


def read_result(repository, owner, run_id, kind, limit, cursor, key):
    if kind not in KINDS or not 1 <= limit <= 10000:
        raise RuntimeFault("INVALID_CONTRACT", "结果类型或分页大小无效")
    with repository.engine.connect() as c:
        current = c.execute(select(Run.id).where(Run.id == identity(run_id), Run.owner == owner)).scalar_one_or_none()
    if current is None:
        return read_legacy(repository, owner, run_id, kind, limit, cursor, key)
    status, pointer = repository.result_pointer(owner, run_id)
    if status["status"] not in TERMINAL:
        raise RuntimeFault("RESULT_NOT_READY", "运行尚未终结")
    if pointer is None:
        return dict(schema_id="qf.results.v1", run_id=str(current), items=[], next_cursor=None,
                    partial=status["partial"], outcome=dict(status=status["status"], error=status["error"]),
                    summary=None, notices={})
    from quantfoundry import _native
    reader = _native.RetainedResultReader(str(repository.result_root), pointer, str(current))
    scope = dict(owner=owner, run_id=str(current), kind=kind, limit=limit)
    if kind == "summary":
        if cursor:
            raise RuntimeFault("INVALID_CONTRACT", "摘要不接受分页游标")
        result = json.loads(reader.summary_json())
    else:
        after = decode(cursor, scope, key) if cursor else None
        result = json.loads(reader.page_json(kind, after, limit))
        if result["next_cursor"]:
            result["next_cursor"] = encode(result["next_cursor"], scope, key)
    result["run_id"], result["engine_kind"] = str(current), "rust"
    return result


def read_legacy(repository, owner, run_id, kind, limit, cursor, key):
    with Session(repository.engine) as session:
        root = legacy_root(session, owner, run_id)
        if root.status == "indeterminate" or root.terminal_status == "indeterminate":
            raise RuntimeFault("DATA_RESTRICTED", "该历史结果不能进入正式比较")
        if kind == "summary":
            if cursor:
                raise RuntimeFault("INVALID_CONTRACT", "摘要不接受分页游标")
            metrics = session.scalars(select(BacktestMetricRecord).where(BacktestMetricRecord.run_id == root.id)
                                      .order_by(BacktestMetricRecord.metric_key, BacktestMetricRecord.formula_version).limit(501)).all()
            saved = session.scalar(select(BacktestAnalysisSummaryRecord).where(BacktestAnalysisSummaryRecord.run_id == root.id))
            result = dict(schema_id="legacy", **legacy_summary(root), summary=root.result_summary,
                          analysis_summary=result_schemas.BacktestAnalysisSummaryItem.model_validate(saved).model_dump(mode="json") if saved else None,
                          metrics=[result_schemas.BacktestMetricItem.model_validate(m).model_dump(mode="json") for m in metrics[:500]],
                          notices=dict(metrics_truncated=len(metrics)>500))
        elif kind in LEGACY:
            if limit > 500:
                raise RuntimeFault("INVALID_CONTRACT", "历史结果每页最多500条")
            stored, schema = LEGACY[kind]
            scope = dict(owner=owner, run_id=str(root.id), kind=kind, limit=limit)
            after = decode(cursor, scope, key) if cursor else None
            page = BacktestResultRepository(session, cursor_signing_key=key).read_page(stored, run_id=root.id, owner_scope=owner, limit=limit, cursor=after)
            result = dict(schema_id="legacy", run_id=str(root.id), engine_kind="legacy_python", partial=root.status != "succeeded",
                          items=[schema.model_validate(row).model_dump(mode="json") for row in page.items],
                          next_cursor=encode(page.next_cursor, scope, key) if page.next_cursor else None, notices={})
        else:
            raise RuntimeFault("CAPABILITY_UNAVAILABLE", "旧历史没有保存此类结果，不能重算或伪造")
        if len(encoded(result)) > 1024*1024:
            raise RuntimeFault("RESOURCE_LIMIT", "历史结果页超过控制预算，请缩小limit")
        return result


def compare_runs(repository, owner, run_ids, key):
    from .history.runs import read_run
    ids = [str(identity(i)) for i in run_ids]
    if len(set(ids)) != len(ids):
        raise RuntimeFault("INVALID_RUN_CONFIG", "比较运行不能重复")
    # Authorize every root before opening any result directory.
    roots = [read_run(repository, owner, i) for i in ids]
    items = []
    for root in roots:
        saved = read_result(repository, owner, root["run_id"], "summary", 100, None, key)
        config = root.get("normalized_config") or root.get("backtest_config") or {}
        items.append(dict(run_id=root["run_id"], engine_kind=root["engine_kind"], status=root["status"],
                          partial=saved.get("partial", root["partial"]), config=config,
                          summary=saved.get("performance", saved.get("summary")), metrics=saved.get("metrics"),
                          execution_model=saved.get("execution_model"),
                          cost_configuration=root.get("cost_configuration"),
                          policy=(saved.get("performance") or {}).get("policy")))
    baseline = items[0]["config"]
    for item in items:
        keys = set(baseline) | set(item["config"])
        item["configuration_differences"] = {k: dict(baseline=baseline.get(k), current=item["config"].get(k))
                                              for k in sorted(keys) if baseline.get(k) != item["config"].get(k)}
        if items[0]["cost_configuration"] != item["cost_configuration"]:
            item["configuration_differences"]["accepted_cost_configuration"] = dict(baseline=items[0]["cost_configuration"], current=item["cost_configuration"])
    result = dict(items=items, ranking=None, warnings=["旧结果保留原公式；日期、费用、频率和撮合模型差异需独立评估"])
    if len(encoded(result)) > 1024*1024:
        raise RuntimeFault("RESOURCE_LIMIT", "比较结果超过控制预算，请减少运行数量")
    return result
