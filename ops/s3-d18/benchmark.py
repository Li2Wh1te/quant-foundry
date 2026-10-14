"""D18 runner-container integration. Fixed FULL loads; smoke is never acceptance."""

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4
import hashlib, json, os, platform, subprocess, sys, time, traceback
import pyarrow as pa
from decimal import Decimal
from sqlalchemy import create_engine, text
from app.backtest_service.data_gateway import (
    AuthorizedRun,
    MarketBinding,
    RunDataGateway,
    DataBatch,
    MARKET_SCHEMA,
)
from app.backtest_service.resampling import ResampledBarGateway
from app.backtest_service.account_config import commission_from_account_fee_schedule
from app.backtest_service.repository import DatabaseRunRepository
from app.backtest_service.service import RunService, normalize
from app.backtest_service.supervisor import Binding, RunnerSupervisor, DataRunServices
from app.backtest_service.runtime_contracts import (
    GroupBudget,
    WorkerLimits,
    RuntimeFault,
    TERMINAL,
)
from app.backtest_service.process import DockerDaemon, WorkerProcess, WorkRoot
from app.backtest_service.results import read_result, compare_runs
from app.core.config import get_settings
from app.core.auth import AuthenticatedPrincipal
from app.data_store.storage import CurrentStore
from app.data_store.limits import StoreLimits
from app.data_store.catalog import SourceUpdate
from app.data_store.schema import fingerprint
from app.legacy_reset.tables import maintenance
from quantfoundry import _native
from s3_runservice_suite import catalogue, CURSOR, OWNER
from fixtures import FULL, SMOKE, DAY, MODEL, dataset, template, facts, batches
from strategies import MOMENTUM, INTRADAY, TICKS, HOT_PROBE

ROOT = Path("/qf-work/final")


class MeasuredGateway(RunDataGateway):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.elapsed = dict(
            read_seconds=0.0,
            conversion_seconds=0.0,
            check_seconds=0.0,
            ipc_encode_seconds=0.0,
        )
        self.scan_bytes = 0

    def check(self, context):
        started = time.monotonic()
        try:
            return super().check(context)
        finally:
            self.elapsed["check_seconds"] += time.monotonic() - started

    def _batch(self, table, binding):
        started = time.monotonic()
        try:
            value = super()._batch(table, binding)
            original = value.ipc

            def ipc(budget):
                t = time.monotonic()
                try:
                    return original(budget)
                finally:
                    self.elapsed["ipc_encode_seconds"] += time.monotonic() - t

            object.__setattr__(value, "ipc", ipc)
            return value
        finally:
            self.elapsed["conversion_seconds"] += time.monotonic() - started

    def _pages(self, *a, **k):
        iterator = super()._pages(*a, **k)
        while True:
            started = time.monotonic()
            try:
                value = next(iterator)
            except StopIteration:
                break
            finally:
                self.elapsed["read_seconds"] += time.monotonic() - started
            yield value


