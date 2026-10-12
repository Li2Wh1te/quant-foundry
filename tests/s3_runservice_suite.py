"""D13 real private PostgreSQL + HTTP + D04/D11 ports, synthetic facts only.

The same suite runs in backend CI and the actual D12 runner container. The
worker test is explicitly required there; host tests never claim that topology.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

# Every regression in this suite (HTTP/SDK/CLI/real worker included) runs with
# both retired namespaces blocked, not merely selected algorithm modules.
class _NoRetiredNamespace:
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p+'.') for p in ('app.backtesting','app.strategy_protocol')):
            raise ImportError('retired execution import: '+fullname)

sys.meta_path.insert(0, _NoRetiredNamespace())

import pyarrow as pa
import requests
from sqlalchemy import create_engine, insert, select, text, update
from sqlalchemy.orm import Session
import uvicorn

from app.backtest_service.account_config import commission_from_account_fee_schedule
from app.backtest_service.accounts.service import AccountProfileService, AccountProfileReferencedError
from app.backtest_service.accounts.types import AccountProfileStatus
from app.backtest_service.data_gateway import AuthorizedRun, MARKET_SCHEMA, MarketBinding, RunDataGateway
from app.backtest_service.models import Run, RunResult
from app.backtest_service.repository import DatabaseRunRepository, identity
from app.backtest_service.runtime_contracts import Claim, RuntimeFault, GroupBudget, WorkerLimits
from app.backtest_service.runtime_services import create_services
from app.backtest_service.serialization import encoded
from app.backtest_service.service import RunService, normalize
from app.backtest_service.supervisor import Binding, RunnerSupervisor
from app.backtest_service.history.storage_models import BacktestRunRecord
from app.backtest_service.history.result_records import BacktestEquityCurveRecord
from app.core.auth import AuthenticatedPrincipal
from app.core.config import Settings, get_settings
from app.data_store.catalog import SourceUpdate
from app.data_store.schema import DatasetSpec, fingerprint
from app.data_store.storage import CurrentStore
from app.db.session import get_db_session
from app.main import create_app
from app.strategies.service import StrategyStorageService
from quantfoundry import _native
from quantfoundry.client import RunConfig

try:
    from support_sdk import spec, batch
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"sdk/python/tests"))
    from support_sdk import spec, batch

TOKEN = "d13-private-http-token-" + "a"*40
CURSOR = "d13-private-cursor-key-" + "b"*40
OWNER = "token:"+hashlib.sha256(TOKEN.encode()).hexdigest()


def account_payload(minimum="1"):
    return dict(key="d13-synthetic-commission", fee_rules=[dict(key="commission", category="commission", side="both",
        rate="0", minimum=minimum, rounding_level="fee_item", rounding_scope="on_fill", rounding_mode="half_up", rounding_precision="0.01")],
        metadata=dict(currency="CNY"))


def catalogue(engine, source="def initialize(c): pass", minimum="1"):
    with Session(engine) as s:
        account = AccountProfileService(s).create(name="d13-"+uuid4().hex, status=AccountProfileStatus.ACTIVE,
                                                 fee_schedule=account_payload(minimum), metadata={"original": "保留账户文本"})
        strategy = StrategyStorageService(s).create_strategy(name="d13-"+uuid4().hex, source_code=source,
                         parameter_schema={"type": "object"}, default_parameters={})
        revision = StrategyStorageService(s).publish_revision(strategy.id, expected_draft_version=1)
        s.commit()
        return account.id, revision.id


class PrivateProvider:
    """Explicit synthetic acceptance, real CurrentStore/gateway and commission adapter."""
    def __init__(self, engine, store, template, name):
        self.engine, self.store, self.template = engine, store, template
        self.name, self.available, self.process = name, True, None
        fields = [pa.field(f.name, f.type, nullable=f.name not in ("security", "time_ns", "sequence"),
                    metadata={b"max_utf8_bytes": b"128"} if pa.types.is_string(f.type) else
                             {b"unit": b"epoch_ns"} if f.name.endswith("_ns") else None) for f in MARKET_SCHEMA]
        self.dataset = DatasetSpec(name, pa.schema(fields, metadata=MARKET_SCHEMA.metadata),
                         ("security", "time_ns", "sequence"), "synthetic-d13-service",
                         {"price_basis": "raw", "price_currency": "CNY", "quantity_unit": "shares"})
        self.store.register(self.dataset)
        table = pa.ipc.open_stream(batch(template["source"]["events"])).read_all().cast(self.dataset.schema)
        self.store.upsert(self.dataset, "default", table.to_batches(),
                          SourceUpdate("d13-test", fingerprint(name), fingerprint("synthetic"), 0),
                          lower=("A.SH",), upper=("A.SH\0",), source_check=lambda _: True)
        self.market = MarketBinding("market", self.dataset, "1d", {f: f for f in MARKET_SCHEMA.names}, {},
                         lambda security, start, end: ((security, start), (security, end+1)), "time_ns")

    def capabilities(self):
        return [dict(binding=self.name, available=self.available, synthetic=True)]

    def gateway(self, work, run_id, stopped):
        grant = AuthorizedRun(run_id, work.owner, tuple(work.config["universe"]),
                   int(self.template["calendar"][0]["before_open_ns"]), int(self.template["calendar"][-1]["after_close_ns"]))
        gw = RunDataGateway(self.store, AuthenticatedPrincipal(work.owner), grant, (self.market,),
                            batch_rows=2, cancelled=stopped.is_set)
        ctx = gw.open(dict(run_id=run_id, universe=work.config["universe"]), dependencies=("market",))
        return gw, ctx

    def check(self, work):
        if not self.available:
            raise RuntimeFault("DATA_RESTRICTED", "private dependency restricted")
        if self.process:
            self.process.daemon.preflight(self.process.image)
        gw, ctx = self.gateway(work, "preflight-"+uuid4().hex, Event())
        try:
            request = self.request(work)
            pages = list(gw.read("market", request, ctx, page_rows=2))
            if not sum(page.table.num_rows for page in pages):
                raise RuntimeFault("CAPABILITY_UNAVAILABLE", "required data missing")
            self.commission(work, "buy")
            self.commission(work, "sell")
            gw.check(ctx)
        finally:
            gw.close(ctx)
        return self.template["calendar_reference"]

    def commission(self, work, side):
        return commission_from_account_fee_schedule(work.specification["account_snapshot"]["fee_schedule"],
            side=side, included_components=(), cost_overrides=work.config.get("cost_overrides"))

    def request(self, work):
        return dict(securities=work.config["universe"], fields=["open", "high", "low", "close", "quantity"],
                    frequency="1d", start_ns=self.template["calendar"][0]["before_open_ns"],
                    end_ns=self.template["calendar"][-1]["after_close_ns"], count_per_security=None, adjustment="none")

    def binding(self, work, claim, stopped):
        if not self.available:
            raise RuntimeFault("DATA_RESTRICTED", "private dependency restricted")
        gw, ctx = self.gateway(work, claim.run_id, stopped)
        source = deepcopy(self.template)
        source.update(run_id=claim.run_id, config=work.config)
        source["source"] = dict(kind="ipc", binding="market", socket_path="/run/qf/ipc/data.sock", request=self.request(work))
        for row in source["facts"]:
            for side in ("buy", "sell"):
                commission = self.commission(work, side)
                row[side+"_fees"].update({k: commission[k] for k in ("commission_rate", "minimum_commission", "settlement_scale", "rounding")})
        return Binding(source, market_routes={"1d": dict(binding="market", fields=self.request(work)["fields"])}, gateway=gw, context=ctx)


class D13Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = get_settings()
        if base.environment != "test" or "test" not in base.database_name:
            raise RuntimeError("D13 regressions require a private test database")
        from sqlalchemy.engine import make_url
        self_url = make_url(base.database_url.render_as_string(hide_password=False))
        if base.database_host.startswith("/"):
            if self_url.query.get("host") != base.database_host:
                raise RuntimeError("private migration socket was lost in URL serialization")
        cls.dbname = "qf_d13_test_"+uuid4().hex
        cls.admin = create_engine(base.database_url, isolation_level="AUTOCOMMIT")
        with cls.admin.connect() as c:
            c.execute(text("CREATE DATABASE "+cls.dbname))
        cls.engine = create_engine(base.database_url.set(database=cls.dbname))
        cls.environment = dict(os.environ, QF_DATABASE_NAME=cls.dbname)
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.alembic = cls.repo_root/"backend/alembic.ini"
        if not cls.alembic.exists():
            cls.alembic = Path("/app/alembic.ini")
        cls.migrate("20261011_01")
        cls.old_account, cls.old_revision = catalogue(cls.engine, "def run(context, parameters): pass")
        from app.backtest_service.accounts.fee_catalog import FeeScheduleVersionRecord
        cls.old_fee_key = "d13-original-"+uuid4().hex
        cls.old_fee_snapshot = dict(account_payload("5.000000000000000123"), key=cls.old_fee_key, version=1)
        cls.old_fee_hash = hashlib.sha256(encoded(cls.old_fee_snapshot)).hexdigest()
        cls.old_run, cls.old_point, cls.old_failure, cls.old_comparison = uuid4(), uuid4(), uuid4(), uuid4()
        with Session(cls.engine) as s:
            s.add(FeeScheduleVersionRecord(key=cls.old_fee_key, version=1, name_zh="原费用目录", name_en="Original fee",
                          snapshot=cls.old_fee_snapshot, snapshot_hash=cls.old_fee_hash))
            s.add(BacktestRunRecord(id=cls.old_run, run_kind="backtest_run", profile="formal@1", status="succeeded", terminal_status="succeeded",
                  idempotency_scope=OWNER, tenant_id=OWNER, idempotency_key=uuid4().hex, config_hash="a"*64,
                  account_profile_id=str(cls.old_account), account_profile_version="1", strategy_revision_id=str(cls.old_revision),
                  initial_cash=Decimal("10000.000000000000000123"), finished_at=datetime.now(timezone.utc),
                  backtest_config={"unchanged": "旧结果不重算"}, result_summary={"value": "123456789.123456789012345678", "time_ns": "1767225600000000123"}))
            s.flush()
            s.add(BacktestEquityCurveRecord(id=cls.old_point, run_id=cls.old_run, sequence=1,
                  as_of=datetime(2026,1,1,tzinfo=timezone.utc), valuation_status="complete",
                  cash=Decimal("123456789.123456789012345678"), market_value=Decimal("0"),
                  equity=Decimal("123456789.123456789012345678"), cumulative_fees=Decimal("1.000000000000000001")))
            s.add(BacktestRunRecord(id=cls.old_failure, run_kind="backtest_run", profile="formal@1", status="failed", terminal_status="failed",
                  idempotency_scope=OWNER, tenant_id=OWNER, idempotency_key=uuid4().hex, config_hash="b"*64,
                  account_profile_id=str(cls.old_account), account_profile_version="1", strategy_revision_id=str(cls.old_revision),
                  initial_cash="9999.123456789012345678", finished_at=datetime.now(timezone.utc),
                  error_message="旧失败原文：余额不足；不重新执行", failure_evidence={"line":17,"time_ns":"1767225600000000456"},
                  result_summary={"value":"9998.123456789012345678","original":"保留失败部分结果"}))
            s.flush()
            from app.backtest_service.history.saved_comparisons import SavedComparison, SavedComparisonMember
            s.add(SavedComparison(id=cls.old_comparison, owner_scope=OWNER, name="旧比较：中文/原顺序", baseline_run_id=cls.old_run))
            s.flush()
            s.add_all([SavedComparisonMember(comparison_id=cls.old_comparison,run_id=cls.old_failure,position=0),
                       SavedComparisonMember(comparison_id=cls.old_comparison,run_id=cls.old_run,position=1)])
            s.commit()
        cls.original_values = cls.legacy_values()
        if any(not rows for rows in cls.original_values.values()):
            raise AssertionError('D17 historical preservation requires nonempty original samples in every protected relation')
        # Also upgrade a nonempty previous S3 schema, including its immutable
        # failed business run; adding progress must not rewrite its old values.
        cls.migrate('20261012_01')
        prior_id=uuid4()
        prior_config=spec(days=1,securities=('A.SH',))['config']
        prior_config.update(account_id=str(cls.old_account),strategy_revision_id=str(cls.old_revision),
                            parameters={'original':'S3 原失败','time_ns':'1767225600000000123'})
        with cls.engine.begin() as c:
            c.execute(text('''INSERT INTO s3_runs
                (id,owner,idempotency_key,request_hash,account_id,account_version,strategy_revision_id,
                 config,accepted,strategy_source,limits,group_policy,status,outcome,finished_at)
                VALUES (:id,:owner,:key,:hash,:account,1,:revision,CAST(:config AS jsonb),
                    CAST(:accepted AS jsonb),:source,CAST(:limits AS jsonb),CAST(:policy AS jsonb),
                    'failed',CAST(:outcome AS jsonb),CURRENT_TIMESTAMP)'''),
                dict(id=prior_id,owner=OWNER,key=uuid4().hex,hash='c'*64,account=cls.old_account,
                    revision=cls.old_revision,config=json.dumps(prior_config,ensure_ascii=False),
                    accepted=json.dumps({'account_snapshot':{'fee_schedule':cls.old_fee_snapshot}},ensure_ascii=False),
                    source='def initialize(c): pass # 原修订不改写',limits=json.dumps(vars(WorkerLimits())),
                    policy=json.dumps(vars(GroupBudget())),outcome=json.dumps(dict(status='failed',partial=True,
                        error=RuntimeFault('STRATEGY_ERROR','S3 原失败，不重新计算').diagnostic()),ensure_ascii=False)))
            prior_values=dict(c.execute(text('SELECT * FROM s3_runs WHERE id=:id'),{'id':prior_id}).mappings().one())
        cls.migrate("head")
        with cls.engine.connect() as c:
            upgraded=dict(c.execute(text('SELECT * FROM s3_runs WHERE id=:id'),{'id':prior_id}).mappings().one())
        if upgraded.pop('progress') is not None or upgraded!=prior_values:
            raise AssertionError('S3 progress migration rewrote a retained prior run')
        if cls.legacy_values() != cls.original_values:
            raise AssertionError('D17 migration changed original IDs/text/money/associations')
        if os.environ.get('QF_D17_EVIDENCE'):
            evidence=Path(os.environ['QF_D17_EVIDENCE']);evidence.mkdir(parents=True,exist_ok=True)
            (evidence/'legacy-upgrade-values.json').write_text(json.dumps(dict(kind='isolated_old_database_upgrade',production_executed=False,
                before=cls.original_values,after=cls.legacy_values(),retired_namespaces_blocked=True,
                prior_s3_run_before=prior_values,prior_s3_run_after=upgraded),ensure_ascii=False,indent=2,default=str)+'\n')
        cls.base = Path(os.environ.get("QF_D13_TEST_ROOT") or tempfile.mkdtemp(prefix="qf-d13-service-"))
        cls.base.mkdir(exist_ok=True)
        cls.settings = Settings(api_token=TOKEN, cursor_signing_key=CURSOR, database_password="private",
             s3_result_root=cls.base/"results", _env_file=None)
        (cls.base/"results").mkdir()
        cls.repository = DatabaseRunRepository(cls.engine, result_root=cls.settings.s3_result_root)
        cls.store_root = cls.base/"store"
        cls.store_root.mkdir()
        if os.environ.get("LF_D01_ALLOW_OVERLAY_TEST") == "1":
            cls.fs_probe = patch("app.data_store.locking._filesystem_name", return_value="ext4")
            cls.fs_probe.start()
        from app.legacy_reset.tables import maintenance
        with cls.engine.begin() as c:
            c.execute(maintenance.insert().values(singleton=1, phase="ready", completed_json=[], files_started=False))
        cls.store = CurrentStore(cls.engine, cls.store_root, cursor_key=CURSOR.encode(), initialize=True)

    @classmethod
    def migrate(cls, target):
        script = '''import sys
class Block:
    def find_spec(self,fullname,path=None,target=None):
        if any(fullname==p or fullname.startswith(p+'.') for p in ('app.backtesting','app.strategy_protocol')):
            raise ImportError('retired namespace in migration: '+fullname)
sys.meta_path.insert(0,Block())
from alembic.config import main
main(sys.argv[1:])
'''
        result = subprocess.run([sys.executable, "-c", script, "-c", str(cls.alembic), "upgrade", target],
                    env=cls.environment, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def legacy_values(cls):
        queries = {
            'account':'SELECT * FROM backtest_account_profiles WHERE id=:account',
            'account_versions':'SELECT * FROM backtest_account_profile_versions WHERE profile_id=:account ORDER BY version',
            'fee':'SELECT * FROM backtest_fee_schedule_versions WHERE key=:fee ORDER BY version',
            'strategy':'SELECT * FROM strategies WHERE id=(SELECT strategy_id FROM strategy_revisions WHERE id=:revision)',
            'draft':'SELECT * FROM strategy_drafts WHERE strategy_id=(SELECT strategy_id FROM strategy_revisions WHERE id=:revision)',
            'revision':'SELECT * FROM strategy_revisions WHERE id=:revision',
            'runs':'SELECT * FROM backtest_runs WHERE id IN (:run,:failure) ORDER BY id',
            'equity':'SELECT * FROM backtest_equity_curve WHERE run_id=:run ORDER BY sequence',
            'comparison':'SELECT * FROM backtest_saved_comparisons WHERE id=:comparison',
            'members':'SELECT * FROM backtest_saved_comparison_members WHERE comparison_id=:comparison ORDER BY position',
        }
        params=dict(account=cls.old_account,fee=cls.old_fee_key,revision=cls.old_revision,run=cls.old_run,failure=cls.old_failure,comparison=cls.old_comparison)
        with cls.engine.connect() as c:
            return {key:[dict(row) for row in c.execute(text(query),params).mappings()] for key,query in queries.items()}

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        if hasattr(cls, "fs_probe"):
            cls.fs_probe.stop()
        cls.engine.dispose()
        with cls.admin.connect() as c:
            c.execute(text("DROP DATABASE "+cls.dbname))
        cls.admin.dispose()

    def setUp(self):
        self.account, self.revision = catalogue(self.engine)
        self.template = spec(days=3, securities=("A.SH",))
        self.template["config"].update(account_id=str(self.account), strategy_revision_id=str(self.revision))
        self.provider = PrivateProvider(self.engine, self.store, self.template, "d13_"+uuid4().hex)
        self.service = RunService(self.repository, self.provider)
        self.app = create_app(self.settings)
        self.app.state.s3_repository, self.app.state.s3_provider = self.repository, self.provider
        def session():
            with Session(self.engine) as s:
                yield s
        self.app.dependency_overrides[get_db_session] = session
        sock = socket.socket(); sock.bind(("127.0.0.1", 0))
        self.url = "http://127.0.0.1:"+str(sock.getsockname()[1])
        self.server = uvicorn.Server(uvicorn.Config(self.app, lifespan="off", log_level="error", access_log=False))
        self.thread = Thread(target=lambda: self.server.run(sockets=[sock]), daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            Event().wait(.02)
        self.assertTrue(self.server.started)
        self.runs = []

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        for run_id in self.runs:
            row = self.repository.request_cancel(OWNER, run_id)
            if row["status"] not in ("succeeded", "failed", "cancelled"):
                with self.engine.connect() as c:
                    data = self.repository._row(c, run_id)
                self.repository.commit_outcome(Claim(run_id, data["token"], data["fence"]), dict(status="cancelled", partial=True), None)

    def http(self, method, path="", payload=None, *, key=None, token=TOKEN):
        headers = {"Authorization": "Bearer "+token} if token else {}
        if key:
            headers["Idempotency-Key"] = key
        return requests.request(method, self.url+"/api/admin/backtests"+path, json=payload, headers=headers, timeout=10)

    def submit(self, parameters=None, key=None):
        config = deepcopy(self.template["config"])
        if parameters is not None:
            config["parameters"] = parameters
        # The actual installed SDK's RunConfig produces the HTTP request body.
        config = RunConfig(**config).to_dict()
        response = self.http("POST", payload=config, key=key or uuid4().hex)
        self.assertEqual(response.status_code, 202, response.text)
        row = response.json(); self.runs.append(row["run_id"])
        return row, config

    def test_admission_pause_preserves_status_cancel_and_authenticated_reads(self):
        row,config=self.submit()
        self.app.state.settings=self.settings.model_copy(update={'s3_accept_new_runs':False})
        paused=self.http('POST',payload=config,key=uuid4().hex)
        self.assertEqual(paused.status_code,503,paused.text)
        self.assertEqual(self.http('POST','/batches',payload={'configs':[config]},key=uuid4().hex).status_code,503)
        self.assertEqual(self.http('POST','/batches/'+str(uuid4())+'/retry-failed',key=uuid4().hex).status_code,503)
        preview=self.http('POST','/preflight',payload=config).json()
        self.assertFalse(preview['errors'])
        self.assertIn('new_run_admission_paused',preview['warnings'])
        self.assertEqual(self.http('GET','/'+row['run_id']).status_code,200)
        self.assertEqual(self.http('POST','/'+row['run_id']+'/cancel').status_code,200)
        self.assertEqual(self.http('GET','/capabilities',token=None).status_code,401)

    def test_http_schema_idempotency_parameter_and_owner_boundaries(self):
        from app.backtest_service.openapi import RUN_SCHEMA
        if (self.repo_root/"contracts/run_request.schema.json").exists():
            self.assertEqual(RUN_SCHEMA, json.loads((self.repo_root/"contracts/run_request.schema.json").read_text()))
        documented = self.app.openapi()["paths"]["/api/admin/backtests"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(documented["properties"]["api_schema"]["const"], "qf.backtest.v2")
        self.assertEqual(self.http("POST", payload={}).status_code, 410)
        self.assertEqual(self.http("POST", payload={}, token=None).status_code, 401)
        bad = dict(self.template["config"], api_schema="qf.backtest.v99")
        self.assertEqual(self.http("POST", payload=bad).status_code, 422)
        for value in (None, 10000, True, "0", "NaN", "1e4"):
            bad = dict(self.template["config"], initial_cash=value)
            self.assertEqual(self.http("POST", payload=bad).status_code, 422)
        key = uuid4().hex
        row, config = self.submit({"exact_integer": 9007199254740993, "large_integer": 10**100}, key)
        self.assertEqual(row["normalized_config"]["parameters"]["large_integer"], 10**100)
        self.assertEqual(self.http("POST", payload=config, key=key).json()["run_id"], row["run_id"])
        self.assertEqual(self.http("POST", payload=row["normalized_config"], key=key).json()["run_id"], row["run_id"])
        config["parameters"] = {"different": 1}
        self.assertEqual(self.http("POST", payload=config, key=key).status_code, 409)
        for params in ({"big": "x"*65536}, {"nested": [[[[[[[[[1]]]]]]]]]}):
            bad = dict(self.template["config"], parameters=params)
            self.assertEqual(self.http("POST", payload=bad, key=uuid4().hex).status_code, 422)
        for action in (lambda: self.repository.status("other-owner", row["run_id"]),
                       lambda: self.repository.request_cancel("other-owner", row["run_id"]),
                       lambda: self.repository.result_pointer("other-owner", row["run_id"])):
            with self.assertRaises(RuntimeFault): action()
        self.assertEqual(self.http("GET", "/%2e%2e%2fresults%2fsummary").status_code, 404)
        self.assertEqual(self.http("POST", "/compare", {"run_ids": [{}, []]}).status_code, 422)
        other_token = "other-private-owner-"+"c"*40
        original_settings = self.app.state.settings
        self.app.state.settings = original_settings.model_copy(update={"api_token": __import__('pydantic').SecretStr(other_token)})
        try:
            for method, path in (("GET", "/"+row["run_id"]), ("POST", "/"+row["run_id"]+"/cancel"),
                                 ("GET", "/"+row["run_id"]+"/results/summary")):
                self.assertEqual(self.http(method, path, token=other_token).status_code, 404)
        finally:
            self.app.state.settings = original_settings
        self.provider.available = False
        self.assertEqual(self.http("POST", "/preflight", self.template["config"]).json()["errors"][0]["code"], "DATA_RESTRICTED")
        self.assertEqual(self.http("POST", payload=self.template["config"], key=uuid4().hex).status_code, 404)

    def test_snapshot_restart_claim_cancel_and_result_atomicity(self):
        row, config = self.submit()
        with Session(self.engine) as s:
            AccountProfileService(s).update(self.account, expected_version=1, fee_schedule=account_payload("9"))
            s.commit()
        claim = self.repository.claim("d13-test", time.time(), 30)
        self.assertEqual(claim.run_id, row["run_id"])
        work = self.repository.work(claim)
        self.assertEqual(work.specification["account_snapshot"]["fee_schedule"]["fee_rules"][0]["minimum"], "1")
        progress = dict(completed_events=2, completed_sessions=1, now_ns="1767225600000000123")
        self.assertTrue(self.repository.progress(claim, progress))
        self.assertEqual(self.http("GET", "/"+claim.run_id).json()["progress_completed"],2)
        with self.assertRaises(RuntimeFault):
            self.repository.progress(claim, dict(progress, now_ns="1767225600000000122"))
        with Session(self.engine) as s:
            with self.assertRaises(AccountProfileReferencedError):
                AccountProfileService(s).permanently_delete(self.account, expected_version=2)
        with self.assertRaises(RuntimeFault):
            self.repository.commit_outcome(claim, dict(status="succeeded", result_id=row["run_id"]), None)
        self.repository.request_cancel(OWNER, row["run_id"])
        self.assertFalse(self.repository.progress(claim, dict(progress, completed_events=3)))
        gw, ctx = self.provider.gateway(work, claim.run_id, Event())
        writer = _native.TrustedResultWriter(str(self.settings.s3_result_root), claim.run_id, 1024*1024)
        # Cancel must reject success before retaining any candidate/pointer.
        self.assertFalse(gw.finalize(ctx, lambda c: self.repository.commit_outcome(claim,
            dict(status="succeeded", result_id=claim.run_id), "candidate", c, lambda: "candidate")))
        self.assertTrue(self.repository.commit_outcome(claim, dict(status="cancelled", partial=True), None))
        self.assertEqual(self.repository.request_cancel(OWNER, claim.run_id)["status"], "cancelled")
        self.assertEqual(self.repository.status(OWNER,claim.run_id)['progress'],progress)
        self.assertFalse(self.repository.progress(claim, dict(progress, completed_events=4)))
        with self.engine.begin() as c:
            with self.assertRaises(Exception):
                c.execute(update(Run).where(Run.id==identity(claim.run_id)).values(progress=None))
        self.assertIsNone(self.repository.result_pointer(OWNER, claim.run_id)[1])
        gw.close(ctx)
        other, _ = self.submit()
        stale = self.repository.claim("crashed", time.time(), 2)
        self.assertTrue(self.repository.progress(stale,progress))
        with self.engine.begin() as c:
            c.execute(update(Run).where(Run.id == identity(stale.run_id)).values(lease_until=time.time()-1))
        restarted = DatabaseRunRepository(self.engine, result_root=self.settings.s3_result_root)
        self.assertTrue(restarted.cancelled(stale))
        self.assertFalse(restarted.commit_outcome(stale, dict(status="failed", partial=True,
                         error=RuntimeFault("STRATEGY_ERROR", "synthetic failure").diagnostic()), None))
        self.assertTrue(restarted.recover_stopped(stale, time.time()))
        current = restarted.claim("replacement", time.time(), 30)
        self.assertNotEqual(current.token, stale.token)
        self.assertGreater(current.fence, stale.fence)
        self.assertIsNone(restarted.status(OWNER,current.run_id)['progress'])
        self.assertFalse(restarted.progress(stale,progress))
        self.assertTrue(restarted.progress(current,dict(progress,completed_events=1)))
        self.assertFalse(restarted.commit_outcome(stale, dict(status="cancelled", partial=True), None))

    def test_database_batch_admission_retries_and_parallel_claim_budget(self):
        configs = [dict(self.template["config"], parameters={"index": i}) for i in range(16)]
        key = uuid4().hex
        response = self.http("POST", "/batches", {"configs": configs}, key=key)
        self.assertEqual(response.status_code, 202, response.text)
        bid = response.json()["batch_id"]
        self.assertEqual(self.http("POST", "/batches", {"configs": configs}, key=key).json()["batch_id"], bid)
        self.runs.extend(r["run_id"] for r in response.json()["items"])
        wrong = deepcopy(configs); wrong[0]["parameters"]["index"] = 99
        self.assertEqual(self.http("POST", "/batches", {"configs": wrong}, key=key).status_code, 409)
        mixed = deepcopy(configs); mixed[0]["initial_cash"] = "1"
        self.assertEqual(self.http("POST", "/batches", {"configs": mixed}, key=uuid4().hex).status_code, 422)
        page = self.http("GET", "/batches/"+bid+"?limit=3").json()
        self.assertEqual(len(page["items"]), 3)
        self.assertTrue(page["next_cursor"])
        self.assertEqual(self.http("GET", "/batches/"+bid+"?limit=3&cursor="+page["next_cursor"][:-1]+"x").status_code, 422)
        # Independent repository instances and threads contend on the same PG
        # group lock, so only two workers may reserve this queue at once.
        claims = []
        def claim():
            claims.append(DatabaseRunRepository(self.engine).claim("parallel-"+uuid4().hex, time.time(), 30))
        threads = [Thread(target=claim) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join(5)
        valid = [c for c in claims if c is not None]
        self.assertEqual(len(valid), 2)
        self.assertEqual(len({c.run_id for c in valid}), 2)
        for c in valid:
            self.assertTrue(self.repository.commit_outcome(c, dict(status="failed", partial=True,
                        error=RuntimeFault("STRATEGY_ERROR", "synthetic failure").diagnostic()), None))
        retry_key = uuid4().hex
        retries = self.http("POST", "/batches/"+bid+"/retry-failed", key=retry_key)
        self.assertEqual(retries.status_code, 202, retries.text)
        self.assertEqual(len(retries.json()["run_ids"]), 2)
        self.runs.extend(retries.json()["run_ids"])
        self.assertEqual(set(self.http("POST", "/batches/"+bid+"/retry-failed", key=retry_key).json()["run_ids"]),
                         set(retries.json()["run_ids"]))
        self.assertTrue(all(self.repository.status(OWNER, c.run_id)["status"] == "failed" for c in valid))
        with self.assertRaises(RuntimeFault): self.repository.cancel_batch("other-owner", bid)
        self.assertEqual(self.http("POST", "/batches/"+bid+"/cancel").status_code, 200)
        with self.engine.begin() as connection:
            with self.assertRaises(Exception):
                connection.execute(update(Run).where(Run.id == identity(valid[0].run_id)).values(status="succeeded"))

    def test_legacy_upgrade_values_same_site_and_startup_without_computation(self):
        from app.backtest_service.accounts.fee_catalog import FeeCatalogRepository
        with Session(self.engine) as session:
            fees = FeeCatalogRepository(session)
            self.assertEqual(fees.require(self.old_fee_key, 1), self.old_fee_snapshot)
            published = fees.publish(key="d13-new-"+uuid4().hex, version=1, name_zh="新费用目录", name_en="New fee",
                                     fee_rules=account_payload()["fee_rules"], metadata={"currency": "CNY"})
            self.assertEqual(len(published.snapshot_hash), 64)
            session.commit()
        response = self.http("GET", "/"+str(self.old_run))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["initial_cash"], "10000.000000000000000123")
        equity = self.http("GET", "/"+str(self.old_run)+"/results/equity").json()["items"][0]
        self.assertEqual(equity["cash"], "123456789.123456789012345678")
        self.assertEqual(equity["cumulative_fees"], "1.000000000000000001")
        self.assertEqual(equity["run_id"], str(self.old_run))
        saved=requests.get(self.url+'/api/admin/backtest-comparisons/'+str(self.old_comparison),
                           headers={'Authorization':'Bearer '+TOKEN},timeout=10)
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(saved.json()['name'],'旧比较：中文/原顺序')
        self.assertEqual(saved.json()['run_ids'],[str(self.old_failure),str(self.old_run)])
        self.assertEqual(saved.json()['baseline_run_id'],str(self.old_run))
        with self.engine.connect() as c:
            error=c.execute(text('SELECT error_message,failure_evidence,account_profile_id,strategy_revision_id FROM backtest_runs WHERE id=:id'),{'id':self.old_failure}).one()
        self.assertEqual(error.error_message,'旧失败原文：余额不足；不重新执行')
        self.assertEqual(error.failure_evidence,{'line':17,'time_ns':'1767225600000000456'})
        self.assertEqual((error.account_profile_id,error.strategy_revision_id),(str(self.old_account),str(self.old_revision)))
        new, _ = self.submit()
        page = self.http("GET").json()
        ids = {r["run_id"] for r in page["items"]}
        while page["next_cursor"]:
            response = self.http("GET", "?limit=100&cursor="+page["next_cursor"])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertLess(len(response.content), 1024*1024)
            page = response.json()
            ids.update(r["run_id"] for r in page["items"])
        self.assertIn(str(self.old_run), ids); self.assertIn(new["run_id"], ids)
        script = '''import sys
class Block:
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname==p or fullname.startswith(p+'.') for p in ('app.backtesting','app.strategy_protocol')):
            raise ImportError("retired compute imported: "+fullname)
sys.meta_path.insert(0, Block())
import app.main, app.models, app.runner.__main__
app.main.app.openapi()
assert not any(n.startswith(('app.backtesting','app.strategy_protocol')) for n in sys.modules)
'''
        checked = subprocess.run([sys.executable, "-c", script], env=self.environment, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_batch_pages_bound_bytes_and_keep_every_run(self):
        fees = account_payload()
        fees["metadata"]["description"] = "x"*48000
        with Session(self.engine) as session:
            AccountProfileService(session).update(self.account, expected_version=1, fee_schedule=fees)
            session.commit()
        configs = [dict(self.template["config"], parameters={"index": i}) for i in range(24)]
        response = self.http("POST", "/batches", {"configs": configs}, key=uuid4().hex)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertLess(len(response.content), 1024*1024)
        page = response.json()
        bid = page["batch_id"]
        self.assertLess(len(page["items"]), 24)
        ids = [r["run_id"] for r in page["items"]]
        while page["next_cursor"]:
            response = self.http("GET", "/batches/"+bid+"?limit=100&cursor="+page["next_cursor"])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertLess(len(response.content), 1024*1024)
            page = response.json()
            ids.extend(r["run_id"] for r in page["items"])
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(len(ids), 24)
        self.runs.extend(ids)
        self.assertEqual(self.http("POST", "/batches/"+bid+"/cancel").status_code, 200)

    def test_real_runner_database_http_sdk_and_d11_result(self):
        if os.environ.get("QF_D13_REAL_WORKER_REQUIRED") != "1":
            self.skipTest("actual runner-container route required in S3 worker isolation CI")
        from app.backtest_service.process import DockerDaemon
        work_root = self.base/"work"; work_root.mkdir()
        settings = self.settings.model_copy(update=dict(s3_work_root=work_root,
                s3_daemon_work_root=Path(os.environ["QF_TEST_DAEMON_ROOT"])/work_root.relative_to(Path("/qf-work")),
                s3_worker_image=os.environ["QF_TEST_WORKER_IMAGE"]))
        # This required container test now runs the production FormalProvider
        # itself over the real E70 physical node layout. Only the explicitly
        # named execution facts are synthetic; no native/source-table fallback.
        from s3_formal_fixtures import nodes, MODEL
        from app.backtest_service.formal_runtime import AcceptedRunPlan
        from app.backtest_service.service import FormalProvider
        market,self.template = nodes(self.store,securities=('A.SH',))
        checked_connections = []
        def resolve(work):
            packet = deepcopy(self.template)
            for fact in packet['facts']:
                for side in ('buy','sell'):
                    commission = commission_from_account_fee_schedule(
                        work.specification['account_snapshot']['fee_schedule'],side=side,
                        included_components=(),cost_overrides=work.config.get('cost_overrides'))
                    fact[side+'_fees'].update({key:commission[key] for key in
                        ('commission_rate','minimum_commission','settlement_scale','rounding')})
            def check_facts(accepted, *, connection):
                if accepted is not work or tuple(accepted.config['universe']) != ('A.SH',):
                    raise RuntimeFault('DATA_RESTRICTED','private dated facts outside accepted scope')
                if connection is not None:
                    # A real final transaction, with no remote I/O or snapshot.
                    self.assertTrue(connection.in_transaction())
                    checked_connections.append(id(connection))
            return AcceptedRunPlan(market,packet,check_facts=check_facts)
        trusted = FormalProvider(settings,engine=self.engine,store=self.store,plan_resolver=resolve)
        repository, process, services = create_services(settings=settings, engine=self.engine, provider=trusted, daemon=DockerDaemon())
        trusted.process = process
        self.provider = FormalProvider(settings,engine=self.engine,store=self.store,plan_resolver=resolve)
        self.service = RunService(self.repository,self.provider)
        self.app.state.s3_provider = self.provider
        self.assertIsNone(self.provider.process,'web must not own Docker access')
        services.readiness.refresh()
        supervisor = RunnerSupervisor(repository, process, services, lease_seconds=10)
        source = """import time
