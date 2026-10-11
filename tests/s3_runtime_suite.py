"""Real runner-container -> sibling-worker regressions; synthetic facts only.

This file is copied into an immutable private test runner, never a worker mount.
No skips or mocked Docker pass; exit nonzero if isolation is unavailable.
"""

import json
import os
import time
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread

from app.backtest_service.local_queue import LocalRunRepository
from app.backtest_service.process import (
    LABEL,
    DockerDaemon,
    WorkerProcess,
    WorkRoot,
    labels,
)
from app.backtest_service.runtime_contracts import (
    AcceptedWork,
    Claim,
    RuntimeFault,
    WorkerLimits,
)
from app.backtest_service.supervisor import Binding, DataRunServices, RunnerSupervisor
from app.runner.__main__ import build_supervisor
from quantfoundry import _native
from support_sdk import batch, event_key, spec

ROOT = Path("/qf-work")
HOST = os.environ["QF_TEST_DAEMON_ROOT"]
IMAGE = os.environ["QF_TEST_WORKER_IMAGE"]
RUNNER = os.environ["QF_TEST_RUNNER_NAME"]
EVIDENCE = dict(
    production_executed=False,
    full_B1_B4=False,
    topology="runner_container_to_sibling_worker",
    runner_work_root=str(ROOT),
    daemon_host_root=HOST,
    tests=[],
    samples=[],
)


class FixtureGateway:
    """Synthetic data adapter through the real D04 serve and framing.

    Formal CurrentStore/quality regressions run in the existing S3 CI job.
    This adapter has no accepted production datasets or source fallback.
    """

    def __init__(self, source, claim, stopped):
        from types import SimpleNamespace

        self.grant = SimpleNamespace(run_id=claim.run_id)
        self.events = source["source"]["events"]
        self.batch_rows = 4
        self.batch_bytes = 1024 * 1024
        self.cancelled = stopped.is_set
        self.closed = False
        self.changed = False
        self.calls = []

    def check(self, context):
        if self.closed:
            raise RuntimeFault("INVALID_CONTRACT", "closed fixture")
        if self.changed:
            raise RuntimeFault("DATA_CHANGED", "changed fixture")
        return "unchanged"

    def _request(self, name, request, context):
        self.check(context)
        if name != "market" or not set(request["securities"]) <= set(
            e["event"]["security"] for e in self.events
        ):
            raise RuntimeFault("DATA_RESTRICTED", "fixture scope")

    def read(self, name, request, context, *, page_rows):
        import pyarrow as pa
        from app.backtest_service.data_gateway import DataBatch

        self.calls.append(request)
        selected = [
            e
            for e in self.events
            if e["event"]["security"] in request["securities"]
            and event_key(e)[0] <= int(request["end_ns"])
            and (
                request["start_ns"] is None
                or event_key(e)[0] >= int(request["start_ns"])
            )
        ]
        count = request["count_per_security"]
        if count is not None:
            selected = [
                e
                for s in request["securities"]
                for e in [x for x in selected if x["event"]["security"] == s][-count:]
            ]
        selected.sort(key=event_key)
        for offset in range(0, len(selected), page_rows):
            self.check(context)
            rows = selected[offset : offset + page_rows]
            table = pa.ipc.open_stream(batch(rows)).read_all()
            times = [event_key(e)[0] for e in rows]
            yield DataBatch(
                table,
                dict(
                    schema_id="qf.market.v1",
                    rows=len(rows),
                    time_unit="utc_nanoseconds",
                    price_currency="CNY",
                    quantity_unit="shares",
                    actual_scope=dict(
                        start_ns=str(min(times)),
                        end_ns=str(max(times)),
                        securities=sorted({e["event"]["security"] for e in rows}),
                    ),
                    limitations=["synthetic_isolation_fixture"],
                ),
            )

    def finalize(self, context, commit):
        self.check(context)
        return commit(None)

    def close(self, context):
        self.closed = True