class HourlyGateway(ResampledBarGateway, MeasuredGateway):
    """Measure the same bounded production resampler used by FormalProvider."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, target_frequency='60m', **kwargs)


class Provider:
    def __init__(self, store, load, frequency, process, telemetry):
        self.store, self.load, self.frequency, self.process, self.telemetry = (
            store,
            load,
            frequency,
            process,
            telemetry,
        )
        self.spec = template(load)
        self.calendar = {s["session"]["key"]: s for s in self.spec["calendar"]}
        self.dataset = dataset(load)
        self.gateways = {}
        self.market = MarketBinding(
            "market",
            self.dataset,
            "tick" if load.ticks else "1m" if load.bars == 240 else "1d",
            {f: f for f in MARKET_SCHEMA.names},
            {},
            lambda security, start, end: ((start,), (end + 1,)),
            "time_ns",
        )

    def capabilities(self):
        return [dict(binding=self.dataset.name, available=True, synthetic=True)]

    def check(self, work):
        self.process.daemon.preflight(self.process.image)
        gw, ctx = self.gateway(work, "preflight-" + uuid4().hex, Event())
        try:
            gw.check(ctx)
        finally:
            gw.close(ctx)
        return self.spec["calendar_reference"]

    def request(self, work):
        return dict(
            securities=work.config["universe"],
            fields=(
                ["price", "quantity", "bid", "ask", "bid_quantity", "ask_quantity"]
                if self.load.ticks
                else ["open", "high", "low", "close", "quantity"]
            ),
            frequency=self.frequency,
            start_ns=self.spec["calendar"][0]["before_open_ns"],
            end_ns=self.spec["calendar"][-1]["after_close_ns"],
            count_per_security=None,
            adjustment="none",
        )

    def gateway(self, work, run_id, stopped):
        grant = AuthorizedRun(
            run_id,
            work.owner,
            tuple(work.config["universe"]),
            int(self.spec["calendar"][0]["before_open_ns"]),
            int(self.spec["calendar"][-1]["after_close_ns"]),
        )
        kind = HourlyGateway if self.frequency == "60m" else MeasuredGateway
        gw = kind(
            self.store,
            AuthenticatedPrincipal(work.owner),
            grant,
            (self.market,),
            batch_rows=10000,
            batch_bytes=64 * 1024**2,
            cache_rows=100000,
            cache_bytes=64 * 1024**2,
            cancelled=stopped.is_set,
            # B1 fills the same 10k row credit with two 5k-security sessions.
            # Rust still clips strategy visibility and every physical read
            # retains CurrentStore gates. Time chunking creates no source copy.
            replay_window_ns=(5_000_000 if self.load.ticks else
                              2*DAY if self.load.name == 'B1' else DAY),
            replay_cache_rows=20000,
            **({"calendar": self.spec["calendar"]} if kind is HourlyGateway else {}),
        )
        ctx = gw.open(
            dict(run_id=run_id, universe=work.config["universe"]),
            dependencies=("market",),
        )
        return gw, ctx

    def binding(self, work, claim, stopped):
        gw, ctx = self.gateway(work, claim.run_id, stopped)
        self.gateways[claim.run_id] = gw
        packet = deepcopy(self.spec)
        packet.update(run_id=claim.run_id, config=work.config)
        packet["source"]["request"] = self.request(work)
        if self.load.kind == "quote_tick":
            # Independent named raw print at this session's accepted boundary.
            # The quote itself never invents a last trade or midpoint mark.
            stamp = packet["calendar"][0]["before_open_ns"]
            packet["initial_marks"] = {
                self.load.universe[0]: dict(
                    price="10.01",
                    price_time_ns=stamp,
                    known_ns=stamp,
                    session=packet["calendar"][0]["session"]["key"],
                    basis="raw",
                    source="fixture session raw print; stale throughout quote replay",
                    origin=dict(kind="synthetic", reference=MODEL),
                )
            }
        commissions = {}
        for side in ("buy", "sell"):
            value = commission_from_account_fee_schedule(
                work.specification["account_snapshot"]["fee_schedule"],
                side=side,
                included_components=(),
                cost_overrides=work.config.get("cost_overrides"),
            )
            commissions[side] = {
                k: value[k]
                for k in (
                    "commission_rate",
                    "minimum_commission",
                    "settlement_scale",
                    "rounding",
                )
            }

        def session_facts(session, offset, maximum):
            if session not in self.calendar:
                raise RuntimeFault("RULE_UNAVAILABLE", "unknown fixture session")
            return facts(
                self.load, self.calendar[session], offset, maximum, commissions
            )

        return Binding(
            packet,
            market_routes={
                self.frequency: dict(
                    binding="market", fields=self.request(work)["fields"]
                )
            },
            gateway=gw,
            context=ctx,
            session_facts=session_facts,
            telemetry=lambda value: self.telemetry.__setitem__(claim.run_id, value),
        )


class MeasuredServices(DataRunServices):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.writer_seconds = 0.0
        self.probe_reached = Event()

    def writer(self, claim, work):
        owner = self
        writer = super().writer(claim, work)

        class MeasuredWriter:
            def __getattr__(self, name):
                return getattr(writer, name)

            def write_json(self, data):
                started = time.monotonic()
                try:
                    result = writer.write_json(data)
                    packet = json.loads(data)
                    if any(
                        r.get("kind") == "log"
                        and r["record"]["message"].strip() == "D18_HOT_PROBE_STARTED"
                        for r in packet["records"]
                    ):
                        owner.probe_reached.set()
                    return result
                finally:
                    owner.writer_seconds += time.monotonic() - started

        return MeasuredWriter()


class MeasuredProcess(WorkerProcess):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_seconds = {}

    def start(self, claim, *args, **kwargs):
        started = time.monotonic()
        try:
            return super().start(claim, *args, **kwargs)
        finally:
            self.start_seconds[claim.run_id] = time.monotonic() - started


def emit(evidence):
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT / "result.json"
    temporary = path.with_suffix(".pending")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    temporary.replace(path)


def sample(daemon, supervisor):
    ids = [os.environ["QF_TEST_RUNNER_NAME"], os.environ["QF_TEST_DATABASE_NAME"]]
    ids += [t.name for t in supervisor.tasks.values() if t.name]
    group = []
    for identity in ids:
        item = daemon.inspect(identity)
        if not item or not item["State"]["Running"]:
            continue
        stats = daemon.request(
            "GET", "/containers/" + item["Id"] + "/stats?stream=false&one-shot=true"
        )
        if not stats.get("memory_stats", {}).get("usage", 0):
            current = daemon.inspect(identity)
            if current is None or not current["State"]["Running"]:
                continue
        host = item["HostConfig"]
        mem = stats.get("memory_stats", {})
        values = mem.get("stats", {})
        group.append(
            dict(
                id=item["Id"],
                memory_bytes=mem.get("usage", 0),
                rss_bytes=values.get(
                    "anon", values.get("total_rss", values.get("rss", 0))
                ),
                limit_bytes=host["Memory"],
                cpu_millis=host.get("NanoCpus", 0) // 1000000,
                pids=stats.get("pids_stats", {}).get("current", 0),
            )
        )
    assert sum(x["limit_bytes"] for x in group) <= 8 * 1024**3, group
    assert sum(x["cpu_millis"] for x in group) <= 4000, group
    assert all(0 < x["memory_bytes"] <= x["limit_bytes"] for x in group), group
    return dict(
        cgroup_bytes=sum(x["memory_bytes"] for x in group),
        rss_bytes=sum(x["rss_bytes"] for x in group),
        members=group,
    )


def prepare(store, load):
    contract = dataset(load)
    store.register(contract)
    cal = template(load)["calendar"]
    started = time.monotonic()
    rows = 0
    step = 250000 if load.ticks else max(1, 250000 // (load.securities * load.bars))
    maximum = load.ticks or load.days
    peak_tmp = 0
    for first in range(0, maximum, step):
        last = min(maximum, first + step)
        # Bounds are the actual canonical time keys. Inputs remain microbatches.
        if load.ticks:
            lo = (
                int(cal[0]["local_midnight_ns"])
                + 570 * 60_000_000_000
                + 123
                + first // 2 * 1000
            )
            hi = (
                int(cal[0]["local_midnight_ns"])
                + 570 * 60_000_000_000
                + 123
                + (last - 1) // 2 * 1000
                + 1
            )
        else:
            lo = int(cal[first]["local_midnight_ns"])
            hi = int(cal[last - 1]["local_midnight_ns"]) + DAY
        result = store.upsert(
            contract,
            "default",
            batches(load, cal, first, last, contract.schema),
            SourceUpdate(
                "d18-fixture",
                fingerprint([load.name, first, last]),
                fingerprint(MODEL),
                (store.source_state(contract.name, "d18-fixture") or {}).get(
                    "revision", 0
                ),
            ),
            lower=(lo,),
            upper=(hi,),
            source_check=lambda _: True,
        )
        rows += result.metrics.input_rows
        peak_tmp = max(peak_tmp, result.metrics.scratch_peak_bytes)
        print(
            json.dumps(
                dict(stage="fixture", load=load.name, first=first, last=last, rows=rows)
            ),
            flush=True,
        )
    with store.catalog.transaction() as c:
        count, size = c.execute(
            text(
                "SELECT coalesce(sum(row_count),0),coalesce(sum(byte_count),0) FROM data_store_files WHERE dataset=:d"
            ),
            {"d": contract.name},
        ).one()
    assert count == load.rows, (count, load.rows)
    return dict(
        seconds=time.monotonic() - started,
        rows=count,
        source_bytes=size,
        max_tmp_bytes=peak_tmp,
    )


class _CorrectionProbe:
    """Keep supervision live while a bounded synthetic write is staged."""

    def __init__(self, correct, *, seconds=15, clock=time.monotonic):
        self.correct, self.seconds, self.clock = correct, seconds, clock
        self.stopped, self.done, self.committed = Event(), Event(), Event()
        self.thread = None
        self.result = self.error = self.committed_at = self.terminal_at = None

    def _commit(self):
        self.committed_at = self.clock()
        self.committed.set()

    def _run(self):
        try:
            self.result = self.correct(self._commit, self.stopped)
        except BaseException as error:
            self.error = error
        finally:
            self.done.set()

    def poll(self, *, ready, terminal):
        if ready and self.thread is None:
            self.thread = Thread(target=self._run, daemon=True)
            self.thread.start()
        if self.error is not None:
            raise self.error
        if terminal and self.terminal_at is None:
            self.terminal_at = self.clock()
        observed = self.terminal_at if terminal else self.clock()
        if (self.committed.is_set() and observed - self.committed_at > self.seconds):
            raise TimeoutError("worker did not terminate within 15 seconds after correction commit")
        return self.done.is_set() or (terminal and self.thread is None)

    def measurement(self):
        return dict(detection_limit_seconds=self.seconds,
                    commit_observed=self.committed.is_set(),
                    commit_to_terminal_seconds=(None if self.committed_at is None or self.terminal_at is None
                                                else max(0.0, self.terminal_at - self.committed_at)))

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(5)
            if self.thread.is_alive():
                raise TimeoutError("controlled correction did not stop within its cleanup budget")


def gateway_measurements(provider, run_ids):
    return {run_id: dict(provider.gateways[run_id].stats,
                         **provider.gateways[run_id].elapsed)
            for run_id in run_ids if run_id in provider.gateways}


def run_group(
    repository, process, services, provider, run_ids, *, fault=None, hook=None
):
    supervisor = RunnerSupervisor(repository, process, services)
    started = time.monotonic()
    next_sample = 0
    peak = {}
    peak_rss = 0
    injected = False
    try:
        while True:
            supervisor.step()
            hook_finished = hook is None or hook(supervisor) is not False
            now = time.monotonic()
            if now >= next_sample:
                value = sample(process.daemon, supervisor)
                peak_rss = max(peak_rss, value["rss_bytes"])
                if value["cgroup_bytes"] > peak.get("cgroup_bytes", 0):
                    peak = value
                next_sample = now + 1
            if fault and not injected and fault in supervisor.tasks:
                task = supervisor.tasks[fault]
                if now - task.started > 1:
                    process.daemon.request(
                        "POST", "/containers/" + task.name + "/kill?signal=KILL"
                    )
                    injected = True
            states = [repository.status(OWNER, i) for i in run_ids]
            if all(s["status"] in TERMINAL for s in states) and hook_finished:
                break
            if now - started > 7500:
                raise TimeoutError("group wall budget reached; not completed")
            time.sleep(0.05)
    finally:
        supervisor.stop()
    return dict(
        wall_seconds=time.monotonic() - started,
        peak=peak,
        sampled_peak_rss_bytes=peak_rss,
        resource_sample_interval_seconds=1,
        container_start_seconds={i: process.start_seconds[i] for i in run_ids},
        peak_workers=supervisor.peak_workers,
        started_runs=list(supervisor.started_runs),
        states=states,
        fault_injected=injected,
        writer_seconds=services.writer_seconds,
        gateway=gateway_measurements(provider, run_ids),
    )


def hot_correction(store, provider, *, committed=lambda: None, stopped=None, phases=None):
    """Correct only this task's synthetic current input; fence the old reader."""
    started = time.monotonic()
    stopped = stopped or Event()
    phases = phases if phases is not None else {}
    work = SimpleNamespace(owner=OWNER, config=provider.spec["config"])
    gateway, context = provider.gateway(work, "correction-" + uuid4().hex, stopped)
    stream = gateway.read("market", provider.request(work), context)
    try:
        head = next(stream).table.slice(0, 2).to_pylist()
        phases["initial_read_seconds"] = time.monotonic() - started
        assert len(head) == 2 and head[0]["time_ns"] == head[1]["time_ns"]
        before = store.catalog.dataset(provider.dataset.name)["generation"]
        field = "price" if provider.load.kind == "trade_tick" else "ask"
        for row in head:
            row[field] = "10.02"
        current = store.source_state(provider.dataset.name, "d18-fixture")
        write_started = time.monotonic()
        correction = store.upsert(
            provider.dataset,
            "default",
            [pa.RecordBatch.from_pylist(head, schema=provider.dataset.schema)],
            SourceUpdate(
                "d18-fixture",
                fingerprint([provider.load.name, "corrected"]),
                fingerprint(MODEL),
                current["revision"],
            ),
            lower=(head[0]["time_ns"],),
            upper=(head[-1]["time_ns"] + 1,),
            source_check=lambda _: True,
            cancelled=stopped.is_set,
        )
        commit_returned = time.monotonic()
        committed()
        phases["upsert_seconds"] = commit_returned - write_started
        phases["start_to_commit_return_seconds"] = commit_returned - started
        for check in (
            lambda: next(stream),
            lambda: gateway.finalize(
                context,
                lambda _: (_ for _ in ()).throw(
                    AssertionError("old generation committed")
                ),
            ),
        ):
            try:
                check()
            except ValueError as error:
                assert getattr(error, "code", None) == "DATA_CHANGED", error
            else:
                raise AssertionError("changed input must reject old run")
        after = store.catalog.dataset(provider.dataset.name)["generation"]
        assert after > before
        phases["old_context_checks_seconds"] = time.monotonic() - commit_returned
    finally:
        stream.close()
        gateway.close(context)
    corrected_read_started = time.monotonic()
    gateway, context = provider.gateway(work, "corrected-" + uuid4().hex, stopped)
    stream = gateway.read("market", provider.request(work), context)
    try:
        observed = next(stream).table.slice(0, 2).to_pylist()
        assert [row[field] for row in observed] == ["10.02", "10.02"]
        expected = dict(time_ns=str(observed[0]["time_ns"]), field=field, value="10.02")
        phases["corrected_read_seconds"] = time.monotonic() - corrected_read_started
    finally:
        stream.close()
        gateway.close(context)
    return dict(
        seconds=time.monotonic() - started,
        scratch_peak_bytes=correction.metrics.scratch_peak_bytes,
        generation_before=before,
        generation_after=after,
        old_context_terminal="DATA_CHANGED",
        corrected_read_verified=True,
        expected_first=expected,
        phases=dict(phases),
    )


