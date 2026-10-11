"""D14 installed clients -> real HTTP/PG/CurrentStore -> D12/D10/D11.

Host callback tests help diagnose examples, but the full HTTP worker test is
mandatory in the actual runner-container CI. Only synthetic inputs are seeded.
"""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4

import pyarrow as pa
import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest_service.data_gateway import AuthorizedRun, MARKET_SCHEMA, MarketBinding, serve
from app.backtest_service.research_gateway import ResearchBinding, ResearchDataGateway, SCHEMA
from app.backtest_service.runtime_contracts import Claim, RuntimeFault
from app.backtest_service.runtime_services import create_services
from app.backtest_service.service import FormalProvider, RunService
from app.backtest_service.supervisor import Binding, RunnerSupervisor
from app.core.auth import AuthenticatedPrincipal
from app.data_store.catalog import SourceUpdate
from app.data_store.schema import DatasetSpec, fingerprint
from app.strategies.models import Strategy, StrategyDraft, StrategyRevision
from app.strategies.service import StrategyStorageService
from quantfoundry import BacktestClient, BacktestError, RunConfig
from quantfoundry._transport import Channel
from quantfoundry.data import _MarketRoute
from quantfoundry._worker import _run_module

from s3_runservice_suite import D13Tests, OWNER, TOKEN, catalogue
from s3_cli_fixtures import example_spec, fundamental_rows
from support_sdk import batch

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT/"examples/strategies").exists():
    ROOT = Path("/app")
EXAMPLES = ROOT/"examples/strategies"
ENTRIES = json.loads((EXAMPLES/"run_configs.json").read_text())


