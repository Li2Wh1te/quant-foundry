"""One v2 admission path for HTTP, the SDK contract and the trusted runner."""
from copy import deepcopy
import hashlib
import json
from threading import Event
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.strategies.models import Strategy, StrategyRevision
from app.strategies.parameter_contract import validate_parameters
from app.strategies.validation import validate_s3_source
from .accounts.service import AccountProfileService, AccountProfileNotFoundError
from .repository import MAX_WORK_BYTES, identity
from .runtime_contracts import AcceptedWork, RuntimeFault, WorkerLimits
from .serialization import canonical_hash, encoded


def normalize(payload):
    if type(payload) is not dict:
        raise RuntimeFault("INVALID_RUN_CONFIG", "运行配置必须是JSON对象")
    schema = payload.get("api_schema")
    if schema is None or schema in ("qf.backtest.v1", "qf.backtest.v0"):
        raise RuntimeFault("LEGACY_ENGINE_RETIRED", "旧计算写入接口已退役，请使用qf.backtest.v2")
    if schema != "qf.backtest.v2":
        raise RuntimeFault("INVALID_RUN_CONFIG", "未知运行配置版本")
    # The installed D01 validator is the only numeric/shape/default policy.
    # Loading it never imports private strategy modules or the legacy engine.
    try:
        from quantfoundry import _native
    except ImportError:
        raise RuntimeFault("CAPABILITY_UNAVAILABLE", "S3预构建制品尚未安装") from None
    try:
        config = json.loads(_native.validate_run_config_json(encoded(payload).decode()))
    except (ValueError, TypeError, RecursionError):
        raise RuntimeFault("INVALID_RUN_CONFIG", "运行配置格式、数值、日期或参数预算无效") from None
    # DB namespace IDs are canonicalized once; they are never paths or SQL.
    for name in ("account_id", "strategy_revision_id"):
        try:
            config[name] = str(identity(config[name]))
        except RuntimeFault:
            raise RuntimeFault("INVALID_RUN_CONFIG", "账户与策略修订标识须为有效UUID") from None
    return config


class FormalProvider:
    """One CurrentStore admission/runtime path for accepted formal node plans.

    The default registry has no accepted raw execution/identity/session facts
    yet. It explicitly refuses those facts. A trusted code-owned plan resolver
    can compose accepted D02/D03/D04 DTOs; REST, parameters and account metadata
    cannot choose a resolver, column, unit, path or execution fact. Isolated
    tests exercise this provider itself against real E50/E70 node layouts.
    """
    def __init__(self, settings, *, engine=None, plan_resolver=None, store=None, process=None):
        self.settings, self.engine = settings, engine
        self.plan_resolver, self.store, self.process = plan_resolver, store, process

    def capabilities(self):
        from .market_projection import current_market_bindings
        return [dict(binding=b.name, frequency=b.frequency, available=False,
                     reason=b.unavailable_reason, limitations=list(b.limitations))
                for b in current_market_bindings()]

    def _plan(self, work):
        from .formal_runtime import AcceptedRunPlan
        from .market_projection import current_market_bindings
        if self.plan_resolver is None:
            candidates = [b for b in current_market_bindings() if b.frequency == work.config['frequency']]
            reasons = ', '.join(b.name + ':' + b.unavailable_reason for b in candidates if b.unavailable_reason)
            raise RuntimeFault('CAPABILITY_UNAVAILABLE',
                '缺少已接受的正式执行事实：' + (reasons or '该频率没有正式输入契约'))
        plan = self.plan_resolver(work)
        if not isinstance(plan, AcceptedRunPlan):
            raise RuntimeFault('INVALID_CONTRACT', '可信正式事实解析器未返回受限运行计划')
        if plan.spec.get('synthetic_model') and getattr(self.settings, 'environment', None) != 'test':
            raise RuntimeFault('CAPABILITY_UNAVAILABLE', '合成执行事实只允许明确的隔离测试环境')
        plan.validate(work)
        return plan

    def _preflight_process(self):
        if self.process is not None:
            self.process.daemon.preflight(self.process.image)
            return
        # Web has no daemon socket or host work mount. It consumes only the
        # trusted runner's short-lived check in the existing shared result root.
        from .runtime_readiness import check_runtime_ready
        check_runtime_ready(self.settings.s3_result_root,
            self.settings.cursor_signing_key.get_secret_value().encode(), self.settings.s3_worker_image)

    def _gateway(self, work, run_id, stopped, plan):
        from app.core.auth import AuthenticatedPrincipal
        from app.data_store.storage import CurrentStore
        from app.db.session import get_engine
        from .data_gateway import AuthorizedRun
        from .formal_runtime import FormalDailyGateway, RunStoreGateway
        calendar = plan.spec['calendar']
        owned = self.store is None
        store = self.store or CurrentStore(self.engine or get_engine(), self.settings.data_store_root,
            cursor_key=self.settings.cursor_signing_key.get_secret_value().encode(), initialize=False)
        try:
            kind=(FormalDailyGateway if plan.market.frequency=='1d' and plan.market.query_time_bounds
                  else RunStoreGateway)
            gateway = kind(store, AuthenticatedPrincipal(work.owner),
                AuthorizedRun(run_id, work.owner, tuple(work.config['universe']),
                    int(calendar[0]['before_open_ns']), int(calendar[-1]['after_close_ns'])),
                (plan.market,), cancelled=stopped.is_set, owned_store=owned,
                check_facts=lambda connection: plan.check_facts(work, connection=connection),
                batch_rows=min(10000, store.limits.query_rows),cache_rows=100000,cache_bytes=64*1024*1024,
                **({'calendar':calendar} if kind is FormalDailyGateway else {}))
            context = gateway.open(dict(run_id=run_id, universe=work.config['universe']),
                dependencies=(plan.market.name,))
            return gateway, context
        except BaseException:
            if owned:
                store.close()
            raise

    def check(self, work):
        plan = self._plan(work)
        self._preflight_process()
        gateway, context = self._gateway(work, 'preflight-' + uuid4().hex, Event(), plan)
        try:
            request = plan.packet(work, 'preflight')['source']['request']
            # No strategy import, run insertion, source mutation or full-history
            # materialization. The first bounded source page uses the real gate.
            stream = gateway.read(plan.market.name, request, context, page_rows=1)
            try:
                page = next(stream)
                if not page.table.num_rows:
                    raise RuntimeFault('CAPABILITY_UNAVAILABLE', '正式运行首个可见会话缺少行情')
            finally:
                stream.close()
            gateway.check(context)
        finally:
            gateway.close(context)
        return plan.spec['calendar_reference']

    def binding(self, work, claim, stopped):
        from .supervisor import Binding
        plan = self._plan(work)
        self._preflight_process()
        gateway, context = self._gateway(work, claim.run_id, stopped, plan)
        packet = plan.packet(work, claim.run_id)
        return Binding(packet, market_routes={work.config['frequency']: dict(binding=plan.market.name,
            fields=packet['source']['request']['fields'])}, gateway=gateway, context=context,
            session_facts=plan.session_facts)