def worker_hot_correction(
    store, provider, repository, process, limits, results, config
):
    account, revision = catalogue(repository.engine, HOT_PROBE)
    services = MeasuredServices(provider.binding, results)
    service = RunService(repository, provider, limits=limits)
    probe_config = dict(
        config, account_id=str(account), strategy_revision_id=str(revision)
    )
    probe = service.submit(OWNER, probe_config, uuid4().hex)["run_id"]
    phases = {}
    correction = _CorrectionProbe(lambda committed, stopped: hot_correction(
        store, provider, committed=committed, stopped=stopped, phases=phases))

    def change(_supervisor):
        return correction.poll(ready=services.probe_reached.is_set(),
                               terminal=repository.status(OWNER, probe)["status"] in TERMINAL)

    try:
        measured = run_group(repository, process, services, provider, [probe], hook=change)
    finally:
        correction.close()
        print(json.dumps(dict(stage="hot_correction_probe", run_id=probe,
                              phases=phases, gateway=gateway_measurements(provider, [probe]),
                              **correction.measurement())), flush=True)
    state = repository.status(OWNER, probe)
    assert services.probe_reached.is_set() and correction.result is not None
    assert correction.committed.is_set()
    assert (
        state["status"] == "failed" and state["error"]["code"] == "DATA_CHANGED"
    ), state
    partial = read_result(repository, OWNER, probe, "summary", 100, None, CURSOR)
    assert partial["partial"]
    actual = provider.telemetry[probe]
    assert actual["python_callbacks"]["by_kind"]["handle_tick"]["count"] == 100, actual
    with store.catalog.transaction() as c:
        rows, size = c.execute(
            text(
                "SELECT sum(row_count),sum(byte_count) FROM data_store_files WHERE dataset=:d"
            ),
            {"d": provider.dataset.name},
        ).one()
    assert rows == provider.load.rows
    return dict(
        correction.result,
        probe_run_id=probe,
        probe_status=state["status"],
        probe_error=state["error"]["code"],
        probe_python_callbacks=100,
        probe_run=measured,
        source_rows=rows,
        current_source_bytes=size,
        **correction.measurement(),
    )


