"""D15 private HTTP/PG/CurrentStore -> restricted Docker worker browser target.

Fresh test DB and explicit synthetic facts; test controls never ship in the API.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from threading import Event, Thread
from uuid import uuid4
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "backend"), str(ROOT / "sdk/python/tests")]
import uvicorn
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from fastapi import Depends
from app.core.auth import require_api_token
from app.core.config import get_settings
from app.backtest_service.models import Run
from app.backtest_service.process import DockerDaemon
from app.backtest_service.runtime_services import create_services
from app.backtest_service.service import FormalProvider
from app.backtest_service.supervisor import RunnerSupervisor
from app.strategies.models import StrategyRevision
from s3_cli_suite import D14Tests, ExampleProvider, ENTRIES
from s3_runservice_suite import catalogue, OWNER, TOKEN

SOURCE = '''from quantfoundry.api import order, order_target_percent, log, record
import time

def initialize(context):
    context.state.sent = False
    if context.params.get("fail", False):
        raise RuntimeError("Explicit synthetic parameter failure")
    time.sleep(context.params.get("delay", 0))

def handle_data(context, data):
    if not context.state.sent:
        # Empty initial holdings: this sale must be rejected independently of
        # the fixture's (deliberately synthetic) trading-lot convention.
        rejection = order("510300.SH", -1)
        if not rejection.accepted:
            log.warning(f"Rejected order: {rejection.reason_code}")
        result = order_target_percent("510300.SH", "0.6")
        if not result.accepted:
            log.warning(f"Rejected target: {result.reason_code}")
        context.state.sent = True
    record(available_cash=context.portfolio.available_cash)
'''


def main():
    settings = get_settings()
    if settings.environment != "test" or not settings.database_name.endswith("test"):
        raise RuntimeError("D15 requires explicit private test configuration")
    os.environ["PYTHONPATH"] = str(ROOT / "backend")
    image = os.environ["QF_D15_WORKER_IMAGE"]
    if not image.startswith("sha256:"):
        raise RuntimeError("Prebuilt immutable worker image required")
    # Same private test image/UID setup as D12/D14. D12's immutable files and
    # IPC directory rely on the normal runner umask; the cloud shell uses 077.
    os.umask(0o022)
    base = Path(tempfile.mkdtemp(prefix="q15-", dir=os.environ.get("QF_D15_ROOT")))
    # Restricted UID must traverse the private host root to its two run mounts.
    # Only each run's code/IPC is mounted; fixtures and DB credentials stay out.
    base.chmod(0o755)
    os.environ["QF_D13_TEST_ROOT"] = str(base / "service")
    D14Tests.setUpClass()
    fixture = D14Tests(methodName="test_documented_migration_creates_new_strategy_revision_preserves_old_values")
    fixture.setUp()
    if os.environ.get("QF_D15_BIND_PORT"):
        fixture.server.should_exit = True
        fixture.thread.join(5)
        fixture.server = uvicorn.Server(uvicorn.Config(fixture.app, host="0.0.0.0",
            port=int(os.environ["QF_D15_BIND_PORT"]), lifespan="off", log_level="error", access_log=False))
        fixture.thread = Thread(target=fixture.server.run, daemon=True)
        fixture.thread.start()
        for _ in range(250):
            if fixture.server.started: break
            Event().wait(.02)
        if not fixture.server.started: raise RuntimeError("Private HTTP port did not start")
    stop = Event()
    try:
        account, revision = catalogue(fixture.engine, SOURCE)
        with Session(fixture.engine) as session:
            strategy_id = str(session.get(StrategyRevision, revision).strategy_id)
            legacy_strategy_id = str(session.get(StrategyRevision, fixture.old_revision).strategy_id)
        entry = deepcopy(ENTRIES[0])
        entry["config"].update(account_id=str(account), strategy_revision_id=str(revision), parameters={"fail": False, "delay": 0})
        provider = ExampleProvider(fixture.engine, fixture.store, entry)
        original_capabilities = provider.capabilities
        provider.capabilities = lambda: [dict(original_capabilities()[0], frequency="1d"),
            dict(binding="synthetic-minute-absent", frequency="1m", available=False),
            dict(binding="synthetic-tick-absent", frequency="tick", available=False)]
        fixture.app.state.s3_provider = provider
        work = base / "work"; work.mkdir()
        host_work = (Path(os.environ["QF_TEST_DAEMON_ROOT"]) / work.relative_to(Path("/qf-work"))) if os.environ.get("QF_TEST_DAEMON_ROOT") else work
        runtime_settings = fixture.settings.model_copy(update=dict(s3_work_root=work, s3_daemon_work_root=host_work, s3_worker_image=image))
        repository, process, services = create_services(settings=runtime_settings, engine=fixture.engine, provider=provider, daemon=DockerDaemon())
        provider.process = process
        supervisor = RunnerSupervisor(repository, process, services, lease_seconds=10)
        thread = Thread(target=lambda: supervisor.run_forever(stop_event=stop), daemon=True); thread.start()
        controls = {}
        @fixture.app.post("/api/test/d15/control", dependencies=[Depends(require_api_token)])
        def control(payload: dict):
            mode = payload.get("mode")
            if mode == "formal": fixture.app.state.s3_provider = FormalProvider(fixture.settings)
            elif mode == "daily": fixture.app.state.s3_provider = provider
            elif mode in ("restrict", "clear"):
                with fixture.engine.begin() as c:
                    c.execute(text("DELETE FROM data_store_issues WHERE dataset=:dataset AND issue_key='d15-quality'"), {"dataset":provider.market.spec.name})
                    if mode == "restrict":
                        c.execute(text("INSERT INTO data_store_issues(dataset,issue_key,scope_key,reason,evidence_token,target_json,resolution_json) VALUES(:dataset,'d15-quality','test','QUALITY_CHANGED',:token,'{}','{}')"), {"dataset":provider.market.spec.name,"token":uuid4().hex})
            else: raise RuntimeError("Unknown isolated control")
            controls[mode] = controls.get(mode, 0) + 1
            return {"synthetic": True, "mode": mode}
        @fixture.app.get("/api/test/d15/evidence", dependencies=[Depends(require_api_token)])
        def evidence():
            with fixture.engine.connect() as c: rows = c.execute(select(Run).where(Run.owner == OWNER)).mappings().all()
            return dict(production_executed=False, topology="browser_http_to_runner_container_to_restricted_sibling_worker" if os.environ.get("QF_TEST_DAEMON_ROOT") else "browser_host_http_to_host_supervisor_to_restricted_sibling_worker",
                current_store_real=True, database=fixture.dbname, controls=controls,
                started_runs=list(supervisor.started_runs), peak_workers=supervisor.peak_workers,
                runs=[dict(run_id=str(r["id"]),status=r["status"],batch_id=str(r["batch_id"]) if r["batch_id"] else None,
                    retry_of=str(r["retry_of"]) if r["retry_of"] else None,outcome=r["outcome"],config=r["config"]) for r in rows],
                legacy_run_id=str(fixture.old_run),worker_image=image)
        manifest = dict(url=fixture.url,token=TOKEN,strategy_id=strategy_id,account_id=str(account),revision_id=str(revision),legacy_strategy_id=legacy_strategy_id,legacy_revision_id=str(fixture.old_revision),legacy_account_id=str(fixture.old_account),legacy_run_id=str(fixture.old_run),config=entry["config"],production_executed=False)
        path = Path(os.environ["QF_D15_MANIFEST"]);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(manifest,ensure_ascii=False));os.chmod(path,0o600)
        print(json.dumps(dict(ready=True,url=fixture.url,manifest=str(path),production_executed=False)),flush=True)
        signal.signal(signal.SIGTERM,lambda *_:stop.set());signal.signal(signal.SIGINT,lambda *_:stop.set())
        while not stop.wait(.1):
            if not thread.is_alive(): raise RuntimeError("Isolated supervisor stopped unexpectedly")
        thread.join(10)
        if thread.is_alive(): raise RuntimeError("Supervisor failed to stop")
    finally:
        stop.set(); fixture.tearDown(); D14Tests.tearDownClass()

if __name__ == "__main__": main()