class Fixtures(DataRunServices):
    def __init__(self, results, *, ipc=False):
        super().__init__(self.builder, results)
        self.ipc = ipc
        self.bindings = []

    def builder(self, work, claim, stopped):
        source = deepcopy(work.specification)
        source["run_id"] = claim.run_id
        gateway = FixtureGateway(source, claim, stopped)
        if self.ipc:
            from types import SimpleNamespace

            fields = ["open", "high", "low", "close", "quantity"]
            source["source"] = dict(
                kind="ipc",
                socket_path="/run/qf/ipc/data.sock",
                binding="market",
                request=dict(
                    securities=work.config["universe"],
                    fields=fields,
                    frequency=work.config["frequency"],
                    start_ns=source["calendar"][0]["before_open_ns"],
                    end_ns=source["calendar"][-1]["after_close_ns"],
                    count_per_security=None,
                    adjustment="none",
                ),
            )
            context = SimpleNamespace(
                to_dict=lambda: dict(scope_id="synthetic-isolation", dependencies=[])
            )
            binding = Binding(
                source,
                market_routes={
                    work.config["frequency"]: dict(binding="market", fields=fields)
                },
                gateway=gateway,
                context=context,
            )
        else:
            binding = Binding(source, gateway=gateway, context=object())
        self.bindings.append(binding)
        return binding

    def prepare(self, claim, work, ipc_directory, stopped):
        if self.ipc:
            return super().prepare(claim, work, ipc_directory, stopped)
        return self.builder(work, claim, stopped)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        import hashlib

        self.base = ROOT / (
            "c-" + hashlib.sha256(self._testMethodName.encode()).hexdigest()[:8]
        )
        self.base.mkdir(mode=0o700)
        for p in ("queue", "work", "results"):
            (self.base / p).mkdir(mode=0o700)
        self.daemon = DockerDaemon()
        self.repository = LocalRunRepository(self.base / "queue")
        root = WorkRoot(
            self.base / "work", Path(HOST) / self.base.relative_to(ROOT) / "work"
        )
        self.process = WorkerProcess(self.daemon, root, IMAGE)
        self.services = Fixtures(self.base / "results")
        self.supervisors = [
            build_supervisor(self.repository, self.process, self.services)
            for _ in range(2)
        ]

    def tearDown(self):
        for supervisor in self.supervisors:
            supervisor.stop()
        canary = self.base / "work" / "secret"
        if canary.is_file():
            canary.unlink()
        self.assertEqual(list((self.base / "work").iterdir()), [])
        EVIDENCE["tests"].append(self._testMethodName)

    def work(self, code, *, parameters=None, limits=None, source=None):
        source = source or spec(days=1, securities=("A.SH",), frequency="1m")
        if parameters is not None:
            source["config"]["parameters"] = parameters
        return AcceptedWork(
            "synthetic-owner", source["config"], code, limits or WorkerLimits(), source
        )

    def enqueue(self, work):
        return self.repository.enqueue_batch([work])[1][0]

    def tick(self):
        for supervisor in self.supervisors:
            supervisor.step()
        count = sum(len(s.tasks) for s in self.supervisors)
        self.assertLessEqual(count, 2)
        sampled = []
        for supervisor in self.supervisors:
            for task in supervisor.tasks.values():
                row = self.daemon.inspect(task.name)
                if row is None:
                    continue
                host = row["HostConfig"]
                config = row["Config"]
                self.assertEqual(host["NetworkMode"], "none")
                self.assertTrue(host["ReadonlyRootfs"])
                self.assertEqual(config["User"], "65532:65532")
                self.assertEqual(host["CapDrop"], ["ALL"])
                self.assertIn("no-new-privileges", host["SecurityOpt"])
                self.assertEqual(host["Memory"], host["MemorySwap"])
                self.assertEqual(host["PidsLimit"], task.work.limits.pids)
                self.assertEqual(
                    host["NanoCpus"], task.work.limits.cpu_millis * 1_000_000
                )
                self.assertEqual(
                    {m["Destination"] for m in row["Mounts"]},
                    {"/run/qf/code", "/run/qf/ipc"},
                )
                self.assertTrue(
                    all(
                        not m["RW"] and str(m["Source"]).startswith(HOST + "/")
                        for m in row["Mounts"]
                    )
                )
                stats = self.daemon.request(
                    "GET",
                    "/containers/" + task.name + "/stats?stream=false&one-shot=true",
                )
                sampled.append(
                    dict(
                        run_id=task.claim.run_id,
                        rss_cgroup=stats.get("memory_stats", {}).get("usage", 0),
                        pids=stats.get("pids_stats", {}).get("current", 0),
                        memory_limit=host["Memory"],
                        cpu_millis=host["NanoCpus"] // 1_000_000,
                    )
                )
        if sampled:
            EVIDENCE["samples"].append(sampled)
        parent = self.daemon.inspect(RUNNER)
        self.assertEqual(parent["HostConfig"]["Memory"], 1024**3)
        self.assertEqual(parent["HostConfig"]["NanoCpus"], 1000 * 1_000_000)
        self.assertEqual(parent["HostConfig"]["PidsLimit"], 64)
        stats = self.daemon.request(
            "GET", "/containers/" + RUNNER + "/stats?stream=false&one-shot=true"
        )
        parent_bytes = stats.get("memory_stats", {}).get("usage", 0)
        EVIDENCE.setdefault("group_samples", []).append(
            dict(
                runner_cgroup_bytes=parent_bytes,
                total_cgroup_bytes=parent_bytes + sum(x["rss_cgroup"] for x in sampled),
                workers=len(sampled),
            )
        )
        self.assertLessEqual(
            parent_bytes + sum(x["rss_cgroup"] for x in sampled), 8 * 1024**3
        )
        # Assigned group budget, including reserved trusted runner/gateway/DB.
        self.assertLessEqual(
            sum(x["memory_limit"] for x in sampled) + 1024**3, 8 * 1024**3
        )
        self.assertLessEqual(sum(x["cpu_millis"] for x in sampled) + 1000, 4000)
        self.assertTrue(all(x["rss_cgroup"] <= x["memory_limit"] for x in sampled))

    def wait_runs(self, ids, *, timeout=100):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.tick()
            rows = [self.repository.status("synthetic-owner", i) for i in ids]
            if all(r["status"] in ("succeeded", "failed", "cancelled") for r in rows):
                return rows
            Event().wait(0.02)
        self.fail("real worker test did not finish")

    def reader(self, row):
        self.assertIsNotNone(row["result"])
        return _native.RetainedResultReader(
            str(self.base / "results"), row["result"], row["run_id"]
        )

    def test_network_secret_socket_files_and_private_tmp_denied(self):
        (self.base / "work" / "secret").write_text("synthetic-secret-do-not-leak")
        # This deliberately outside-run canary is protected by the mount boundary.
        code = """import os, socket, pathlib, resource
from quantfoundry.api import record

def initialize(c):
    assert os.getuid()==65532
    assert not any(k.startswith(('QF_DATABASE','QF_API','AWS_')) for k in os.environ)
    assert not pathlib.Path('/var/run/docker.sock').exists()
    assert not pathlib.Path('/app').exists()
    for path in ['/qf-work','/workspace','/run/qf/code/../secret','/run/qf/ipc/../../secret']:
        assert not pathlib.Path(path).exists(), path
    try:
        pathlib.Path('/root-write').write_text('x')
        raise AssertionError('root writable')
    except OSError: pass
    try:
        pathlib.Path('/run/qf/code/strategy.py').write_text('replace')
        raise AssertionError('code writable')
    except OSError: pass
    for address in [('1.1.1.1',443),('172.17.0.1',80)]:
        s=socket.socket();s.settimeout(.1)
        try:
            s.connect(address);raise AssertionError('network access')
        except OSError:pass
        finally:s.close()
    status=pathlib.Path('/proc/self/status').read_text()
    assert 'NoNewPrivs:\\t1' in status and 'CapEff:\\t0000000000000000' in status
    assert 'Seccomp:\\t2' in status
    assert resource.getrlimit(resource.RLIMIT_NOFILE)==(128,128)
    assert pathlib.Path('/proc/net/route').read_text().count('\\n')==1
    try:
        with open('/tmp/oversize','wb') as f:
            while True:f.write(b'x'*1024*1024)
    except OSError:pass
    assert pathlib.Path('/tmp/oversize').stat().st_size==32*1024*1024
    assert os.statvfs('/tmp').f_blocks*os.statvfs('/tmp').f_frsize<=64*1024*1024
    full=False
    for n in range(3):
        try:
            with open('/tmp/block-'+str(n),'wb') as f:
                for _ in range(24):f.write(b'x'*1024*1024)
        except OSError:full=True;break
    assert full, 'tmpfs quota not enforced'
    for name in ['oversize','block-0','block-1','block-2']:
        pathlib.Path('/tmp',name).unlink(missing_ok=True)
    import signal,time
    children=[]
    try:
        for _ in range(40):
            try:pid=os.fork()
            except OSError:break
            if pid==0:time.sleep(10);os._exit(0)
            children.append(pid)
        assert 0<len(children)<40, 'pids quota not enforced'
    finally:
        for pid in children:os.kill(pid,signal.SIGKILL)
        for pid in children:os.waitpid(pid,0)
    record(isolation='actual-worker-denials')
"""
        row = self.wait_runs([self.enqueue(self.work(code))])[0]
        self.assertEqual(row["status"], "succeeded", row)
        pages = json.loads(self.reader(row).page_json("records", None, 10))
        self.assertEqual(
            pages["items"][0]["values"]["isolation"]["value"], "actual-worker-denials"
        )
        (self.base / "work" / "secret").unlink()

    def test_true_d04_ipc_rust_account_results_and_no_old_engine(self):
        self.services.ipc = True
        code = """import sys
class DenyOld:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('app.backtesting'):raise AssertionError('legacy engine import')
sys.meta_path.insert(0,DenyOld())
from quantfoundry.api import get_price, order, LimitOrder, record

def before_trading_start(c): order('A.SH',100,style=LimitOrder('10'))
def handle_data(c,d):
    frame=get_price('A.SH',count=2,fields=['close'])
    assert len(frame)<=2
    if c.portfolio.positions['A.SH'].quantity==100 and not getattr(c.state,'sold',False):
        c.state.sold=True;order('A.SH',-40,style=LimitOrder('12'))
    record(cash=c.portfolio.cash)
"""
        source = spec(days=1, securities=("A.SH",), frequency="1m")
        for row in source["source"]["events"][1:]:
            row["event"].update(open="12", high="13", low="11", close="12")
        row = self.wait_runs([self.enqueue(self.work(code, source=source))])[0]
        self.assertEqual(row["status"], "succeeded", row)
        reader = self.reader(row)
        summary = json.loads(reader.summary_json())
        self.assertEqual(
            Decimal(summary["performance"]["terminal_equity"]), Decimal("10198")
        )
        self.assertEqual(
            Decimal(summary["performance"]["trades"]["fill_fees"]), Decimal("2")
        )
        self.assertEqual(
            len(json.loads(reader.page_json("trades", None, 10))["items"]), 2
        )
        self.assertGreater(len(self.services.bindings[0].gateway.calls), 1)

    def test_16_parameters_two_supervisors_one_failure_retry_keeps_successes(self):
        code = """from quantfoundry.api import record
import time

def initialize(c):
    time.sleep(.1)
    assert c.portfolio.cash==10000
    record(parameter=c.params['i'],initial_cash=c.portfolio.cash)
"""
        items = [self.work(code, parameters=dict(i=i)) for i in range(16)]
        batch_id, ids = self.repository.enqueue_batch(items)
        end = time.monotonic() + 100
        injected = False
        while time.monotonic() < end:
            self.tick()
            if not injected:
                for supervisor in self.supervisors:
                    for task in supervisor.tasks.values():
                        if task.work.config["parameters"]["i"] == 7:
                            self.daemon.request(
                                "POST", "/containers/" + task.name + "/kill?signal=KILL"
                            )
                            injected = True
            rows = [self.repository.status("synthetic-owner", i) for i in ids]
            if all(r["status"] in ("failed", "succeeded") for r in rows):
                break
            Event().wait(0.02)
        self.assertTrue(injected)
        self.assertEqual([r["status"] for r in rows].count("failed"), 1)
        original = {
            r["run_id"]: r["result"] for r in rows if r["status"] == "succeeded"
        }
        # The failed worker is restarted with the identical accepted source/config.
        retry = self.repository.retry_failed("synthetic-owner", batch_id)
        self.assertEqual(len(retry), 1)
        rerun = self.wait_runs(retry)[0]
        self.assertEqual(rerun["status"], "succeeded", rerun)
        current = self.repository.batch_status("synthetic-owner", batch_id, limit=256)[
            "items"
        ]
        self.assertEqual([r["status"] for r in current], ["succeeded"] * 16)
        self.assertEqual(
            {r["run_id"]: r["result"] for r in current if r["run_id"] in original},
            original,
        )
        starts = [r for s in self.supervisors for r in s.started_runs]
        self.assertEqual(len(starts), 17)
        self.assertEqual(len(starts), len(set(starts)))
        for row in current:
            record = json.loads(self.reader(row).page_json("records", None, 10))[
                "items"
            ][0]["values"]
            self.assertEqual(record["initial_cash"]["value"], "10000")
        EVIDENCE["parameter_batch"] = dict(
            parameters=16,
            maximum_concurrency=max(s["workers"] for s in EVIDENCE["group_samples"]),
            total_executions=17,
            successful_results_retained=15,
        )

    def test_python_exception_ffi_crash_oom_wall_and_output(self):
        cases = [
            (
                'def initialize(c): raise ValueError("intentional")',
                "STRATEGY_ERROR",
                WorkerLimits(),
            ),
            (
                "import ctypes\ndef initialize(c): ctypes.string_at(0)",
                "STRATEGY_ERROR",
                WorkerLimits(),
            ),
            (
                "def initialize(c):\n    blobs=[]\n    while True: blobs.append(bytearray(8*1024*1024))",
                "RESOURCE_LIMIT",
                WorkerLimits(memory_bytes=256 * 1024**2),
            ),
            (
                "def initialize(c):\n    while True: pass",
                "RESOURCE_LIMIT",
                WorkerLimits(wall_seconds=2),
            ),
            (
                'def initialize(c):\n    for i in range(10000):print("x"*1024)',
                "succeeded",
                WorkerLimits(output_bytes=4096),
            ),
        ]
        for code, expected, limits in cases:
            row = self.wait_runs([self.enqueue(self.work(code, limits=limits))])[0]
            if expected == "succeeded":
                self.assertEqual(row["status"], expected, row)
                self.assertTrue(
                    json.loads(self.reader(row).summary_json())["notices"][
                        "logs_truncated"
                    ]
                )
            else:
                self.assertEqual(row["status"], "failed", row)
                self.assertEqual(row["outcome"]["error"]["code"], expected, row)

    def test_cancel_noncooperative_worker_and_batch_preserves_complete(self):
        done = self.wait_runs([self.enqueue(self.work("def initialize(c): pass"))])[0]
        code = "def initialize(c):\n    while True: pass"
        batch, ids = self.repository.enqueue_batch(
            [self.work(code, parameters={"i": i}) for i in range(4)]
        )
        self.tick()
        self.tick()
        self.repository.cancel_batch("synthetic-owner", batch)
        rows = self.wait_runs(ids)
        self.assertEqual([r["status"] for r in rows], ["cancelled"] * 4)
        self.assertEqual(
            self.repository.request_cancel("synthetic-owner", done["run_id"])["status"],
            "succeeded",
        )
        self.assertEqual(
            self.repository.status("synthetic-owner", done["run_id"])["result"],
            done["result"],
        )

    def test_expired_heartbeat_old_claim_late_submission_and_crash_restart(self):
        run_id = self.enqueue(
            self.work(
                "def initialize(c):\n    while True: pass",
                limits=WorkerLimits(wall_seconds=20),
            )
        )
        first = self.supervisors[0]
        first.step()
        task = first.tasks[run_id]
        # Deliberate private-daemon process failure, followed by lease expiry.
        self.daemon.request("POST", "/containers/" + task.name + "/kill?signal=KILL")
        with self.repository._locked():
            row = self.repository._row(run_id)
            row["lease_until"] = time.time() - 1
            self.repository._write(run_id + ".json", row)
        # New supervisor must terminate/confirm old container before next attempt.
        self.supervisors[1].step()
        second = next(iter(self.supervisors[1].tasks.values()))
        self.assertGreater(second.claim.fence, task.claim.fence)
        self.assertEqual(
            self.daemon.inspect(second.name)["Config"]["Labels"][LABEL + ".token"],
            second.claim.token,
        )
        self.assertFalse(
            self.repository.commit_outcome(
                task.claim, dict(status="succeeded", result_id=run_id), "forged"
            )
        )
        self.assertFalse(self.repository.heartbeat(task.claim, time.time(), 5))
        self.repository.request_cancel("synthetic-owner", run_id)
        self.wait_runs([run_id])
        # The old task's cleanup is safe after recovery; never kills the new one.
        first.step()

    def test_path_injection_unknown_control_and_data_changed_commit(self):
        for bad in ["../x/code", "/tmp/code", "x/../ipc", "x/code/extra"]:
            with self.assertRaises(RuntimeFault):
                self.process.root.host_path(bad)
        alias = "qf-s3-" + "a" * 32
        (self.base / "work" / alias).symlink_to("/tmp")
        with self.assertRaises(RuntimeFault):
            self.process.root.host_path(alias + "/code")
        (self.base / "work" / alias).unlink()
        run_id = self.enqueue(
            self.work("import time\ndef initialize(c):time.sleep(.4)")
        )
        self.tick()
        self.services.bindings[-1].gateway.changed = True
        row = self.wait_runs([run_id])[0]
        self.assertEqual(row["status"], "failed", row)
        self.assertEqual(row["outcome"]["error"]["code"], "DATA_CHANGED")

    def test_raw_unknown_enum_oversize_and_result_path_injection_refused(self):
        for mode, expected in [
            ("unknown", "INVALID_CONTRACT"),
            ("oversize", "RESOURCE_LIMIT"),
            ("path", "INVALID_CONTRACT"),
        ]:
            with self.subTest(mode=mode):
                code = """import json,sys
from quantfoundry._transport import PREFIX,PROTOCOL
def initialize(c):
    client=sys.stdout.client
    sock=client.channel.sock
    mode=c.params['mode']
    item=dict(protocol=PROTOCOL,run_id=client.run_id,request_id=client.sequence+1,
              op='unknown' if mode=='unknown' else 'result',status='request',body={},payload_bytes=0)
    if mode=='path':item['body']={'records':[],'path':'/etc/passwd'}
    data=json.dumps(item).encode()
    size=1024*1024+1 if mode=='oversize' else len(data)
    sock.sendall(PREFIX.pack(b'QFD4',size,0)+data)
    assert sock.recv(1)==b''
"""
                row = self.wait_runs(
                    [self.enqueue(self.work(code, parameters=dict(mode=mode)))]
                )[0]
                self.assertEqual(row["status"], "failed", row)
                self.assertEqual(row["outcome"]["error"]["code"], expected, row)

    def test_container_replacement_between_inspect_and_stop_is_not_killed(self):
        name = "qf-s3-d12-name-race-" + self.base.name
        old, new = Claim(name, "old-test-token", 1), Claim(name, "new-test-token", 2)
        fixture = dict(
            Image=IMAGE,
            User="65532:65532",
            Entrypoint=["python", "-c"],
            Cmd=["import time; time.sleep(60)"],
            HostConfig=dict(
                NetworkMode="none",
                ReadonlyRootfs=True,
                CapDrop=["ALL"],
                SecurityOpt=["no-new-privileges"],
                Memory=32 * 1024**2,
                MemorySwap=32 * 1024**2,
                NanoCpus=100_000_000,
                PidsLimit=8,
                LogConfig=dict(Type="none"),
            ),
        )

        def create(claim):
            identity = self.daemon.request(
                "POST",
                "/containers/create?name=" + name,
                dict(fixture, Labels=labels(claim)),
            )["Id"]
            self.daemon.request("POST", "/containers/" + identity + "/start")
            return identity

        create(old)
        daemon = self.daemon
        replacement = []

        class RacingDaemon(DockerDaemon):
            def inspect(self, identity):
                row = super().inspect(identity)
                if identity == name and not replacement:
                    daemon.stop_remove(name, old)
                    replacement.append(create(new))
                return row

        try:
            RacingDaemon().stop_remove(name, old)
            self.assertTrue(self.daemon.inspect(replacement[0])["State"]["Running"])
        finally:
            self.daemon.stop_remove(name, new if replacement else old)

    def test_busy_callback_keeps_claim_alive_without_scheduler_polling(self):
        self.supervisors = [
            RunnerSupervisor(
                self.repository, self.process, self.services, lease_seconds=2
            )
        ]
        run_id = self.enqueue(
            self.work("import time\ndef initialize(c): time.sleep(3)")
        )
        self.tick()
        task = self.supervisors[0].tasks[run_id]
        # The scheduler is deliberately idle beyond its claim lease. Renewal
        # must come from the activity thread, without a Python callback heartbeat.
        Event().wait(2.5)
        self.assertTrue(self.repository.heartbeat(task.claim, time.time(), 2))
        row = self.wait_runs([run_id])[0]
        self.assertEqual(row["status"], "succeeded", row)
        self.assertEqual(row["fence"], 1)
        self.assertEqual(list(self.supervisors[0].started_runs), [run_id])

    def test_worker_stop_leaves_private_publisher_running(self):
        name = "qf-s3-d12-publisher-" + self.base.name
        created = self.daemon.request(
            "POST",
            "/containers/create?name=" + name,
            dict(
                Image=IMAGE,
                User="65532:65532",
                Entrypoint=["python", "-c"],
                Cmd=["import time; n=0\nwhile True:\n n+=1; time.sleep(.01)"],
                Labels={
                    "io.quantfoundry.s3.private-publisher": self.base.name,
                    "io.quantfoundry.s3.private-test-root": HOST,
                },
                HostConfig=dict(
                    NetworkMode="none",
                    ReadonlyRootfs=True,
                    CapDrop=["ALL"],
                    SecurityOpt=["no-new-privileges"],
                    Memory=32 * 1024**2,
                    MemorySwap=32 * 1024**2,
                    NanoCpus=100_000_000,
                    PidsLimit=8,
                    LogConfig=dict(Type="none"),
                ),
            ),
        )
        identity = created["Id"]
        try:
            self.daemon.request("POST", "/containers/" + identity + "/start")
            started = self.daemon.inspect(identity)["State"]["StartedAt"]
            first = self.daemon.request(
                "GET", "/containers/" + identity + "/stats?stream=false&one-shot=true"
            )
            claim = Claim("different-run", "different-token", 1)
            with self.assertRaises(RuntimeFault):
                self.daemon.stop_remove(name, claim)
            run_id = self.enqueue(self.work("def initialize(c):\n    while True: pass"))
            self.tick()
            self.repository.request_cancel("synthetic-owner", run_id)
            self.wait_runs([run_id])
            after = self.daemon.inspect(identity)
            self.assertTrue(after["State"]["Running"])
            self.assertEqual(after["State"]["StartedAt"], started)
            second = self.daemon.request(
                "GET", "/containers/" + identity + "/stats?stream=false&one-shot=true"
            )
            self.assertGreater(
                second["cpu_stats"]["cpu_usage"]["total_usage"],
                first["cpu_stats"]["cpu_usage"]["total_usage"],
            )
            EVIDENCE["private_publisher_survived_worker_stop"] = True
        finally:
            self.daemon.request(
                "POST",
                "/containers/" + identity + "/kill?signal=KILL",
                missing=True,
                stopped=True,
            )
            self.daemon.request("DELETE", "/containers/" + identity, missing=True)

    def test_actual_cancel_success_competition_has_one_terminal(self):
        ids = [self.enqueue(self.work("def initialize(c): pass")) for _ in range(8)]

        def cancel():
            Event().wait(0.2)
            for run_id in ids:
                self.repository.request_cancel("synthetic-owner", run_id)

        thread = Thread(target=cancel)
        thread.start()
        rows = self.wait_runs(ids)
        thread.join()
        for row in rows:
            winner = row["status"]
            self.assertIn(winner, ("cancelled", "succeeded"))
            self.assertEqual(
                self.repository.request_cancel("synthetic-owner", row["run_id"])[
                    "status"
                ],
                winner,
            )


if __name__ == "__main__":
    from s3_runservice_suite import D13Tests
    from s3_cli_suite import D14Tests
    assert os.environ.get("QF_D13_REAL_WORKER_REQUIRED") == "1", "D13 real DB integration is mandatory"
    assert os.environ.get("QF_D14_REAL_WORKER_REQUIRED") == "1", "D14 real HTTP/client/examples integration is mandatory"
    suite = unittest.TestSuite([
        unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeTests),
        unittest.defaultTestLoader.loadTestsFromTestCase(D13Tests),
        unittest.defaultTestLoader.loadTestsFromTestCase(D14Tests),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    EVIDENCE["d13_database_http_gateway_result_commit"] = True
    EVIDENCE["d14_installed_cli_python_five_examples"] = True
    EVIDENCE["skipped"] = len(result.skipped)
    EVIDENCE["success"] = result.wasSuccessful()
    EVIDENCE["test_count"] = result.testsRun
    # Samples are bounded by the test workload, not per market event.
    (ROOT / "isolation-evidence.json").write_text(json.dumps(EVIDENCE, indent=2) + "\n")
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