class ExampleProvider:
    """Test-only seed through CurrentStore; no source tables or alternate engine."""
    def __init__(self, engine, store, entry):
        self.engine, self.store, self.entry = engine, store, deepcopy(entry)
        self.template = example_spec(entry)
        self.name, self.available, self.process = "d14_"+uuid4().hex, True, None
        keys = ("time_ns", "security", "source_session", "channel", "sequence", "stable_input_sequence")
        fields = [pa.field(f.name, f.type, nullable=f.name not in keys,
            metadata={b"max_utf8_bytes": b"128"} if pa.types.is_string(f.type) else
                     {b"unit": b"epoch_ns"} if f.name.endswith("_ns") else None) for f in MARKET_SCHEMA]
        dataset = DatasetSpec(self.name, pa.schema(fields, metadata=MARKET_SCHEMA.metadata),
            keys, "synthetic-d14",
            {"price_basis": "raw", "price_currency": "CNY", "quantity_unit": "shares"})
        self.put(dataset, pa.ipc.open_stream(batch(self.template["source"]["events"])).read_all())
        frequency = entry["config"]["frequency"]
        self.market = MarketBinding("market", dataset, frequency, {f: f for f in MARKET_SCHEMA.names}, {},
                         lambda security, start, end: ((start,), (end+1,)), "time_ns")
        self.bindings = [self.market]
        self.research_routes = {}
        if entry["example"] == "fundamental_selection.py":
            research_fields = [pa.field(f.name, f.type, nullable=f.name in ("value", "report_period", "effective_until_ns"),
                metadata={b"max_utf8_bytes": b"128"} if pa.types.is_string(f.type) else {b"unit": b"epoch_ns"}) for f in SCHEMA]
            research = DatasetSpec(self.name+"_fundamentals", pa.schema(research_fields, metadata=SCHEMA.metadata),
                ("security", "record_id", "field"), "synthetic-d14-public-times", {"knowledge_basis": "synthetic_explicit_public_time"})
            self.put(research, pa.Table.from_pylist(fundamental_rows(self.template), schema=research.schema))
            self.bindings.append(ResearchBinding("fundamentals", research, "fundamentals", {f: f for f in SCHEMA.names}, {},
                lambda security, *_: ((security,), (security+"\0",)), "time_ns", {"net_profit": ("decimal", "CNY")},
                ("verified_public_time", "stable_identity", "verified_effective_dates"),
                limitations=("synthetic_d14_not_production_data",)))
            self.research_routes = {"fundamentals": "fundamentals"}

    def put(self, dataset, table):
        self.store.register(dataset)
        rows = sorted(table.to_pylist(), key=lambda r: dataset.key_bytes(tuple(r[k] for k in dataset.key)))
        table = pa.Table.from_pylist(rows, schema=dataset.schema)
        securities = sorted(set(table["security"].to_pylist()))
        if dataset.key[0] == "time_ns":
            lower, upper = (min(r["time_ns"] for r in rows),), (max(r["time_ns"] for r in rows)+1,)
        else:
            lower, upper = (securities[0],), (securities[-1]+"\0",)
        self.store.upsert(dataset, "default", table.to_batches(),
            SourceUpdate("d14-test", fingerprint(dataset.name), fingerprint("synthetic"), 0),
            lower=lower, upper=upper, source_check=lambda _: True)

    def capabilities(self):
        return [dict(binding=self.name, available=self.available, synthetic=True, requires=self.entry["requires"])]

    def request(self, work):
        fields = ["price", "quantity"] if work.config["frequency"] == "tick" else ["open", "high", "low", "close", "quantity"]
        return dict(securities=work.config["universe"], fields=fields, frequency=work.config["frequency"],
            start_ns=self.template["calendar"][0]["before_open_ns"], end_ns=self.template["calendar"][-1]["after_close_ns"],
            count_per_security=None, adjustment="none")

    def gateway(self, work, run_id, stopped):
        grant = AuthorizedRun(run_id, work.owner, tuple(work.config["universe"]),
            int(self.template["calendar"][0]["before_open_ns"]), int(self.template["calendar"][-1]["after_close_ns"]))
        gateway = ResearchDataGateway(self.store, AuthenticatedPrincipal(work.owner), grant, tuple(self.bindings), batch_rows=4, cancelled=stopped.is_set)
        return gateway, gateway.open(dict(run_id=run_id, universe=work.config["universe"]), dependencies=("market",))

    def check(self, work):
        if not self.available:
            raise RuntimeFault("CAPABILITY_UNAVAILABLE", "合成测试所需能力被显式移除；禁止补零或访问供应商")
        if work.config["frequency"] != self.entry["config"]["frequency"] or set(work.config["universe"]) != set(self.entry["config"]["universe"]):
            raise RuntimeFault("CAPABILITY_UNAVAILABLE", "此合成数据没有请求的频率或授权集合")
        if self.process:
            self.process.daemon.preflight(self.process.image)
        gateway, context = self.gateway(work, "preflight-"+uuid4().hex, Event())
        try:
            rows = sum(p.table.num_rows for p in gateway.read("market", self.request(work), context, page_rows=4))
            if not rows:
                raise RuntimeFault("CAPABILITY_UNAVAILABLE", "缺少必需数据")
            if self.research_routes:
                gateway.declare("fundamentals", context)
                self.bindings[-1].validate_capability()
            gateway.check(context)
        finally:
            gateway.close(context)
        return self.template["calendar_reference"]

    def binding(self, work, claim, stopped):
        from app.backtest_service.account_config import commission_from_account_fee_schedule
        gateway, context = self.gateway(work, claim.run_id, stopped)
        source = deepcopy(self.template)
        source.update(run_id=claim.run_id, config=work.config)
        source["source"] = dict(kind="ipc", binding="market", socket_path="/run/qf/ipc/data.sock", request=self.request(work))
        for facts in source["facts"]:
            for side in ("buy", "sell"):
                commission = commission_from_account_fee_schedule(work.specification["account_snapshot"]["fee_schedule"],
                    side=side, included_components=(), cost_overrides=work.config.get("cost_overrides"))
                facts[side+"_fees"].update({k: commission[k] for k in ("commission_rate", "minimum_commission", "settlement_scale", "rounding")})
        return Binding(source, {work.config["frequency"]: dict(binding="market", fields=self.request(work)["fields"])},
                       self.research_routes, gateway, context)


