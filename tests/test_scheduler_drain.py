"""Fictional operations only: no Docker, network, database or real task IDs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from scripts.scheduler_drain import (
    CONTAINER_CODE, DockerClient, DrainError, Episode, StateFile,
    digest, main, validate_deployment,
)


def task(key, state="active", version=1):
    return {"id": key, "task_type": "fixture.collect", "state": state, "version": version,
            "schedule": {"type": "cron", "expression": "*/10 * * * *", "timezone": "UTC"},
            "parameters_hash": "fixture-parameters", "parameter_version": 1, "registered_here": True}


COMMAND = ["docker", "compose", "-p", "fixture-project", "-f", "fixture.yaml",
           "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait",
           "--wait-timeout", "120", "backend", "runner"]


class Clock:
    def __init__(self): self.seconds = 0
    def now(self): return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)
    def monotonic(self): return self.seconds
    def sleep(self, seconds): self.seconds += seconds


class Client:
    def __init__(self):
        self.tasks = {t["id"]: t for t in (task("a"), task("b", version=4), task("p", "paused", 8), task("o", "completed", 9))}
        self.runs = {"run-a": {"id": "run-a", "task_id": "a", "task_version": 1,
                     "task_type": "fixture.collect", "status": "queued", "trigger_type": "scheduled",
                     "parameter_version": 1, "priority": 0, "parameters_hash": "frozen-run",
                     "created_at": "2025-12-31T23:50:00+00:00"}}
        self.calls = []
        self.finish = True
        self.new_count = 0
        self.after_change = lambda *_: None
        self.on_read = lambda *_: None
        self.before_task = lambda *_: None
        self.project_name = "fixture-project"
        self.application_services = [{"service": "backend", "id": "fixture-backend", "image": "fixture-old"}, {"service": "runner", "id": "fixture-runner", "image": "fixture-old"}]
        self.preservation = {"held_locks": [], "backtests": 0, "ingestion_leases": 0, "files": "fixture-v2", "spools": "fixture-seal"}

    def project(self): return self.project_name
    def services(self): return deepcopy(self.application_services)
    def snapshot(self):
        return deepcopy({"tasks": list(self.tasks.values()), "runs": [r for r in self.runs.values() if r["status"] in ("queued", "running")]})
    def task(self, key):
        self.before_task(self, key)
        return deepcopy(self.tasks.get(key))
    def read_runs(self, ids):
        self.on_read(self)
        return deepcopy([self.runs[i] for i in ids if i in self.runs])
    def new_runs(self, _): return self.new_count
    def fence(self): return deepcopy(self.preservation)
    def change(self, key, version, target):
        self.calls.append((key, version, target))
        current = self.tasks[key]
        if current["version"] != version or current["state"] != ("active" if target == "paused" else "paused"):
            raise DrainError("http_409")
        current.update(state=target, version=version + 1)
        if target == "paused" and self.finish:
            for run in self.runs.values(): run["status"] = "succeeded"
        self.after_change(self, key, target)


class DrainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name).resolve() / "state.json"
        self.journal = StateFile(self.path)
        self.lock = self.journal.locked()
        self.lock.__enter__()
        self.clock, self.client = Clock(), Client()
        self.episode = Episode(self.client, self.journal, now=self.clock.now,
                               monotonic=self.clock.monotonic, sleep=self.clock.sleep)

    def tearDown(self):
        self.lock.__exit__(None, None, None)
        self.directory.cleanup()

    def plan(self, command=()): return self.episode.plan(expected_count=2, seconds=900, command=command)
    def run_episode(self, value, execute=None): return self.episode.run(value, value["plan_sha256"], interval=300, execute=execute)

    def test_success_retains_accepted_rows_and_originally_paused_completed_tasks(self):
        original = deepcopy(self.client.runs)
        value = self.plan(COMMAND)
        calls = []
        self.run_episode(value, lambda argv, timeout: calls.append((argv, timeout)))
        self.assertEqual(calls[0][0], COMMAND)
        self.assertTrue(value["deployment_completed"])
        self.assertEqual(self.client.tasks["a"]["version"], 3)
        self.assertEqual(self.client.tasks["b"]["version"], 6)
        self.assertEqual(self.client.tasks["p"], task("p", "paused", 8))
        self.assertEqual(self.client.tasks["o"], task("o", "completed", 9))
        self.assertEqual(set(self.client.runs), set(original))
        for field in ("parameters_hash", "task_version", "parameter_version", "priority"):
            self.assertEqual(self.client.runs["run-a"][field], original["run-a"][field])
        self.assertEqual(value["deadline"], "2026-01-01T00:15:00+00:00")
        self.assertEqual(value["phase"], "restored")

    def test_active_set_or_version_drift_prevents_every_pause(self):
        for change in (lambda c: c.tasks.update(extra=task("extra")), lambda c: c.tasks["b"].update(version=5)):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                journal = StateFile(Path(d).resolve() / "state.json")
                client = Client()
                with journal.locked():
                    episode = Episode(client, journal)
                    value = episode.plan(expected_count=2)
                    change(client)
                    with self.assertRaisesRegex(DrainError, "active_scope_or_version_changed"):
                        episode.run(value, value["plan_sha256"])
                    self.assertEqual(client.calls, [])

    def test_once_tasks_and_unregistered_pending_runs_refuse_plan(self):
        self.client.tasks["a"]["schedule"] = {"type": "once", "run_at": "2026-01-01T01:00:00Z"}
        with self.assertRaisesRegex(DrainError, "only_recurring_cron"):
            self.plan()
        self.client.tasks["a"] = task("a")
        self.client.runs["run-a"]["task_id"] = "unknown"
        with self.assertRaisesRegex(DrainError, "unregistered_pending_work"):
            self.plan()
        self.assertEqual(self.client.calls, [])

    def test_partial_pause_failure_restores_only_acknowledged_tasks_and_keeps_queue(self):
        self.client.finish = False
        def read(client, key):
            if key == "b": client.tasks[key].update(state="paused", version=5)
        self.client.before_task = read
        value = self.plan()
        with self.assertRaisesRegex(DrainError, "task_changed_before_pause"):
            self.run_episode(value)
        self.assertEqual(self.client.tasks["a"]["state"], "active")
        self.assertEqual(self.client.tasks["b"]["state"], "paused")
        self.assertFalse(any(c[0] == "b" for c in self.client.calls))
        self.assertEqual(self.client.runs["run-a"]["status"], "queued")

    def test_concurrent_edit_after_our_pause_is_never_overwritten(self):
        def changed(client, key, target):
            if key == "a" and target == "paused":
                client.tasks[key].update(version=3, parameters_hash="user-edited")
        self.client.after_change = changed
        value = self.plan()
        with self.assertRaisesRegex(DrainError, "paused_task_modified"):
            self.run_episode(value)
        self.assertEqual(self.client.tasks["a"]["parameters_hash"], "user-edited")
        self.assertEqual(self.client.tasks["a"]["state"], "paused")
        self.assertEqual(self.client.tasks["b"]["state"], "active")
        self.assertEqual(value["restore"]["a"]["outcome"], "concurrent_change_preserved")

    def test_unknown_pause_response_does_not_infer_ownership_from_paused_row(self):
        def unknown(client, key, target):
            if key == "b" and target == "paused": raise DrainError("lost_response", uncertain=True)
        self.client.after_change = unknown
        value = self.plan()
        with self.assertRaisesRegex(DrainError, "lost_response"):
            self.run_episode(value)
        self.assertEqual(self.client.tasks["a"]["state"], "active")
        self.assertEqual(self.client.tasks["b"]["state"], "paused")
        self.assertNotIn("b", value["confirmed"])
        self.assertEqual(value["unresolved"], [{"action": "pause", "id": "b", "version": 4}])
        self.assertEqual(value["phase"], "recovery_required")
        self.episode.restore(value)
        self.assertFalse(any(c[0] == "b" and c[2] == "active" for c in self.client.calls))

    def test_deadline_recovery_preserves_unfinished_queue_without_killing_workers(self):
        self.client.finish = False
        value = self.plan(COMMAND)
        deployed = []
        with self.assertRaisesRegex(DrainError, "episode_expired"):
            self.run_episode(value, lambda *_: deployed.append(True))
        self.assertEqual(deployed, [])
        self.assertLessEqual(self.clock.seconds, 900)
        self.assertEqual(self.client.runs["run-a"]["status"], "queued")
        self.assertTrue(all(self.client.tasks[k]["state"] == "active" for k in ("a", "b")))
        self.assertEqual(value["deadline"], "2026-01-01T00:15:00+00:00")

    def test_new_manual_run_aborts_switch_and_restores(self):
        self.client.new_count = 1
        value = self.plan(COMMAND)
        with self.assertRaisesRegex(DrainError, "new_run_after_pause"):
            self.run_episode(value, lambda *_: self.fail("must not deploy"))
        self.assertEqual(set(value["restore"]), {"a", "b"})

    def test_changed_or_deleted_accepted_record_blocks_switch(self):
        for mode in ("changed", "deleted"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                client = Client()
                journal = StateFile(Path(d).resolve() / "state.json")
                with journal.locked():
                    episode = Episode(client, journal)
                    value = episode.plan(expected_count=2)
                    def change(c):
                        if mode == "changed": c.runs["run-a"]["parameters_hash"] = "edited"
                        else: c.runs.clear()
                    client.on_read = change
                    with self.assertRaisesRegex(DrainError, "accepted_run_missing_or_modified"):
                        episode.run(value, value["plan_sha256"])

    def test_edit_during_pausing_does_not_replace_first_run_snapshot(self):
        value = self.plan()
        self.client.runs["run-a"]["priority"] = 99
        with self.assertRaisesRegex(DrainError, "accepted_run_missing_or_modified"):
            self.run_episode(value)

    def test_command_failure_and_preservation_mismatch_restore_owned_changes(self):
        value = self.plan(COMMAND)
        with self.assertRaisesRegex(DrainError, "deployment_failed"):
            self.run_episode(value, lambda *_: (_ for _ in ()).throw(DrainError("deployment_failed")))
        self.assertEqual(value["phase"], "restored")
        self.assertNotIn("deployment_completed", value)
        self.assertTrue(all(self.client.tasks[k]["state"] == "active" for k in ("a", "b")))

    def test_store_writer_blocks_command(self):
        value = self.plan(COMMAND)
        self.client.preservation["held_locks"] = ["fixture-writer"]
        with self.assertRaisesRegex(DrainError, "other_active_writers"):
            self.run_episode(value, lambda *_: self.fail("writer still active"))

    def test_live_download_lease_blocks_switch_after_scheduler_is_idle(self):
        value = self.plan(COMMAND)
        self.client.preservation["ingestion_leases"] = 1
        with self.assertRaisesRegex(DrainError, "other_active_writers"):
            self.run_episode(value, lambda *_: self.fail("lease still active"))
        self.assertTrue(all(self.client.tasks[k]["state"] == "active" for k in ("a", "b")))

    def test_service_change_before_or_during_pause_prevents_switch(self):
        value = self.plan(COMMAND)
        self.client.application_services[0]["image"] = "concurrent-deployment"
        with self.assertRaisesRegex(DrainError, "application_services_changed"):
            self.run_episode(value)
        self.assertEqual(self.client.calls, [])
        self.client.application_services[0]["image"] = "fixture-old"
        def changed(client, key, target):
            if key == "b" and target == "paused": client.application_services[1]["id"] = "replacement"
        self.client.after_change = changed
        with self.assertRaisesRegex(DrainError, "application_services_changed"):
            self.run_episode(value, lambda *_: self.fail("service changed"))
        self.assertTrue(all(self.client.tasks[k]["state"] == "active" for k in ("a", "b")))

    def test_expected_service_recreation_does_not_fail_post_command_guard(self):
        value = self.plan(COMMAND)
        def command(*_):
            self.client.application_services[0].update(id="new-backend", image="fixture-new")
            self.client.application_services[1].update(id="new-runner", image="fixture-new")
        self.run_episode(value, command)
        self.assertTrue(value["deployment_completed"])

    def test_start_event_contains_persisted_absolute_deadline_before_first_pause(self):
        value = self.plan()
        events = []
        def emit(event):
            self.assertEqual(self.client.calls, [])
            self.assertEqual(self.journal.load()["deadline"], event["deadline"])
            events.append(event)
        self.episode.emit = emit
        self.run_episode(value)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tasks"], 2)
        self.assertEqual(events[0]["deadline"], "2026-01-01T00:15:00+00:00")

    def test_interruption_recovers_without_extending_deadline(self):
        value = self.plan(COMMAND)
        with self.assertRaises(KeyboardInterrupt):
            self.run_episode(value, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(value["phase"], "restored")
        old_deadline = value["deadline"]
        self.clock.seconds = 10000
        self.episode.restore(value)
        self.assertEqual(value["deadline"], old_deadline)
        self.assertEqual(len([c for c in self.client.calls if c[2] == "active"]), 2)

    def test_failed_restore_read_does_not_skip_other_owned_tasks(self):
        value = self.plan()
        def read(client, key):
            if key == "b" and client.tasks["a"]["state"] == "paused": raise DrainError("read_unavailable")
        def changed(client, key, target):
            if key == "b" and target == "paused": client.before_task = read
        self.client.after_change = changed
        self.run_episode(value)
        self.assertEqual(self.client.tasks["a"]["state"], "active")
        self.assertEqual(value["phase"], "recovery_required")
        self.client.before_task = lambda *_: None
        self.episode.restore(value)
        self.assertEqual(self.client.tasks["b"]["state"], "active")

    def test_unknown_resume_response_retains_recovery_fence(self):
        def changed(client, key, target):
            if key == "a" and target == "active": raise DrainError("resume_unknown", uncertain=True)
        self.client.after_change = changed
        value = self.plan()
        self.run_episode(value)
        self.assertEqual(value["phase"], "recovery_required")
        self.assertEqual(value["unresolved"][0]["action"], "resume")
        self.assertEqual(self.client.tasks["a"]["state"], "active")

    def test_later_restore_never_repeats_an_uncertain_resume(self):
        value = self.plan()
        original_change = self.client.change
        def change(key, version, target):
            if key == "a" and target == "active":
                self.client.calls.append((key, version, target))
                raise DrainError("response_lost", uncertain=True)
            original_change(key, version, target)
        self.client.change = change
        self.run_episode(value)
        self.assertEqual(self.client.tasks["a"]["state"], "paused")
        self.client.change = original_change
        previous = deepcopy(self.client.calls)
        self.episode.restore(self.journal.load())
        self.assertEqual(self.client.calls, previous)
        self.assertEqual(self.journal.load()["phase"], "recovery_required")

    def test_process_loss_with_intent_recovers_only_durable_acknowledgments(self):
        value = self.plan()
        self.client.change("a", 1, "paused")
        self.client.change("b", 4, "paused")
        value.update(phase="pausing", intent={"action": "pause", "id": "b", "version": 4},
                     confirmed={"a": {"version": 2}}, deadline="2026-01-01T00:15:00+00:00")
        self.journal.save(value)
        recovered = self.journal.load()
        self.clock.seconds = 10000
        self.episode.restore(recovered)
        self.assertEqual(self.client.tasks["a"]["state"], "active")
        self.assertEqual(self.client.tasks["b"]["state"], "paused")
        self.assertEqual(recovered["phase"], "recovery_required")
        self.assertEqual(recovered["deadline"], "2026-01-01T00:15:00+00:00")

    def test_data_change_and_new_run_during_command_are_not_reported_complete(self):
        for mode in ("data", "new_run"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                client = Client()
                journal = StateFile(Path(d).resolve()/"state.json")
                with journal.locked():
                    episode = Episode(client, journal)
                    value = episode.plan(expected_count=2, command=COMMAND)
                    def command(*_):
                        if mode == "data": client.preservation["files"] = "changed"
                        else: client.new_count = 1
                    with self.assertRaises(DrainError):
                        episode.run(value, value["plan_sha256"], execute=command)
                    self.assertNotIn("deployment_completed", value)
                    self.assertTrue(all(client.tasks[k]["state"] == "active" for k in ("a", "b")))

    def test_slow_final_check_does_not_begin_deployment_without_recovery_time(self):
        value = self.plan(COMMAND)
        def slow():
            self.clock.seconds = 550
            return deepcopy(self.client.preservation)
        self.client.fence = slow
        with self.assertRaisesRegex(DrainError, "deployment_budget_exhausted"):
            self.run_episode(value, lambda *_: self.fail("late deployment"))
        self.assertTrue(all(self.client.tasks[k]["state"] == "active" for k in ("a", "b")))

    def test_checksum_lock_and_uncooperative_file_edit_are_rejected(self):
        value = self.plan()
        with self.assertRaisesRegex(DrainError, "state_busy"):
            with StateFile(self.path).locked(): pass
        raw = self.path.read_text()
        self.path.write_text(raw + " ")
        with self.assertRaisesRegex(DrainError, "state_changed_outside_lock"):
            self.run_episode(value)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.path.read_text(), raw + " ")
        envelope = json.loads(raw)
        envelope["payload"]["phase"] = "tampered"
        self.path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(DrainError, "corrupt_state"):
            StateFile(self.path).load()

    def test_symlink_state_is_not_followed(self):
        target = self.path.parent / "other.json"
        target.write_text("untouched")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(DrainError, "state_already_exists"):
            self.plan()
        self.assertEqual(target.read_text(), "untouched")

    def test_different_project_or_unreviewed_plan_cannot_pause(self):
        value = self.plan()
        with self.assertRaisesRegex(DrainError, "reviewed_plan_required"):
            self.episode.run(value, "wrong-hash")
        self.client.project_name = "different-project"
        with self.assertRaisesRegex(DrainError, "project_changed"):
            self.run_episode(value)
        self.assertEqual(self.client.calls, [])


class AdapterTests(unittest.TestCase):
    def test_service_snapshot_only_reads_application_identity_and_mounts(self):
        values = [{"id": key, "image": "fixture-old", "status": "running", "health": "healthy" if key == "backend" else None, "project": "fixture-project", "service": key, "mounts": [{"Type": "volume", "Name": "fixture-volume", "Destination": "/app/data", "RW": True, "Source": "fixture-private-source"}]} for key in ("backend", "runner")]
        runner = subprocess.CompletedProcess([], 0, stdout="fixture-runner\n")
        inspected = subprocess.CompletedProcess([], 0, stdout="\n".join(json.dumps(v) for v in values))
        with patch.object(DockerClient, "project", return_value="fixture-project"), patch("scripts.scheduler_drain.subprocess.run", side_effect=[runner, inspected]) as run:
            result = DockerClient("fixture-backend").services()
        self.assertNotIn("fixture-private-source", json.dumps(result))
        self.assertNotIn(".Config.Env", run.call_args_list[1].args[0][3])
        with patch.object(DockerClient, "project", return_value="fixture-project"), patch("scripts.scheduler_drain.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="one\ntwo\n")):
            with self.assertRaisesRegex(DrainError, "one_runner_required"):
                DockerClient("fixture-backend").services()

    def test_stable_backend_name_is_required_across_container_recreation(self):
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps({"name": "/fixture-backend", "project": "fixture-project", "service": "backend"}))
        with patch("scripts.scheduler_drain.Path.is_file", return_value=True), patch("scripts.scheduler_drain.subprocess.run", return_value=result):
            self.assertEqual(DockerClient("fixture-backend").project(), "fixture-project")
            with self.assertRaisesRegex(DrainError, "stable_compose_backend_name_required"):
                DockerClient("container-id").project()

    def test_missing_linux_lock_table_stops_before_docker_or_pause(self):
        with patch("scripts.scheduler_drain.Path.is_file", return_value=False), patch("scripts.scheduler_drain.subprocess.run") as run:
            with self.assertRaisesRegex(DrainError, "linux_docker_host_required"):
                DockerClient("fixture-backend").project()
            run.assert_not_called()

    def test_unknown_container_output_and_os_failures_are_fenced(self):
        for output in ("null", "{}", '{"ok":true}', '{"ok":"yes"}'):
            with self.subTest(output=output), patch("scripts.scheduler_drain.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=output)):
                with self.assertRaises(DrainError) as caught:
                    DockerClient("fixture-backend").change("00000000-0000-0000-0000-000000000001", 1, "paused")
                self.assertTrue(caught.exception.uncertain)
        with patch("scripts.scheduler_drain.subprocess.run", side_effect=OSError("fixture-only")):
            with self.assertRaises(DrainError) as caught:
                DockerClient("fixture-backend").task("00000000-0000-0000-0000-000000000001")
            self.assertFalse(caught.exception.uncertain)
        with patch("scripts.scheduler_drain.subprocess.run", side_effect=DrainError("operator_interrupted")):
            with self.assertRaises(DrainError) as caught:
                DockerClient("fixture-backend").change("00000000-0000-0000-0000-000000000001", 1, "paused")
            self.assertTrue(caught.exception.uncertain)

    def test_committed_api_adapter_handles_ack_conflict_and_ambiguous_response(self):
        # Execute the actual committed adapter against synthetic modules and
        # responses. No application dependencies, socket or Docker is used.
        def module(name, **attributes):
            value = ModuleType(name)
            value.__dict__.update(attributes)
            return value
        settings = SimpleNamespace(database_url="fixture-only", server_port=12345,
                                   api_token=SimpleNamespace(get_secret_value=lambda: "fixture-secret-do-not-output"))
        modules = {
            "sqlalchemy": module("sqlalchemy", create_engine=lambda *a, **k: None, text=lambda q: q),
            "app": module("app"), "app.core": module("app.core"),
            "app.core.config": module("app.core.config", get_settings=lambda: settings),
            "app.scheduling": module("app.scheduling"),
            "app.scheduling.registry": module("app.scheduling.registry", task_registry=None),
        }
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, *_): raise AssertionError("must not read task response fields")
        request = {"action": "change", "id": "00000000-0000-0000-0000-000000000001", "version": 1, "target": "paused"}
        for code in (200, 409, 500, None):
            with self.subTest(code=code):
                stdout = io.StringIO()
                opener = SimpleNamespace(open=lambda *a, **k: Response())
                if code != 200:
                    def fail(*_args, **_kwargs):
                        if code is None: raise TimeoutError("fixture-secret-do-not-output")
                        raise HTTPError("http://fixture", code, "fixture-secret-do-not-output", {}, None)
                    opener.open = fail
                with patch.dict(sys.modules, modules), patch("urllib.request.build_opener", return_value=opener), patch("sys.stdin", io.StringIO(json.dumps(request))), patch("sys.stdout", stdout):
                    exec(compile(CONTAINER_CODE, "committed-adapter", "exec"), {})
                result = json.loads(stdout.getvalue())
                self.assertNotIn("fixture-secret-do-not-output", stdout.getvalue())
                self.assertEqual(result["ok"], code == 200)
                if code != 200: self.assertEqual(result["uncertain"], code != 409)

    def test_only_explicit_same_project_application_recreation_is_accepted(self):
        validate_deployment(COMMAND, "fixture-project")
        mixed_projects = COMMAND[:6] + ["--project-name", "different"] + COMMAND[6:]
        for command in (["make", "selfhost"], COMMAND[:-1] + ["postgres"], COMMAND[:-1] + ["frontend"], COMMAND[:], mixed_projects, ["docker", "compose", "down"]):
            with self.subTest(command=command):
                project = "different" if command == COMMAND else "fixture-project"
                with self.assertRaises(DrainError): validate_deployment(command, project)

    def test_container_transport_has_no_host_token_or_shell_command(self):
        with patch("scripts.scheduler_drain.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout='{"ok":true,"value":{"acknowledged":true}}')) as run:
            DockerClient("fixture-backend").change("00000000-0000-0000-0000-000000000001", 1, "paused")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["docker", "exec", "-i", "fixture-backend"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(set(request), {"action", "id", "version", "target"})
        compile(CONTAINER_CODE, "committed-container-adapter", "exec")

    def test_cli_options_after_action_and_deployment_separator(self):
        with tempfile.TemporaryDirectory() as d, patch("scripts.scheduler_drain.DockerClient", return_value=Client()), patch("builtins.print"):
            argv = ["scheduler_drain.py", "plan", "--state", str(Path(d).resolve()/"state.json"), "--container", "fixture-backend", "--expected-active-count", "2", "--"] + COMMAND
            with patch("sys.argv", argv): self.assertEqual(main(), 0)
            value = StateFile(Path(d).resolve()/"state.json").load()
            self.assertEqual(value["plan"]["command"], COMMAND)
            self.assertEqual(value["plan_sha256"], digest(value["plan"]))


if __name__ == "__main__":
    unittest.main()