def validate(repository, provider, run_ids, telemetry, load, frequency):
    results = []
    for run_id in run_ids:
        status = repository.status(OWNER, run_id)
        if status["status"] != "succeeded":
            print(
                json.dumps(
                    dict(
                        stage="failed_result",
                        run_id=run_id,
                        gateway=gateway_measurements(provider, [run_id]),
                        logs=read_result(
                            repository, OWNER, run_id, "logs", 10, None, CURSOR
                        ),
                    ),
                    default=str,
                ),
                flush=True,
            )
        assert status["status"] == "succeeded", status
        report = telemetry[run_id]
        progress = report["engine"]["progress"]
        expected = load.days * load.securities * 4 if frequency == "60m" else load.rows
        assert progress["completed_events"] == expected, (progress, expected)
        assert progress["completed_sessions"] == load.days, progress
        assert (
            status["progress_completed"] == expected and status["progress"] == progress
        ), status
        if frequency == "60m":
            gateway = provider.gateways[run_id]
            assert (
                gateway.replay_raw_rows == load.rows
                and gateway.replay_hour_rows == expected
            )
        expected_callbacks = load.ticks or load.days * (
            4 if frequency == "60m" else load.bars
        )
        assert (
            report["python_callbacks"]["by_kind"][
                "handle_tick" if load.ticks else "handle_data"
            ]["count"]
            == expected_callbacks
        ), report
        summary = read_result(repository, OWNER, run_id, "summary", 100, None, CURSOR)
        counts = {}
        last_record = None
        cash = Decimal(status["normalized_config"]["initial_cash"])
        fees = Decimal(0)
        positions = {}
        last_equity = None
        for kind in ("equity", "orders", "trades", "positions", "records", "logs"):
            cursor = None
            count = 0
            while True:
                page = read_result(repository, OWNER, run_id, kind, 100, cursor, CURSOR)
                count += len(page["items"])
                if kind == "trades":
                    for row in page["items"]:
                        value = Decimal(row["price"]) * int(row["quantity"])
                        fee = Decimal(row["fee"])
                        cash += (-value if row["side"] == "buy" else value) - fee
                        fees += fee
                if kind == "positions":
                    for row in page["items"]:
                        positions[row["security"]] = row
                if kind == "equity" and page["items"]:
                    last_equity = page["items"][-1]
                if kind == "records" and page["items"]:
                    last_record = page["items"][-1]
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            counts[kind] = count
        assert counts["equity"] == load.days + 1, counts
        assert (
            counts["trades"] > 0 and counts["orders"] > 0 and counts["records"] > 0
        ), counts
        assert (
            int(last_record["values"]["callbacks"]["value"]) == expected_callbacks
        ), last_record
        assert fees > 0 and last_equity is not None, (fees, last_equity)
        portfolio = report["portfolio"]
        assert cash == Decimal(portfolio["cash"]), (cash, portfolio)
        assert cash == Decimal(portfolio["available_cash"]) + Decimal(
            portfolio["frozen_cash"]
        )
        market = sum(
            (Decimal(row["market_value"]) for row in positions.values()), Decimal(0)
        )
        assert cash + market + Decimal(portfolio["receivables"]) == Decimal(
            last_equity["equity"]
        )
        assert cash >= 0
        with repository.engine.connect() as c:
            pointer = c.execute(
                text("SELECT directory FROM s3_run_results WHERE run_id=:i"),
                {"i": run_id},
            ).scalar_one()
        directory = Path(repository.result_root) / pointer
        size = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
        results.append(
            dict(
                run_id=run_id,
                counts=counts,
                result_bytes=size,
                summary=summary,
                telemetry=report,
                gateway=dict(
                    provider.gateways[run_id].stats, **provider.gateways[run_id].elapsed
                ),
                source_rows=load.rows,
                completed_events=expected,
                independent_accounting=dict(
                    cash=str(cash), fees=str(fees), cash_conserved=True
                ),
                order_limit_rejections=(int(last_record['values']['order_limit_rejections']['value'])
                    if load.name in ('B1','B4') else None),
            )
        )
    return results


