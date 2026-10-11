"""D12 atomic scheduling/resource contract. Required with the S3 wheel installed."""

import multiprocessing
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

try:
    import quantfoundry

    assert quantfoundry.__name__ == "quantfoundry"
except ImportError:
    import os

    if os.getenv("QF_S3_INTEGRATION_REQUIRED") == "1":
        raise
    pytest.skip("S3 installed-wheel CI runs this module", allow_module_level=True)

from app.backtest_service.local_queue import LocalRunRepository
from app.backtest_service.runtime_contracts import (
    AcceptedWork,
    GroupBudget,
    RuntimeFault,
    WorkerLimits,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk/python/tests"))
from support_sdk import spec


def work(parameters=None, limits=None):
    source = spec(days=1, securities=("A.SH",))
    source["config"]["parameters"] = parameters or {}
    return AcceptedWork(
        "owner",
        source["config"],
        "def initialize(c): pass",
        limits or WorkerLimits(),
        source,
    )


def claim_from_process(path, start, out):
    repository = LocalRunRepository(path)
    start.wait(5)
    out.put(repository.claim("independent-process", time.time(), 5))


def test_two_real_processes_only_one_claim_and_expired_claim_is_fenced(tmp_path):
    repository = LocalRunRepository(tmp_path)
    _, ids = repository.enqueue_batch([work()])
    processes = multiprocessing.get_context("spawn")
    start = processes.Event()
    out = processes.Queue()
    children = [
        processes.Process(target=claim_from_process, args=(tmp_path, start, out))
        for _ in range(2)
    ]
    for child in children:
        child.start()
    start.set()
    claims = [out.get(timeout=10) for _ in children]
    for child in children:
        child.join(5)
        assert child.exitcode == 0
    assert sum(c is not None for c in claims) == 1
    claim = next(c for c in claims if c is not None)
    assert repository.running(claim)
    assert repository.heartbeat(claim, time.time(), 5)
    assert repository.expired(time.time()) == []
    assert not repository.recover_stopped(claim, time.time())
    with repository._locked():
        row = repository._row(ids[0])
        row["lease_until"] = time.time() - 1
        repository._write(ids[0] + ".json", row)
    assert not repository.heartbeat(claim, time.time(), 5)
    assert not repository.commit_outcome(
        claim, dict(status="succeeded", result_id=ids[0]), "forged"
    )
    assert repository.recover_stopped(claim, time.time())
    next_claim = repository.claim("new", time.time(), 5)
    assert next_claim.fence == claim.fence + 1 and next_claim.token != claim.token
    assert not repository.commit_outcome(
        claim, dict(status="failed", partial=True), "late"
    )


def test_group_budget_and_batch_immutability(tmp_path):
    budget = GroupBudget(concurrency=2, cpu_millis=2000, reserve_cpu_millis=1000)
    repository = LocalRunRepository(tmp_path, budget)
    _, ids = repository.enqueue_batch([work({"i": i}) for i in range(16)])
    claim = repository.claim("a", time.time(), 5)
    assert claim is not None
    assert repository.claim("b", time.time(), 5) is None  # CPU before concurrency
    with pytest.raises(RuntimeFault):
        repository.enqueue_batch([work(limits=WorkerLimits(cpu_millis=2000))])
    with pytest.raises(RuntimeFault):
        repository.enqueue_batch(
            [work(), replace(work(), config=dict(work().config, initial_cash="2"))]
        )
    with pytest.raises(RuntimeFault):
        repository.status("other", ids[0])
    with pytest.raises(RuntimeFault):
        repository.request_cancel("other", ids[0])


def test_atomic_success_cancel_and_retention_failed_only_retry(tmp_path):
    repository = LocalRunRepository(tmp_path)
    batch, ids = repository.enqueue_batch([work({"i": i}) for i in range(3)])
    first = repository.claim("a", time.time(), 5)
    published = []
    assert repository.commit_outcome(
        first,
        dict(status="succeeded", result_id=first.run_id),
        "retained",
        publish=lambda: published.append(1),
    )
    assert published == [1]
    assert not repository.commit_outcome(
        first, dict(status="failed", partial=True), None
    )
    assert repository.request_cancel("owner", first.run_id)["status"] == "succeeded"
    second = repository.claim("a", time.time(), 5)
    repository.request_cancel("owner", second.run_id)
    assert not repository.commit_outcome(
        second,
        dict(status="succeeded", result_id=second.run_id),
        "bad",
        publish=lambda: published.append(2),
    )
    assert published == [1]
    assert repository.commit_outcome(
        second, dict(status="cancelled", partial=True), None
    )
    third = repository.claim("a", time.time(), 5)
    assert repository.commit_outcome(third, dict(status="failed", partial=True), None)
    retry = repository.retry_failed("owner", batch)
    assert len(retry) == 1
    assert repository.status("owner", first.run_id)["result"] == "retained"
    assert repository.status("owner", retry[0])["retry_of"] == third.run_id
    assert repository.batch_status("owner", batch, limit=1)["next_offset"] == 1


def test_symlink_hardlink_state_and_paths_refused(tmp_path):
    from app.backtest_service.process import WorkRoot

    real = tmp_path / "real"
    real.mkdir()
    repository = LocalRunRepository(real)
    _, ids = repository.enqueue_batch([work()])
    path = real / (ids[0] + ".json")
    link = real / "hard"
    link.hardlink_to(path)
    with pytest.raises(RuntimeFault):
        repository.status("owner", ids[0])
    link.unlink()
    root = WorkRoot(real, "/private/host")
    for value in ("../../etc", "/etc", "x/code/extra"):
        with pytest.raises(RuntimeFault):
            root.host_path(value)
    root_path = real / "run"
    root_path.symlink_to("/tmp")
    with pytest.raises(RuntimeFault):
        root.host_path("run/code")


def test_runner_entry_has_no_legacy_import_and_production_composition_fails_closed():
    from app.backtest_service.runtime_services import create_services
    from app.runner.__main__ import build_supervisor

    assert callable(build_supervisor)
    with pytest.raises(RuntimeFault) as fault:
        create_services()
    assert fault.value.code == "CAPABILITY_UNAVAILABLE"


def test_batch_rejections_do_not_partially_enqueue_or_retry(tmp_path):
    repository = LocalRunRepository(tmp_path, queue_limit=2)
    oversized = work()
    oversized.specification["test_payload"] = "x" * (1024 * 1024)
    with pytest.raises(RuntimeFault):
        repository.enqueue_batch([work(), oversized])
    assert list(tmp_path.glob("*.json")) == [tmp_path / "policy.json"]
    batch, ids = repository.enqueue_batch([work({"i": i}) for i in range(2)])
    for _ in ids:
        claim = repository.claim("a", time.time(), 5)
        assert repository.commit_outcome(
            claim, dict(status="failed", partial=True), None
        )
    repository.enqueue_batch([work()])
    files = set(tmp_path.iterdir())
    with pytest.raises(RuntimeFault):
        repository.retry_failed("owner", batch)
    assert set(tmp_path.iterdir()) == files
    assert [
        r["run_id"] for r in repository.batch_status("owner", batch)["items"]
    ] == ids