class D14Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        previous = os.environ.get("QF_D13_TEST_ROOT")
        if previous:
            os.environ["QF_D13_TEST_ROOT"] = str(Path(previous).with_name("d14-service"))
        try:
            D13Tests.setUpClass.__func__(cls)
        finally:
            if previous:
                os.environ["QF_D13_TEST_ROOT"] = previous

    tearDownClass = classmethod(D13Tests.tearDownClass.__func__)
    migrate = classmethod(D13Tests.migrate.__func__)
    setUp = D13Tests.setUp
    tearDown = D13Tests.tearDown

    def cli(self, *args, expected=0):
        # Run the actual installed qf entry point from a directory without source
        # imports. Token is environment-only and never part of argv/output.
        # An optional separate wheel-only consumer lets the documented install
        # be checked without giving that client the server/test dependencies.
        client_python = os.environ.get("QF_D14_CLIENT_PYTHON", sys.executable)
        executable = shutil.which("qf", path=str(Path(client_python).parent))
        self.assertIsNotNone(executable, "installed wheel must provide qf")
        env = dict(os.environ, QF_BASE_URL=self.url, QF_API_TOKEN=TOKEN)
        env.pop("PYTHONPATH", None)
        process = subprocess.run([executable, "backtest", *args], env=env, cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, expected, process.stdout+process.stderr)
        self.assertNotIn(TOKEN, process.stdout+process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        return json.loads(process.stdout if process.stdout else process.stderr)

    def seed(self, entry):
        entry = deepcopy(entry)
        source = (EXAMPLES/entry["example"]).read_text()
        account, revision = catalogue(self.engine, source)
        entry["config"].update(account_id=str(account), strategy_revision_id=str(revision))
        # Explicit published schema is consumed identically by CLI/Python/HTTP.
        with Session(self.engine) as session:
            r = session.get(StrategyRevision, revision)
            # Revisions are immutable: publish a new schema revision from a new
            # draft instead of changing the original revision or historical text.
            storage = StrategyStorageService(session)
            storage.save_draft(r.strategy_id, expected_version=1, parameter_schema=dict(type="object", properties={
                "window": {"type": "integer", "minimum": 1}, "weight": {"type": "string"},
                "securities": {"type": "array", "items": {"type": "string"}}, "security": {"type": "string"},
                "decision_day": {"type": "integer"}, "top_n": {"type": "integer", "minimum": 1},
                "threshold": {"type": "string"}, "fundamental_field": {"type": "string"}, "end_date": {"type": "string"}}, additionalProperties=False),
                default_parameters=entry["config"]["parameters"])
            published = storage.publish_revision(r.strategy_id, expected_draft_version=2)
            entry["config"]["strategy_revision_id"] = str(published.id)
            session.commit()
        self.provider = ExampleProvider(self.engine, self.store, entry)
        self.app.state.s3_provider = self.provider
        self.service = RunService(self.repository, self.provider)
        self.client = BacktestClient(base_url=self.url, token=TOKEN)
        return entry, RunConfig.from_dict(entry["config"])

    def config_file(self, config, directory, name="config.json"):
        path = Path(directory)/name
        path.write_text(json.dumps(config.to_dict()))
        return str(path)

    def test_real_http_cli_python_idempotency_errors_cancel_batch_and_history_protection(self):
        entry, config = self.seed(ENTRIES[0])
        with tempfile.TemporaryDirectory() as directory:
            path = self.config_file(config, directory)
            self.assertEqual(self.cli("capabilities")["api_schema"], "qf.backtest.v2")
            self.assertEqual(self.cli("preflight", path), self.client.preflight(config))
            row = self.cli("submit", path, "--idempotency-key", "cli-python-"+uuid4().hex)
            self.runs.append(row["run_id"])
            key = "same-config-"+uuid4().hex
            direct = self.client.submit(config, idempotency_key=key)
            self.runs.append(direct["run_id"])
            replay = self.cli("run", path, "--idempotency-key", key)
            self.assertEqual(replay["run_id"], direct["run_id"])
            self.assertEqual(replay["normalized_config"], direct["normalized_config"])
            different = RunConfig.from_dict(dict(config.to_dict(), initial_cash="9000"))
            wrong = self.config_file(different, directory, "wrong.json")
            self.assertEqual(self.cli("submit", wrong, "--idempotency-key", key, expected=2)["code"], "IDEMPOTENCY_CONFLICT")
            self.assertEqual(self.cli("status", row["run_id"])["status"], "queued")
            self.assertEqual(self.cli("result", row["run_id"], expected=2)["code"], "RESULT_NOT_READY")
            cancelled = self.cli("cancel", row["run_id"])
            self.assertTrue(cancelled["cancel_requested"])
            self.client.cancel(direct["run_id"])
            self.provider.available = False
            self.assertEqual(self.cli("preflight", path, expected=2)["errors"][0]["code"], "CAPABILITY_UNAVAILABLE")
            self.provider.available = True
            wrong_config = config.to_dict(); wrong_config["parameters"]["window"] = "bad"
            invalid = Path(directory)/"invalid.json"; invalid.write_text(json.dumps(wrong_config))
            self.assertEqual(self.cli("preflight", str(invalid), expected=2)["errors"][0]["code"], "STRATEGY_ERROR")
            payload = Path(directory)/"batch.json"; payload.write_text(json.dumps({"configs": [config.to_dict(), config.to_dict()]}))
            result = self.cli("batch", str(payload), "--idempotency-key", "batch-"+uuid4().hex)
            self.runs.extend(i["run_id"] for i in result["items"])
            first = self.cli("batch-status", result["batch_id"], "--limit", "1")
            next_page = self.cli("batch-status", result["batch_id"], "--limit", "1", "--cursor", first["next_cursor"])
            self.assertNotEqual(first["items"][0]["run_id"], next_page["items"][0]["run_id"])
            # Explicit isolated control-state faults verify the retry CLI. Full
            # worker failure and cancellation run through the D12 suite/next test.
            for item in result["items"]:
                claim=self.repository.claim("d14-control-test",time.time(),30)
                self.assertIn(claim.run_id,[i["run_id"] for i in result["items"]])
                self.assertTrue(self.repository.commit_outcome(claim,dict(status="failed",partial=True,
                    error=RuntimeFault("STRATEGY_ERROR","显式合成控制状态故障").diagnostic()),None))
            self.cli("cancel-batch", result["batch_id"])
            retried = self.cli("retry-failed", result["batch_id"], "--idempotency-key", "retry-"+uuid4().hex)
            self.runs.extend(retried["run_ids"])
            self.assertEqual(len(retried["run_ids"]), 2)
            self.assertTrue(self.cli("result", result["items"][0]["run_id"])["partial"])
            self.cli("compare", *(i["run_id"] for i in result["items"]))
            self.app.state.s3_provider = FormalProvider(self.settings)
            self.assertEqual(self.cli("preflight", path, expected=2)["errors"][0]["code"], "CAPABILITY_UNAVAILABLE")

    def assert_rotation(self, trades, config):
        old, new = self.provider.entry["config"]["universe"]
        old_buys = [t for t in trades if t["security"] == old and t["side"] == "buy"]
        old_sales = [t for t in trades if t["security"] == old and t["side"] == "sell"]
        new_buys = [t for t in trades if t["security"] == new and t["side"] == "buy"]
        self.assertTrue(old_buys and old_sales and new_buys, trades)
        self.assertLess(int(old_buys[0]["execution_time_ns"]), int(old_sales[-1]["execution_time_ns"]))
        self.assertLess(int(old_sales[-1]["execution_time_ns"]), int(new_buys[0]["execution_time_ns"]))
        self.assertEqual(sum(t["quantity"] for t in old_buys), sum(t["quantity"] for t in old_sales))

    def test_documented_migration_creates_new_strategy_revision_preserves_old_values(self):
        entry, config = self.seed(ENTRIES[0])
        with Session(self.engine) as session:
            revision = session.get(StrategyRevision, self.old_revision)
            strategy_id = revision.strategy_id
            draft = session.scalar(select(StrategyDraft).where(StrategyDraft.strategy_id == strategy_id))
            original = (str(revision.id), revision.source_code, revision.source_hash, draft.source_code, draft.version)
        # Exactly the existing create + publish routes documented for a new copy;
        # no update/PATCH of the original draft or immutable revision.
        response = requests.post(self.url+"/api/admin/strategies", headers={"Authorization":"Bearer "+TOKEN}, json=dict(
            name="d14-migrated-copy-"+uuid4().hex, source_code=(EXAMPLES/"etf_rotation.py").read_text(),
            parameter_schema={"type":"object"}, default_parameters=entry["config"]["parameters"]), timeout=10)
        self.assertEqual(response.status_code,201,response.text)
        created=response.json()
        response=requests.post(self.url+"/api/admin/strategies/"+created["id"]+"/publish",
            headers={"Authorization":"Bearer "+TOKEN},json=dict(draft_version=created["draft"]["version"],alias="S3 migrated copy"),timeout=10)
        self.assertEqual(response.status_code,201,response.text)
        self.assertNotEqual(response.json()["id"],str(self.old_revision))
        migrated=RunConfig.from_dict(dict(config.to_dict(),strategy_revision_id=response.json()["id"]))
        self.assertFalse(self.client.preflight(migrated)["errors"])
        with Session(self.engine) as session:
            revision=session.get(StrategyRevision,self.old_revision)
            draft=session.scalar(select(StrategyDraft).where(StrategyDraft.strategy_id==strategy_id))
            self.assertEqual((str(revision.id),revision.source_code,revision.source_hash,draft.source_code,draft.version),original)
            self.assertEqual(session.get(Strategy,strategy_id).current_revision_id,self.old_revision)
        old=self.client.result(str(self.old_run))
        self.assertEqual(old["summary"],{"value":"123456789.123456789012345678","time_ns":"1767225600000000123"})
        old_curve=self.client.result(str(self.old_run),kind="equity",limit=1)["items"]
        self.assertEqual(old_curve[0]["equity"],"123456789.123456789012345678")

    def test_chronological_pages_single_identity_and_unordered_replay_refusal(self):
        entry,config=self.seed(ENTRIES[0])
        work=self.service.accepted_work(OWNER,config.to_dict())
        gateway,context=self.provider.gateway(work,"ordering-"+uuid4().hex,Event())
        request=self.provider.request(work)
        try:
            pages=list(gateway.read("market",request,context,page_rows=1))
            keys=[(r["time_ns"],r["security"],r["source_session"],r["channel"],r["sequence"],r["stable_input_sequence"])
                  for page in pages for r in page.table.to_pylist()]
            self.assertEqual(keys,sorted(keys));self.assertEqual(len(keys),24)
            single=dict(request,securities=[config.to_dict()["universe"][0]])
            single_pages=list(gateway.read("market",single,context,page_rows=1))
            self.assertTrue(all(set(p.table["security"].to_pylist())==set(single["securities"]) for p in single_pages))
        finally:gateway.close(context)
        # This physical layout is perfectly valid CurrentStore research data,
        # but a single multi-security replay cannot claim chronological pages.
        market=self.provider.market
        dataset=DatasetSpec(self.provider.name+"_security_first",market.spec.schema,
            ("security","time_ns","sequence"),"synthetic-ordering-counterexample",market.spec.semantics)
        self.provider.put(dataset,pa.ipc.open_stream(batch(self.provider.template["source"]["events"])).read_all())
        self.provider.bindings=[replace(market,spec=dataset,bounds=lambda s,a,b:((s,a),(s,b+1)))]
        gateway,context=self.provider.gateway(work,"unordered-"+uuid4().hex,Event())
        try:
            with self.assertRaises(ValueError) as error:list(gateway.read("market",request,context,page_rows=1))
            self.assertEqual(error.exception.code,"CAPABILITY_UNAVAILABLE")
            self.assertIn("跨页时间顺序",error.exception.message)
            window=dict(request,start_ns=None,count_per_security=3)
            self.assertEqual(sum(p.table.num_rows for p in gateway.read("market",window,context,page_rows=2)),6)
        finally:gateway.close(context)

    def wait(self, supervisor, ids):
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            supervisor.step()
            states=[self.client.status(run_id) for run_id in ids]
            if all(s["status"] in ("succeeded","failed","cancelled") for s in states):return states
            Event().wait(.05)
        self.fail("真实 worker 未在预算内终结")

    def documented_commands(self, supervisor, config, path, row, other, key, directory):
        variables=dict(CONFIG=path,KEY=key,RUN_ID=row["run_id"],OTHER_RUN_ID=other["run_id"],
                       BATCH_KEY="docs-batch-"+uuid4().hex,RETRY_KEY="docs-retry-"+uuid4().hex,
                       WORKFLOW_KEY="docs-workflow-"+uuid4().hex)
        payload=Path(directory)/"docs-batch.json"
        second=config.to_dict();second["parameters"]["weight"]="0.5"
        payload.write_text(json.dumps({"configs":[config.to_dict(),second]}))
        variables["BATCH_CONFIG"]=str(payload)
        tested=[];batch_ids=[]
        guide=(ROOT/"docs/backtesting/README.md").read_text()
        for line in guide.splitlines():
            if not line.startswith("qf backtest "):continue
            command=shlex.split(line)[2:]
            command=[variables[value[1:]] if value.startswith("$") else value for value in command]
            value=self.cli(*command)
            tested.append(line)
            if command[0]=="batch":
                variables["BATCH_ID"]=value["batch_id"]
                batch_ids=[i["run_id"] for i in value["items"]];self.runs.extend(batch_ids)
            if command[0]=="batch-status" and value.get("next_cursor"):
                variables["BATCH_CURSOR"]=value["next_cursor"]
            if command[0]=="result" and value.get("next_cursor"):
                variables["CURSOR"]=value["next_cursor"]
            if command[0]=="cancel-batch":
                self.assertTrue(all(s["status"]=="cancelled" for s in self.wait(supervisor,batch_ids)))
            if command[0]=="retry-failed":
                self.runs.extend(value["run_ids"])
                # D13 retries failed only. This batch was cancelled before it
                # started: cancellation is intentional, so there are no failures.
                self.assertEqual(value["run_ids"],[])
                self.assertTrue(all(s["status"]=="succeeded" for s in self.wait(supervisor,value["run_ids"])))
        self.assertEqual(len(tested),15)
        failures=[];stop=Event()
        def run():
            try:
                while not stop.is_set():supervisor.step();stop.wait(.02)
            except Exception as error:failures.append(error)
        thread=Thread(target=run,daemon=True);thread.start()
        try:
            process=subprocess.run([sys.executable,"-I",str(ROOT/"examples/http_workflow.py"),path,variables["WORKFLOW_KEY"]],
                env=dict(os.environ,QF_BASE_URL=self.url,QF_API_TOKEN=TOKEN),cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=150)
            self.assertEqual(process.returncode,0,process.stdout+process.stderr)
            self.assertNotIn(TOKEN,process.stdout+process.stderr)
            self.assertEqual(json.loads(process.stdout)["outcome"]["status"],"succeeded")
        finally:
            stop.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertFalse(failures,failures)
        return tested+["python -I examples/http_workflow.py config.json key"]

    def test_host_installed_examples_actual_currentstore_queries_and_rust_callbacks(self):
        # This deliberately does not claim Docker isolation or a CLI executor.
        # The identical strategies then run through real workers in the next test.
        for original in ENTRIES:
            variants = [original]
            if original["example"] == "intraday.py":
                minute = deepcopy(original); minute["config"]["frequency"] = "1m"; variants.append(minute)
            for entry in variants:
                with self.subTest(example=entry["example"], frequency=entry["config"]["frequency"]):
                    entry, config = self.seed(entry)
                    work = self.service.accepted_work(OWNER, config.to_dict())
                    binding = self.provider.binding(work, Claim(str(uuid4()), "a"*32, 1), Event())
                    with tempfile.TemporaryDirectory(prefix="qf-d14-callback-") as directory:
                        listener = socket.socket(socket.AF_UNIX); endpoint=Path(directory)/"data.sock"; listener.bind(str(endpoint)); listener.listen(1)
                        failures=[]
                        def data():
                            channel = None
                            try:
                                peer, _ = listener.accept(); channel=Channel(peer, timeout=30)
                                serve(channel, binding.gateway, binding.context)
                            except Exception as error:
                                if getattr(error,"code",None) != "CANCELLED": failures.append(error)
                            finally:
                                if channel: channel.close()
                        thread=Thread(target=data,daemon=True); thread.start()
                        source=deepcopy(binding.spec); source["source"]["socket_path"]=str(endpoint)
                        module=SimpleNamespace(**runpy.run_path(str(EXAMPLES/entry["example"])))
                        routes={freq:_MarketRoute(value["binding"],tuple(value["fields"])) for freq,value in binding.market_routes.items()}
                        try:
                            result=_run_module(module,source,market_routes=routes,research_routes=binding.research_routes)
                            report=json.loads(result.summary_json())
                            self.assertEqual(report["outcome"]["status"],"succeeded",report)
                            trades=json.loads(result.page_json("trades",None,1000))["items"]
                            self.assertTrue(trades)
                            if len(config.to_dict()["universe"]) == 2:
                                self.assert_rotation(trades,config)
                            if entry["example"]=="basic_tick.py":
                                records=json.loads(result.page_json("records",None,1000))["items"]
                                self.assertEqual(records[-1]["values"]["tick_callbacks"]["value"],72)
                                self.assertTrue(all(v["values"]["tick_time_ns"]["value"].endswith("123") for v in records))
                            result.close()
                        finally:
                            listener.close(); thread.join(5)
                            self.assertFalse(thread.is_alive()); self.assertFalse(failures,failures)

    def test_real_worker_five_examples_cli_python_web_semantics_and_documented_commands(self):
        if os.environ.get("QF_D14_REAL_WORKER_REQUIRED") != "1":
            self.skipTest("actual runner-container -> sibling worker path is mandatory in isolation CI")
        from app.backtest_service.process import DockerDaemon
        evidence=[]
        for original in ENTRIES:
            variants=[original]
            if original["example"] == "intraday.py":
                minute=deepcopy(original); minute["config"]["frequency"]="1m"; variants.append(minute)
            for entry in variants:
                entry,config=self.seed(entry)
                # Keep the private root short enough for the unchanged D12
                # run/claim/control.sock path within Linux's Unix socket limit.
                work_root=self.base/("w-"+uuid4().hex[:8]); work_root.mkdir()
                settings=self.settings.model_copy(update=dict(s3_work_root=work_root,
                    s3_daemon_work_root=Path(os.environ["QF_TEST_DAEMON_ROOT"])/work_root.relative_to(Path("/qf-work")),
                    s3_worker_image=os.environ["QF_TEST_WORKER_IMAGE"]))
                repository,process,services=create_services(settings=settings,engine=self.engine,provider=self.provider,daemon=DockerDaemon())
                self.provider.process=process
                supervisor=RunnerSupervisor(repository,process,services,lease_seconds=10)
                with tempfile.TemporaryDirectory() as directory:
                    path=self.config_file(config,directory)
                    self.assertFalse(self.cli("preflight",path)["errors"])
                    key="d14-"+uuid4().hex
                    row=self.cli("run",path,"--idempotency-key",key)
                    self.runs.append(row["run_id"])
                    self.assertEqual(self.client.submit(config,idempotency_key=key)["run_id"],row["run_id"])
                    other=self.client.submit(config,idempotency_key=key+"-python")
                    self.runs.append(other["run_id"])
                    try:
                        states=self.wait(supervisor,[row["run_id"],other["run_id"]])
                        self.assertTrue(all(s["status"]=="succeeded" for s in states),states)
                        result=self.cli("result",row["run_id"])
                        python_result=self.client.result(other["run_id"])
                        self.assertEqual(result["performance"],python_result["performance"])
                        self.assertEqual(result["execution_model"],python_result["execution_model"])
                        self.assertFalse(result["partial"])
                        trades=self.cli("result",row["run_id"],"--kind","trades","--limit","1000")["items"]
                        self.assertTrue(trades)
                        orders=self.client.result(row["run_id"],kind="orders",limit=1000)["items"]
                        self.assertFalse([o for o in orders if o["status"]=="rejected"],orders)
                        if len(config.to_dict()["universe"])==2:self.assert_rotation(trades,config)
                        if entry["example"]=="basic_tick.py":
                            records=self.client.result(row["run_id"],kind="records",limit=1000)["items"]
                            self.assertEqual(records[-1]["values"]["tick_callbacks"]["value"],72)
                        page=self.cli("result",row["run_id"],"--kind","equity","--limit","1")
                        following=self.cli("result",row["run_id"],"--kind","equity","--limit","1","--cursor",page["next_cursor"])
                        self.assertNotEqual(page["items"],following["items"])
                        self.assertEqual(self.cli("cancel",row["run_id"])["status"],"succeeded")
                        if entry["example"]=="etf_rotation.py":
                            commands=self.documented_commands(supervisor,config,path,row,other,key,directory)
                            source=(EXAMPLES/entry["example"]).read_text()+"\ndef before_trading_start(context):\n    get_price(context.state.pool, count=1, end=context.now_ns+1, fields=['close'])\n"
                            _,revision=catalogue(self.engine,source)
                            future=RunConfig.from_dict(dict(config.to_dict(),strategy_revision_id=str(revision)))
                            future_path=self.config_file(future,directory,"future.json")
                            self.assertFalse(self.cli("preflight",future_path)["errors"])
                            refused=self.cli("submit",future_path,"--idempotency-key","future-"+uuid4().hex)
                            self.runs.append(refused["run_id"])
                            state=self.wait(supervisor,[refused["run_id"]])[0]
                            self.assertEqual(state["status"],"failed",state)
                            self.assertEqual(state["error"]["code"],"LOOKAHEAD_FORBIDDEN",state)
                            self.assertTrue(self.cli("result",refused["run_id"])["partial"])
                        evidence.append(dict(example=entry["example"],frequency=config.to_dict()["frequency"],run_id=row["run_id"],trades=len(trades),status="succeeded"))
                        self.assertFalse(list(work_root.iterdir()))
                    finally:supervisor.stop()
        (self.base/"d14-examples.json").write_text(json.dumps(dict(production_executed=False,full_B1_B4=False,
            topology="runner_container_to_sibling_worker",examples=evidence,documented_commands=commands),indent=2)+"\n")


if __name__ == "__main__": unittest.main(verbosity=2)