class RunService:
    def __init__(self, repository, provider, *, limits=None, accepting=True):
        self.repository, self.provider = repository, provider
        self.limits = limits or WorkerLimits()
        self.accepting = accepting

    def require_admission(self):
        if not self.accepting:
            raise RuntimeFault("CAPABILITY_UNAVAILABLE", "新运行接单已暂停；状态、取消和历史结果仍可访问")

    def capabilities(self):
        try:
            from quantfoundry import _native  # noqa: F401
            installed = True
        except ImportError:
            installed = False
        return dict(api_schema="qf.backtest.v2", engine=dict(installed=installed,
                    frequencies=["1d", "1m", "5m", "15m", "30m", "60m", "tick"],
                    execution_models=["bar_next_interval_v1", "trade_tick_v1", "quote_tick_v1"],
                    result_kinds=["summary", "equity", "orders", "trades", "positions", "records", "logs"],
                    fields=["open", "high", "low", "close", "price", "bid", "ask", "quantity", "bid_quantity", "ask_quantity"],
                    parameter_schema=dict(type="object", maxProperties=128),
                    errors=["CAPABILITY_UNAVAILABLE", "DATA_RESTRICTED", "DATA_CHANGED", "RULE_UNAVAILABLE",
                            "INVALID_RUN_CONFIG", "IDEMPOTENCY_CONFLICT", "RESOURCE_LIMIT", "CANCELLED",
                            "LOOKAHEAD_FORBIDDEN", "NUMERIC_RANGE_UNSUPPORTED", "INSUFFICIENT_CASH",
                            "INSUFFICIENT_SELLABLE", "INVALID_ORDER", "STRATEGY_ERROR",
                            "RESULT_BUDGET_EXCEEDED", "LEGACY_ENGINE_RETIRED"]),
                    actual_data=self.provider.capabilities(),
                    limits=dict(control_bytes=1024*1024, parameter_bytes=64*1024, parameter_depth=8,
                                batch_runs=256, compare_runs=16, result_page_items=10000))

    def _capture(self, owner, config):
        with Session(self.repository.engine) as session:
            try:
                account = AccountProfileService(session).get(identity(config["account_id"]))
            except AccountProfileNotFoundError:
                raise RuntimeFault("DATA_RESTRICTED", "账户配置不存在或不可使用") from None
            # The existing account and strategy catalogues are deployment-wide
            # admin resources. Only runs/results are private to owner_scope;
            # adding invented per-user catalogue ownership would change old ACLs.
            if account.status != "active":
                raise RuntimeFault("RULE_UNAVAILABLE", "所选账户配置不可用于新运行")
            revision = session.get(StrategyRevision, identity(config["strategy_revision_id"]))
            strategy = session.get(Strategy, revision.strategy_id) if revision else None
            if revision is None or strategy is None or strategy.state != "active":
                raise RuntimeFault("DATA_RESTRICTED", "策略修订不存在或不可使用")
            if (hashlib.sha256(revision.source_code.encode()).hexdigest() != revision.source_hash
                    or len(revision.source_code.encode()) > 256*1024):
                raise RuntimeFault("STRATEGY_ERROR", "策略修订完整性或大小不符合运行要求")
            issues = validate_parameters(revision.parameter_schema or {}, config["parameters"])
            validation = validate_s3_source(revision.source_code)
            if issues or not validation.valid:
                raise RuntimeFault("STRATEGY_ERROR", "参数或策略回调不符合S3契约；旧策略原文需保存新的修订")
            accepted = dict(account_version=int(account.version), source_hash=revision.source_hash,
                            account_snapshot=dict(id=str(account.id), version=int(account.version),
                                fee_schedule=dict(key=account.fee_schedule_key, version=account.fee_schedule_version,
                                                  fee_rules=deepcopy(account.fee_rules), metadata=deepcopy(account.fee_schedule_metadata)),
                                metadata=deepcopy(account.profile_metadata)))
            if len(encoded(accepted)) > 64*1024:
                raise RuntimeFault("RESOURCE_LIMIT", "账户配置超过运行控制预算")
            return AcceptedWork(owner, deepcopy(config), revision.source_code, self.limits, accepted)

    def accepted_work(self, owner, config, *, for_submission=True):
        if for_submission:
            self.require_admission()
        work = self._capture(owner, config)
        # Recheck actual dependencies and sandbox/resources at every admission.
        # check never imports user code and creates no task. Runtime binding
        # later checks them again, using this frozen account/strategy only.
        try:
            calendar = self.provider.check(work)
        except RuntimeFault:
            raise
        except ValueError as error:
            code = getattr(error, "code", None)
            if code in {"DATA_CHANGED", "DATA_RESTRICTED", "RULE_UNAVAILABLE", "CAPABILITY_UNAVAILABLE", "RESOURCE_LIMIT", "CANCELLED"}:
                raise RuntimeFault(code, "实际数据、规则、费用或运行依赖未通过检查") from None
            raise
        if type(calendar) is not str or not calendar:
            raise RuntimeFault("RULE_UNAVAILABLE", "参考日历缺失或有歧义")
        normalized = deepcopy(work.config)
        if "reference_calendar" in normalized and normalized["reference_calendar"] != calendar:
            raise RuntimeFault("RULE_UNAVAILABLE", "所选参考日历不适用于本运行")
        normalized["reference_calendar"] = calendar
        normalized = normalize(normalized)
        if len(encoded(dict(config=normalized, accepted=work.specification,
                            strategy_source=work.strategy_source, limits=vars(work.limits)))) > MAX_WORK_BYTES - 16*1024:
            raise RuntimeFault("RESOURCE_LIMIT", "已受理控制配置超过预算")
        return AcceptedWork(work.owner, normalized, work.strategy_source, work.limits, work.specification)

    def preflight(self, owner, config):
        try:
            work = self.accepted_work(owner, config, for_submission=False)
            warnings=["current_revision_not_historical_snapshot"]
            if not self.accepting:
                warnings.append("new_run_admission_paused")
            return dict(errors=[], warnings=warnings, normalized_config=work.config)
        except RuntimeFault as error:
            return dict(errors=[error.diagnostic()], warnings=[], normalized_config=config)

    def submit(self, owner, config, key):
        digest = canonical_hash(config)
        previous = self.repository.existing(owner, key, digest, config=config)
        if previous:
            return self.repository.status(owner, previous["id"])
        return self.repository.submit(self.accepted_work(owner, config), key, digest)

    def batch(self, owner, configs, key):
        digest = canonical_hash(configs)
        previous = self.repository.existing(owner, key, digest, batch=True, config=configs)
        if previous:
            return str(previous["id"])
        # Validate every item before the single atomic queue insertion.
        works = [self.accepted_work(owner, config) for config in configs]
        if any(w.specification != works[0].specification for w in works):
            raise RuntimeFault("DATA_CHANGED", "批次受理期间账户或策略配置发生变化")
        return self.repository.submit_batch(works, key, digest)