from quantfoundry.api import order, LimitOrder, get_price
def before_trading_start(c):
    if not getattr(c.state, 'bought', False):
        c.state.bought = True
        assert order('A.SH', 100, style=LimitOrder('10')).accepted
def handle_data(c, data):
    frame=get_price(['A.SH'],count=2,fields=['close'])
    assert list(frame.columns)==['security','time_ns','close']
    assert all(type(value).__name__=='Decimal' for value in frame['close'])
    time.sleep(.65)
"""
        self.account, self.revision = catalogue(self.engine, source)
        self.template["config"].update(account_id=str(self.account), strategy_revision_id=str(self.revision))
        def legacy_execution_rows():
            from app.backtest_service.history import result_records
            tables = {'backtest_runs', *(getattr(result_records, name).__tablename__ for name in result_records.__all__)}
            with self.engine.connect() as c:
                return {table: sorted(json.dumps(dict(row),sort_keys=True,default=str,ensure_ascii=False)
                    for row in c.execute(text('SELECT * FROM "'+table+'"')).mappings()) for table in sorted(tables)}
        protected = legacy_execution_rows()
        row, config = self.submit()
        with Session(self.engine) as session:
            AccountProfileService(session).update(self.account, expected_version=1, fee_schedule=account_payload("9"))
            session.commit()
        try:
            live_progress=[]
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                supervisor.step()
                status = self.http("GET", "/"+row["run_id"]).json()
                if status['status']=='running' and status['progress_completed']>0:
                    live_progress.append(status['progress_completed'])
                if status["status"] in ("succeeded", "failed", "cancelled"):
                    break
                Event().wait(.05)
            self.assertEqual(status["status"], "succeeded", status)
            self.assertTrue(live_progress,'real HTTP status stayed at zero throughout execution')
            self.assertEqual(status['progress_completed'],3)
            self.assertEqual(status['progress']['completed_sessions'],len(self.template['calendar']))
            result = self.http("GET", "/"+row["run_id"]+"/results/summary")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["performance"]["initial_equity"], "10000")
            self.assertEqual(result.json()["performance"]["trades"]["fill_fees"], "1.00")
            trades = self.http("GET", "/"+row["run_id"]+"/results/trades").json()["items"]
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["fee"], "1.00")
            self.assertEqual(self.http("POST", "/"+row["run_id"]+"/cancel").json()["status"], "succeeded")
            comparison = self.http("POST", "/compare", {"run_ids": [str(self.old_run), row["run_id"]]})
            self.assertEqual(comparison.status_code, 200, comparison.text)
            self.assertIsNone(comparison.json()["ranking"])
            self.assertEqual({item["engine_kind"] for item in comparison.json()["items"]}, {"legacy_python", "rust"})
            page = self.http("GET", "/"+row["run_id"]+"/results/equity?limit=1").json()
            self.assertEqual(len(page["items"]), 1)
            if page["next_cursor"]:
                next_page = self.http("GET", "/"+row["run_id"]+"/results/equity?limit=1&cursor="+page["next_cursor"])
                self.assertEqual(next_page.status_code, 200, next_page.text)
                self.assertNotEqual(next_page.json()["items"], page["items"])
            self.assertFalse(list(work_root.iterdir()))
            self.assertTrue(checked_connections,'accepted facts were not checked inside final transaction')
            self.assertEqual(legacy_execution_rows(),protected, 'new Rust run wrote old roots/snapshots/events/results')
            (self.base/'retired-execution-writes.json').write_text(json.dumps(dict(production_executed=False,
                real_runner_container=True,formal_provider=True,formal_node_entry='E70',
                execution_fact_model=MODEL,run_id=row['run_id'],before=protected,after=legacy_execution_rows()),ensure_ascii=False,indent=2)+'\n')
        finally:
            try:
                supervisor.stop()
            finally:
                services.readiness.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