def _main():
    smoke = os.environ.get("QF_S3_FINAL_SMOKE") == "1"
    selection = os.environ.get("QF_S3_FINAL_CASES")
    selected = set(selection.split(",")) if selection else None
    assert selected is None or selected <= set(x.name for x in FULL)
    frequency_selection = os.environ.get('QF_S3_FINAL_FREQUENCIES')
    selected_frequencies = set(frequency_selection.split(',')) if frequency_selection else None
    assert selected_frequencies is None or selected_frequencies <= {'1d', '1m', '60m', 'tick'}
    def frequencies(load):
        declared = ['1m', '60m'] if load.bars == 240 else ['tick'] if load.ticks else ['1d']
        return [f for f in declared if selected_frequencies is None or f in selected_frequencies]
    loads = [
        x for x in (SMOKE if smoke else FULL) if (selected is None or x.name in selected) and frequencies(x)
    ]
    assert loads, 'the declared workload/frequency selection must not be empty'
    evidence = dict(
        production_executed=False,
        full_B1_B4=False,
        mode="smoke" if smoke else "full",
        selected_cases=[x.name for x in loads],
        selected_frequencies=None if selected_frequencies is None else sorted(selected_frequencies),
        passed=False,
        architecture=platform.machine(),
        execution=dict(ci_run_url=os.environ.get("QF_S3_CI_RUN_URL")),
        topology="runner_container_to_sibling_workers",
        resource_budget=dict(cpu=4, memory_bytes=8 * 1024**3),
        hardware=dict(kernel=platform.release(), logical_cpus=os.cpu_count()),
        source=json.loads(Path("/app/s3-source.json").read_text()),
        cases=[],
        cache="warm after fixture construction; OS cache shared, no per-run market copy",
        old_engine_comparison="unavailable: final tree physically lacks legacy executable",
        status={},
    )
    if not smoke:
        assert evidence["source"].get("measurement_worktree_dirty") is False, (
            "full measurement requires a clean, identified current checkout"
        )
    emit(evidence)
    settings = get_settings()
    assert settings.environment == "test" and "test" in settings.database_name
    daemon = DockerDaemon()
    process = MeasuredProcess(
        daemon,
        WorkRoot(Path("/qf-work"), os.environ["QF_TEST_DAEMON_ROOT"]),
        os.environ["QF_TEST_WORKER_IMAGE"],
    )
    engine = create_engine(settings.database_url)
    environment = dict(os.environ, QF_DATABASE_NAME=settings.database_name)
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "/app/alembic.ini", "upgrade", "head"],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert migration.returncode == 0, migration.stderr
    with engine.begin() as c:
        if not c.execute(
            text("SELECT EXISTS(SELECT 1 FROM data_store_legacy_maintenance)")
        ).scalar_one():
            c.execute(
                maintenance.insert().values(
                    singleton=1, phase="ready", completed_json=[], files_started=False
                )
            )
    root = ROOT / "store"
    root.mkdir(parents=True)
    if smoke and os.environ.get("LF_D01_ALLOW_OVERLAY_TEST") == "1":
        from unittest.mock import patch

        patch("app.data_store.locking._filesystem_name", return_value="ext4").start()
        evidence["filesystem_probe_substitute"] = True
    telemetry = {}
    results = ROOT / "results"
    results.mkdir()
    budget = GroupBudget(
        reserve_cpu_millis=2000,
        reserve_memory_bytes=3 * 1024**3,
        reserve_pids=96,
        tmp_bytes=1024**3,
    )
    repository = DatabaseRunRepository(engine, budget=budget, result_root=results)
    limits = WorkerLimits(
        cpu_millis=2000,
        memory_bytes=5 * 1024**3,
        pids=64,
        tmp_bytes=512 * 1024**2,
        file_bytes=256 * 1024**2,
        result_bytes=128 * 1024**2,
        wall_seconds=3600,
    )
    try:
        with CurrentStore(
            engine,
            root,
            cursor_key=CURSOR.encode(),
            initialize=True,
            limits=StoreLimits(duckdb_threads=1),
        ) as store:
            from app.data_store.locking import _filesystem_name

            evidence["hardware"]["filesystem"] = _filesystem_name(store.files.fd)
            evidence["hardware"]["free_bytes_before_fixtures"] = (
                __import__("shutil").disk_usage(root).free
            )
            for load in loads:
                construction = prepare(store, load)
                for frequency in frequencies(load):
                    provider = Provider(store, load, frequency, process, telemetry)
                    services = MeasuredServices(provider.binding, results)
                    use_limits = (
                        limits
                        if load.name != "B4"
                        else WorkerLimits(
                            **dict(
                                vars(limits),
                                cpu_millis=1000,
                                memory_bytes=5 * 1024**3 // 2,
                                tmp_bytes=256 * 1024**2,
                                file_bytes=128 * 1024**2,
                                result_bytes=128 * 1024**2,
                            )
                        )
                    )
                    service = RunService(repository, provider, limits=use_limits)
                    account, revision = catalogue(
                        engine,
                        (
                            TICKS
                            if load.ticks
                            else INTRADAY if load.bars == 240 else MOMENTUM
                        ),
                    )
                    config = deepcopy(provider.spec["config"])
                    config.update(
                        account_id=str(account),
                        strategy_revision_id=str(revision),
                        frequency=frequency,
                        execution_model=(
                            "quote_tick_v1"
                            if load.kind == "quote_tick"
                            else (
                                "trade_tick_v1"
                                if load.ticks
                                else "bar_next_interval_v1"
                            )
                        ),
                        parameters={"securities": load.universe, "window": 20},
                    )
                    if load.name == "B1":
                        config["benchmark"] = load.universe[0]
                        provider.spec["benchmark_origin"] = dict(
                            time_ns=provider.spec["calendar"][0]["before_open_ns"],
                            value="10",
                            unavailable_reason=None,
                        )
                    config = normalize(config)
                    correction = None
                    if load.ticks:
                        correction = worker_hot_correction(
                            store,
                            provider,
                            repository,
                            process,
                            use_limits,
                            results,
                            config,
                        )
                        config["parameters"]["expected_first"] = correction[
                            "expected_first"
                        ]
                    case = dict(
                        load=asdict(load),
                        frequency=frequency,
                        fixture=construction,
                        passed=False,
                        hot_correction=correction,
                    )
                    evidence["cases"].append(case)
                    emit(evidence)
                    print(
                        json.dumps(
                            dict(
                                stage="measurement_start",
                                load=load.name,
                                frequency=frequency,
                            )
                        ),
                        flush=True,
                    )
                    measured_started = time.monotonic()
                    try:
                        if load.name == "B4":
                            configs = [
                                dict(
                                    config,
                                    parameters=dict(
                                        config["parameters"], window=5 + i, variant=i
                                    ),
                                )
                                for i in range(16)
                            ]
                            bid = service.batch(OWNER, configs, uuid4().hex)
                            case["admission_seconds"] = (
                                time.monotonic() - measured_started
                            )
                            runs, _ = repository.batch_rows(OWNER, bid, limit=16)
                            ids = [r["run_id"] for r in runs]
                            case["first"] = run_group(
                                repository,
                                process,
                                services,
                                provider,
                                ids,
                                fault=ids[7],
                            )
                            failures = [
                                s
                                for s in case["first"]["states"]
                                if s["status"] != "succeeded"
                            ]
                            assert (
                                len(failures) == 1 and failures[0]["run_id"] == ids[7]
                            ), failures
                            successful = [i for i in ids if i != ids[7]]
                            original = [
                                read_result(
                                    repository, OWNER, i, "summary", 100, None, CURSOR
                                )
                                for i in successful
                            ]
                            retries = repository.retry_failed(OWNER, bid, uuid4().hex)
                            assert len(retries) == 1, retries
                            case["retry"] = run_group(
                                repository, process, services, provider, retries
                            )
                            assert original == [
                                read_result(
                                    repository, OWNER, i, "summary", 100, None, CURSOR
                                )
                                for i in successful
                            ]
                            final_ids = successful + retries
                            case["results"] = validate(
                                repository,
                                provider,
                                final_ids,
                                telemetry,
                                load,
                                frequency,
                            )
                            case["comparison"] = compare_runs(
                                repository, OWNER, final_ids, CURSOR
                            )
                        else:
                            run = service.submit(OWNER, config, uuid4().hex)
                            case["admission_seconds"] = (
                                time.monotonic() - measured_started
                            )
                            ids = [run["run_id"]]
                            case["run"] = run_group(
                                repository, process, services, provider, ids
                            )
                            case["results"] = validate(
                                repository, provider, ids, telemetry, load, frequency
                            )
                        case["passed"] = True
                    except Exception as error:
                        case["failure"] = dict(
                            type=type(error).__name__,
                            message=str(error)[:8192],
                            traceback=traceback.format_exc()[-16384:],
                        )
                    emit(evidence)
                    case["wall_with_admission_and_acceptance_reads_seconds"] = (
                        time.monotonic() - measured_started
                    )
                    print(
                        json.dumps(
                            dict(
                                stage="measurement_end",
                                load=load.name,
                                frequency=frequency,
                                passed=case["passed"],
                                failure=case.get("failure"),
                                fixture=case["fixture"],
                                wall_with_admission_and_acceptance_reads_seconds=case[
                                    "wall_with_admission_and_acceptance_reads_seconds"
                                ],
                                groups={
                                    name: {
                                        key: value
                                        for key, value in case[name].items()
                                        if key != "states"
                                    }
                                    for name in ("run", "first", "retry")
                                    if name in case
                                },
                                results=[
                                    {
                                        key: value
                                        for key, value in result.items()
                                        if key != "telemetry"
                                    }
                                    | {
                                        "engine_timing": result["telemetry"]["engine"][
                                            "timing"
                                        ],
                                        "python_callbacks": result["telemetry"][
                                            "python_callbacks"
                                        ],
                                    }
                                    for result in case.get("results", [])
                                ],
                            ),
                            default=str,
                        ),
                        flush=True,
                    )
            passed = len(evidence["cases"]) == sum(
                len(frequencies(x)) for x in loads
            ) and all(c["passed"] for c in evidence["cases"])
            evidence.update(
                passed=passed, full_B1_B4=passed and not smoke and selected is None and selected_frequencies is None
            )
            if not passed:
                raise AssertionError(
                    "full declared loads were not all completed correctly"
                )
    except BaseException as error:
        evidence["failure"] = dict(
            type=type(error).__name__,
            message=str(error)[:8192],
            traceback=traceback.format_exc()[-16384:],
        )
        emit(evidence)
        raise
    finally:
        engine.dispose()
        emit(evidence)
    print(
        json.dumps(
            dict(
                passed=evidence["passed"],
                full_B1_B4=evidence["full_B1_B4"],
                output=str(ROOT / "result.json"),
            )
        ),
        flush=True,
    )


def main():
    try:
        _main()
    except BaseException as error:
        path = ROOT / "result.json"
        evidence = (
            json.loads(path.read_text())
            if path.exists()
            else dict(passed=False, full_B1_B4=False)
        )
        evidence["failure"] = dict(
            type=type(error).__name__,
            message=str(error)[:8192],
            traceback=traceback.format_exc()[-16384:],
        )
        emit(evidence)
        raise
