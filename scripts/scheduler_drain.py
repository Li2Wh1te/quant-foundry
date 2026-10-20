#!/usr/bin/env python3
"""Pause future schedules, drain accepted runs, then restore only owned changes.

This foreground operator uses the application's existing authenticated APIs.
It does not cancel runs, edit database state, migrate, or collect new input.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from uuid import uuid4


class DrainError(Exception):
    def __init__(self, reason: str, *, uncertain: bool = False):
        super().__init__(reason)
        self.reason, self.uncertain = reason, uncertain


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise DrainError("timezone_required")
    return result.astimezone(timezone.utc)


class StateFile:
    """Private, checksummed atomic state with one cooperating command owner."""
    def __init__(self, path):
        self.path = Path(path)
        self.etag = None

    @contextmanager
    def locked(self):
        parent = self.path.parent
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise DrainError("absolute_state_path_required")
        if parent.resolve() != parent:
            raise DrainError("canonical_state_directory_required")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.resolve() != parent or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
            raise DrainError("private_state_directory_required")
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise DrainError("unsafe_state_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DrainError("state_busy") from None
            yield self
        finally:
            os.close(fd)

    def _bytes(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4 * 1024 * 1024:
                raise DrainError("unsafe_state_file")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                return handle.read(4 * 1024 * 1024 + 1)
        finally:
            os.close(fd)

    def load(self):
        raw = self._bytes()
        try:
            envelope = json.loads(raw)
            value = envelope["payload"]
            if envelope["sha256"] != digest(value) or value["version"] != 1 or value["plan_sha256"] != digest(value["plan"]):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise DrainError("corrupt_state") from None
        self.etag = hashlib.sha256(raw).hexdigest()
        return value

    def unchanged(self):
        if self.etag is None:
            if self.path.exists() or self.path.is_symlink():
                raise DrainError("state_already_exists")
        elif hashlib.sha256(self._bytes()).hexdigest() != self.etag:
            raise DrainError("state_changed_outside_lock")

    def save(self, value):
        self.unchanged()
        raw = encoded({"payload": value, "sha256": digest(value)}) + b"\n"
        if len(raw) > 4 * 1024 * 1024:
            raise DrainError("state_budget_exceeded")
        temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            self.unchanged()
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self.etag = hashlib.sha256(raw).hexdigest()
        finally:
            temporary.unlink(missing_ok=True)


TASK_FIELDS = ("id", "task_type", "version", "state", "schedule", "parameters_hash", "parameter_version")
DEFINITION_FIELDS = ("task_type", "schedule", "parameters_hash", "parameter_version")
RUN_FIELDS = ("id", "task_id", "task_version", "task_type", "trigger_type", "parameter_version", "priority", "parameters_hash", "created_at")


def active_tasks(snapshot):
    return sorted((t for t in snapshot["tasks"] if t["registered_here"] and t["state"] == "active"), key=lambda t: t["id"])


def task_vector(tasks):
    return [{k: t[k] for k in TASK_FIELDS} for t in tasks]


class Episode:
    def __init__(self, client, journal, *, now=utcnow, monotonic=time.monotonic, sleep=time.sleep, emit=None):
        self.client, self.journal = client, journal
        self.now, self.monotonic, self.sleep = now, monotonic, sleep
        self.emit = emit

    def plan(self, *, expected_count, seconds=7200, command=()):
        if not 600 <= seconds <= 7200:
            raise DrainError("episode_budget_out_of_range")
        snapshot = self.client.snapshot()
        tasks = active_tasks(snapshot)
        if len(tasks) != expected_count or not tasks:
            raise DrainError("active_scope_count_changed")
        if any(t["schedule"]["type"] != "cron" for t in tasks):
            raise DrainError("only_recurring_cron_supported")
        registered = {t["id"] for t in snapshot["tasks"] if t["registered_here"]}
        if any(r["task_id"] not in registered for r in snapshot["runs"]):
            raise DrainError("unregistered_pending_work")
        if command:
            validate_deployment(command, self.client.project())
        plan = {"episode": uuid4().hex, "created_at": self.now().isoformat(), "seconds": seconds, "project": self.client.project(),
                "tasks": tasks, "runs": snapshot["runs"], "command": list(command), "services": self.client.services()}
        value = {"version": 1, "plan": plan, "plan_sha256": digest(plan), "phase": "planned",
                 "confirmed": {}, "intent": None, "unresolved": [], "observations": [], "restore": {}, "errors": []}
        self.journal.save(value)
        return value

    def _guard(self, value):
        self.journal.unchanged()
        if value["phase"] != "deploying" and self.client.services() != value["plan"]["services"]:
            raise DrainError("application_services_changed")
        snapshot = self.client.snapshot()
        if active_tasks(snapshot):
            raise DrainError("unexpected_active_schedule")
        tasks = {t["id"]: t for t in snapshot["tasks"]}
        for original in value["plan"]["tasks"]:
            current = tasks.get(original["id"])
            receipt = value["confirmed"].get(original["id"])
            if not current or not receipt or current["state"] != "paused" or current["version"] != receipt["version"] or any(current[k] != original[k] for k in DEFINITION_FIELDS):
                raise DrainError("paused_task_modified")
        accepted = value["accepted"]
        saved = {r["id"]: r for r in self.client.read_runs([r["id"] for r in accepted])}
        for original in accepted:
            current = saved.get(original["id"])
            if not current or any(current[k] != original[k] for k in RUN_FIELDS):
                raise DrainError("accepted_run_missing_or_modified")
        if self.client.new_runs(value["pause_finished"]):
            raise DrainError("new_run_after_pause")
        counts = {s: sum(r["status"] == s for r in snapshot["runs"]) for s in ("queued", "running")}
        value["observations"].append({"at": self.now().isoformat(), "counts": counts})
        self.journal.save(value)
        return counts

    def restore(self, value, *, budget=300):
        """An expired episode still permits recovery; task CAS remains required."""
        self.journal.unchanged()
        if value["intent"]:
            # A persisted intent without an acknowledgment must not be converted
            # into ownership merely because the current row looks like ours.
            if value["intent"] not in value["unresolved"]:
                value["unresolved"].append(value["intent"])
            value["intent"] = None
            self.journal.save(value)
        original = {t["id"]: t for t in value["plan"]["tasks"]}
        until = self.monotonic() + budget
        for task_id, receipt in reversed(list(value["confirmed"].items())):
            if value["restore"].get(task_id, {}).get("outcome") == "restored":
                continue
            if any(i["action"] == "resume" and i["id"] == task_id for i in value["unresolved"]):
                # A later observation cannot turn a lost response into a durable
                # receipt. Preserve the ambiguity instead of repeating the API.
                continue
            if self.monotonic() >= until:
                value["restore"][task_id] = {"outcome": "recovery_budget_exhausted"}
                continue
            try:
                current = self.client.task(task_id)
            except DrainError as error:
                value["restore"][task_id] = {"outcome": error.reason, "uncertain": False}
                self.journal.save(value)
                continue
            expected = original[task_id]
            if current is None or current["state"] != "paused" or current["version"] != receipt["version"] or any(current[k] != expected[k] for k in DEFINITION_FIELDS):
                value["restore"][task_id] = {"outcome": "concurrent_change_preserved"}
            else:
                # Do not guess ownership after a lost response. Persist the intent
                # before the API, and retain it until a definite acknowledgment.
                value["intent"] = {"action": "resume", "id": task_id, "version": receipt["version"]}
                self.journal.save(value)
                try:
                    self.client.change(task_id, receipt["version"], "active")
                    value["restore"][task_id] = {"outcome": "restored", "version": receipt["version"] + 1}
                    value["intent"] = None
                except DrainError as error:
                    value["restore"][task_id] = {"outcome": error.reason, "uncertain": error.uncertain}
                    if error.uncertain:
                        value["unresolved"].append(value["intent"])
                    value["intent"] = None
            self.journal.save(value)
        value["phase"] = "recovery_required" if value["unresolved"] or any(r["outcome"] not in ("restored", "concurrent_change_preserved") for r in value["restore"].values()) else "restored"
        self.journal.save(value)
        return value

    def run(self, value, expected_sha, *, interval=1800, execute=None):
        if value["phase"] != "planned" or expected_sha != value["plan_sha256"]:
            raise DrainError("reviewed_plan_required")
        if self.client.project() != value["plan"]["project"]:
            raise DrainError("project_changed")
        if self.client.services() != value["plan"]["services"]:
            raise DrainError("application_services_changed")
        current = self.client.snapshot()
        if task_vector(active_tasks(current)) != task_vector(value["plan"]["tasks"]):
            raise DrainError("active_scope_or_version_changed")
        started = self.now()
        value.update(phase="pausing", started_at=started.isoformat(), deadline=(started + timedelta(seconds=value["plan"]["seconds"])).isoformat())
        until = self.monotonic() + value["plan"]["seconds"] - 300
        self.journal.save(value)
        try:
            if self.emit:
                self.emit({"message": "调度暂停窗口已开始，任务数量与绝对截止时间已记录。", "tasks": len(value["plan"]["tasks"]), "started_at": value["started_at"], "deadline": value["deadline"]})
            for task in value["plan"]["tasks"]:
                if self.monotonic() >= until:
                    raise DrainError("episode_expired")
                self.journal.unchanged()
                live = self.client.task(task["id"])
                if live is None or any(live[k] != task[k] for k in TASK_FIELDS):
                    raise DrainError("task_changed_before_pause")
                value["intent"] = {"action": "pause", "id": task["id"], "version": task["version"]}
                self.journal.save(value)
                try:
                    self.client.change(task["id"], task["version"], "paused")
                except DrainError as error:
                    if not error.uncertain:
                        value["intent"] = None
                    raise
                value["confirmed"][task["id"]] = {"version": task["version"] + 1}
                value["intent"] = None
                self.journal.save(value)
            after = self.client.snapshot()
            accepted = {}
            for run in value["plan"]["runs"] + current["runs"] + after["runs"]:
                # Retain the first frozen record so an intervening edit cannot
                # silently replace the reviewed parameter/priority snapshot.
                accepted.setdefault(run["id"], run)
            value["accepted"] = list(accepted.values())
            value.update(phase="draining", pause_finished=self.now().isoformat())
            self.journal.save(value)
            while True:
                if self.now() < started or self.now() >= timestamp(value["deadline"]) or self.monotonic() >= until:
                    raise DrainError("episode_expired")
                counts = self._guard(value)
                if not any(counts.values()):
                    break
                next_check = min(until, self.monotonic() + interval)
                while self.monotonic() < next_check:
                    self.sleep(min(60, next_check - self.monotonic()))
            before = self.client.fence()
            if before["held_locks"] or before["backtests"] or before["ingestion_leases"]:
                raise DrainError("other_active_writers")
            if any(self._guard(value).values()):
                raise DrainError("work_arrived_before_deployment")
            value.update(phase="ready", preservation_before=before)
            self.journal.save(value)
            if value["plan"]["command"]:
                # Leave room for the bounded command and all post-command
                # observations, in addition to the separate recovery reserve.
                if execute is None or until - self.monotonic() < 540:
                    raise DrainError("deployment_budget_exhausted")
                validate_deployment(value["plan"]["command"], self.client.project())
                value["phase"] = "deploying"
                self.journal.save(value)
                execute(value["plan"]["command"], min(180, until - self.monotonic()))
                if self.client.fence() != before:
                    raise DrainError("preservation_changed")
                if any(self._guard(value).values()):
                    raise DrainError("work_arrived_during_deployment")
                value["deployment_completed"] = True
                self.journal.save(value)
        except BaseException as error:
            value["errors"].append(error.reason if isinstance(error, DrainError) else type(error).__name__)
            self.journal.save(value)
            raise
        finally:
            self.restore(value)
        return value


def validate_deployment(command, project):
    """Only an explicit same-project Compose application recreation is allowed."""
    if list(command[:2]) != ["docker", "compose"] or "up" not in command:
        raise DrainError("explicit_compose_up_required")
    index = command.index("up")
    options = {}; i = 2
    while i < index:
        flag = command[i]
        if flag not in ("-f", "--file", "-p", "--project-name", "--project-directory", "--env-file") or i + 1 >= index:
            raise DrainError("unsupported_compose_option")
        options.setdefault(flag, []).append(command[i + 1]); i += 2
    projects = options.get("-p", []) + options.get("--project-name", [])
    if not (options.get("-f") or options.get("--file")) or projects != [project]:
        raise DrainError("compose_profile_or_project_mismatch")
    tail = list(command[index + 1:])
    required = {"-d", "--no-deps", "--no-build", "--wait"}
    if not required.issubset(tail) or "--pull" not in tail or tail[tail.index("--pull") + 1:tail.index("--pull") + 2] != ["never"]:
        raise DrainError("bounded_application_recreation_required")
    services = []; i = 0
    while i < len(tail):
        item = tail[i]
        if item in required:
            i += 1
        elif item == "--pull" and i + 1 < len(tail) and tail[i + 1] == "never":
            i += 2
        elif item == "--wait-timeout" and i + 1 < len(tail) and tail[i + 1].isdigit() and 1 <= int(tail[i + 1]) <= 120:
            i += 2
        elif item in ("backend", "runner"):
            services.append(item); i += 1
        else:
            raise DrainError("unsupported_application_recreation")
    if sorted(services) != ["backend", "runner"] or "--wait-timeout" not in tail:
        raise DrainError("explicit_backend_runner_and_wait_required")


CONTAINER_CODE = r'''
import hashlib,json,os,stat,sys
from pathlib import Path
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError
from sqlalchemy import create_engine,text
from app.core.config import get_settings
from app.scheduling.registry import task_registry
# Authentication stays inside the existing container. SQL returns only metadata
# and parameter digests; no environment or task parameter values are exported.
request=json.load(sys.stdin);settings=get_settings()
class ScopeTooLarge(Exception):pass
engine=create_engine(settings.database_url,connect_args={'connect_timeout':5,'options':'-c statement_timeout=8000 -c lock_timeout=1000'})
def rows(q,p=None):
 with engine.connect() as c:
  c.execute(text('SET TRANSACTION READ ONLY'));r=[dict(x) for x in c.execute(text(q),p or {}).mappings()];c.rollback();return r
task_sql="SELECT id,task_type,state,version,schedule,md5(parameters::text) AS parameters_hash,parameter_version FROM scheduled_tasks"
run_sql="SELECT id,task_id,task_version,task_type,status,trigger_type,parameter_version,priority,md5(parameters::text) AS parameters_hash,created_at FROM task_runs"
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
try:
 action=request['action']
 if action=='snapshot':
  known={d.key for d in task_registry.list()};tasks=rows(task_sql+' ORDER BY id LIMIT 2001')
  pending=rows(run_sql+" WHERE status IN ('queued','running') ORDER BY id LIMIT 2001")
  if len(tasks)>2000 or len(pending)>2000:raise ScopeTooLarge()
  for t in tasks:t['registered_here']=t['task_type'] in known
  value={'tasks':tasks,'runs':pending}
 elif action=='task':
  value=rows(task_sql+' WHERE id=:i',{'i':request['id']});value=value[0] if value else None
 elif action=='read_runs':value=rows(run_sql+' WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id',{'ids':request['ids']}) if request['ids'] else []
 elif action=='new_runs':value=rows('SELECT count(*) AS n FROM task_runs WHERE created_at>CAST(:t AS timestamptz)',{'t':request['since']})[0]['n']
 elif action=='change':
  from uuid import UUID
  task_id=str(UUID(request['id']));version=int(request['version']);endpoint='pause' if request['target']=='paused' else 'resume'
  url=f'http://127.0.0.1:{settings.server_port}/api/admin/tasks/{task_id}/{endpoint}?version={version}'
  with build_opener(NoRedirect).open(Request(url,method='POST',headers={'Authorization':'Bearer '+settings.api_token.get_secret_value()}),timeout=10) as response:
   value={'acknowledged':response.status==200}
 elif action=='fence':
  value={}
  queries={'phase':'SELECT phase FROM data_store_legacy_maintenance WHERE singleton=1','head':'SELECT version_num FROM alembic_version','policy':'SELECT root_token,policy_json FROM data_store_runtime WHERE singleton=1','files':"SELECT count(*) AS n,coalesce(sum(byte_count),0) AS bytes,md5(string_agg(id::text||':'||content_hash||':'||path,'|' ORDER BY id)) AS digest FROM data_store_files",'datasets':"SELECT md5(string_agg(name||':'||generation::text||':'||coalesce(last_commit::text,'')||':'||row_count::text||':'||byte_count::text,'|' ORDER BY name)) AS digest FROM data_store_datasets",'checkpoints':"SELECT md5(string_agg(md5(dataset||':'||scope_key||':'||checkpoint_json),'|' ORDER BY dataset,scope_key)) AS digest FROM data_store_scopes",'ranges':'SELECT dataset,count(*) AS n,count(*) FILTER(WHERE active IS NOT NULL) AS active FROM data_store_source_ranges GROUP BY dataset ORDER BY dataset'}
  for name,q in queries.items():value[name]=rows(q)
  value['backtests']=rows("SELECT count(*) AS n FROM backtest_runs WHERE status IN ('queued','starting','running','cancel_requested')")[0]['n']
  value['ingestion_leases']=rows("SELECT count(*) AS n FROM tonghuashun_dump_imports WHERE status='downloading' AND lease_until>now()")[0]['n']
  root=settings.data_store_root;value['lock_inodes']=[];value['spools']=[]
  for p in (root/'.locks').iterdir():
   s=p.lstat()
   if stat.S_ISREG(s.st_mode):value['lock_inodes'].append([os.major(s.st_dev),os.minor(s.st_dev),s.st_ino])
  for p in sorted((root/'.scratch').glob('*/spill/*.sqlite')):
   if p.is_symlink():raise ValueError('unsafe_spool')
   h=hashlib.sha256()
   with p.open('rb') as f:
    for block in iter(lambda:f.read(1048576),b''):h.update(block)
   value['spools'].append({'path':str(p.relative_to(root)),'bytes':p.stat().st_size,'sha256':h.hexdigest()})
 else:raise ValueError('unknown_action')
 print(json.dumps({'ok':True,'value':value},default=str))
except HTTPError as error:
 code=error.code;error.close()
 print(json.dumps({'ok':False,'reason':'http_'+str(code),'uncertain':code not in (400,401,403,404,409,422)}))
except Exception as error:
 print(json.dumps({'ok':False,'reason':type(error).__name__,'uncertain':request['action']=='change'}))
'''


class DockerClient:
    """Use existing container authentication; only private metadata leaves it."""
    def __init__(self, container):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
            raise DrainError("invalid_container")
        self.container = container

    def _call(self, action, **fields):
        try:
            result = subprocess.run(["docker", "exec", "-i", self.container, "python", "-B", "-c", CONTAINER_CODE], input=json.dumps({"action": action, **fields}), text=True, capture_output=True, timeout=60)
            if result.returncode:
                raise DrainError("container_worker_failed", uncertain=action == "change")
            output = json.loads(result.stdout)
            if not isinstance(output, dict) or type(output.get("ok")) is not bool:
                raise ValueError()
            if output["ok"]:
                return output["value"]
            if not isinstance(output.get("reason"), str) or type(output.get("uncertain", False)) is not bool:
                raise ValueError()
        except DrainError as error:
            # A signal during an in-flight API cannot be a definite rejection.
            # Keep its persisted intent until a real acknowledgment is known.
            if action == "change":
                error.uncertain = True
            raise
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            raise DrainError("container_response_unknown", uncertain=action == "change") from None
        raise DrainError(output["reason"], uncertain=output.get("uncertain", False))

    def project(self):
        if not Path("/proc/locks").is_file():
            raise DrainError("linux_docker_host_required")
        template = '{"name":{{json .Name}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}}}'
        result = subprocess.run(["docker", "inspect", "--format", template, self.container], capture_output=True, text=True, timeout=10)
        if result.returncode or not result.stdout.strip():
            raise DrainError("compose_project_unavailable")
        try:
            value = json.loads(result.stdout)
            if value["name"] != "/" + self.container or value["service"] != "backend" or not value["project"]:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise DrainError("stable_compose_backend_name_required") from None
        return value["project"]

    def snapshot(self): return self._call("snapshot")
    def task(self, task_id): return self._call("task", id=task_id)
    def read_runs(self, ids): return self._call("read_runs", ids=ids)
    def new_runs(self, since): return self._call("new_runs", since=since)

    def services(self):
        """Freeze only application identity and mounts, never environment data."""
        project = self.project()
        try:
            runner = subprocess.run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=" + project, "--filter", "label=com.docker.compose.service=runner", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=10)
            names = runner.stdout.splitlines()
            if runner.returncode or len(names) != 1:
                raise DrainError("one_runner_required")
            template = '{"id":{{json .Id}},"image":{{json .Image}},"status":{{json .State.Status}},"health":{{if index .State "Health"}}{{json (index (index .State "Health") "Status")}}{{else}}null{{end}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"mounts":{{json .Mounts}}}'
            result = subprocess.run(["docker", "inspect", "--format", template, self.container, names[0]], capture_output=True, text=True, timeout=10)
            values = [json.loads(line) for line in result.stdout.splitlines()]
            if result.returncode or len(values) != 2 or {v["service"] for v in values} != {"backend", "runner"}:
                raise DrainError("application_services_unavailable")
            for value in values:
                if value["project"] != project or value["status"] != "running" or value["health"] not in (None, "healthy"):
                    raise DrainError("application_service_not_ready")
                value["mounts"] = sorted(({k: m.get(k) for k in ("Type", "Name", "Destination", "RW")} for m in value["mounts"]), key=lambda m: m["Destination"])
            return sorted(values, key=lambda v: v["service"])
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            raise DrainError("application_identity_unknown") from None
    def change(self, task_id, version, target):
        if target not in ("paused", "active") or not isinstance(version, int) or version < 1:
            raise DrainError("unsupported_task_state_change")
        if not self._call("change", id=task_id, version=version, target=target)["acknowledged"]:
            raise DrainError("api_response_unknown", uncertain=True)

    def fence(self):
        result = self._call("fence")
        identities = {tuple(i) for i in result.pop("lock_inodes")}
        held = []
        for line in Path("/proc/locks").read_text().splitlines():
            for word in line.split():
                if word.count(":") != 2: continue
                try:
                    a, b, c = word.split(":"); identity = (int(a, 16), int(b, 16), int(c))
                except ValueError: continue
                if identity in identities: held.append(identity)
        result["held_locks"] = sorted(held)
        return result


def execute(command, timeout):
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DrainError("deployment_command_timeout") from None
    if result.returncode:
        raise DrainError("deployment_command_failed_" + str(result.returncode))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run", "restore", "status"))
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--container")
    parser.add_argument("--expected-active-count", type=int)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--episode-seconds", type=int, default=7200)
    parser.add_argument("--check-seconds", type=int, default=1800)
    argv = sys.argv[1:]
    split = argv.index("--") if "--" in argv else len(argv)
    command = argv[split + 1:] if split < len(argv) else []
    args = parser.parse_args(argv[:split])
    if command and args.action != "plan":
        parser.error("the deployment command is supplied only to plan")
    if not 300 <= args.check_seconds <= 7200:
        parser.error("--check-seconds must be 300..7200")
    if args.action != "status" and not args.container:
        parser.error("--container is required")
    if args.action == "plan" and (args.expected_active_count is None or args.expected_active_count < 1):
        parser.error("plan requires a positive --expected-active-count")
    if args.action == "run" and not args.plan_sha256:
        parser.error("run requires the reviewed --plan-sha256")
    def interrupted(*_):
        raise DrainError("operator_interrupted")
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name): signal.signal(getattr(signal, name), interrupted)
    journal = StateFile(args.state)
    try:
        with journal.locked():
            episode = Episode(DockerClient(args.container), journal, emit=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)) if args.action != "status" else None
            if args.action == "plan":
                value = episode.plan(expected_count=args.expected_active_count, seconds=args.episode_seconds, command=command)
            else:
                value = journal.load()
                if args.action == "run": value = episode.run(value, args.plan_sha256, interval=args.check_seconds, execute=execute)
                elif args.action == "restore": value = episode.restore(value)
            print(json.dumps({"message": "调度运维阶段已完成，任务状态与恢复结果已记录。", "phase": value["phase"], "plan_sha256": value["plan_sha256"], "tasks": len(value["plan"]["tasks"]), "started_at": value.get("started_at"), "deadline": value.get("deadline"), "restore": value["restore"]}, ensure_ascii=False))
            return 0 if value["phase"] != "recovery_required" else 2
    except (DrainError, OSError) as error:
        print(json.dumps({"message": "调度运维已停止，请核对私有状态文件及未完成恢复。", "reason": error.reason if isinstance(error, DrainError) else type(error).__name__}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
