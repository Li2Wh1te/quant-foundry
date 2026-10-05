#!/usr/bin/env python3
"""One finite P01 switch using the application's existing admission fence.

Run on the Linux deployment host with --plan PLAN.json --evidence NEW.jsonl.
The reviewed plan supplies immutable images, a complete candidate app manifest,
the fresh all-178 fingerprint, the original-177 fingerprint, and the full owned
definition hash. No task state, configuration, credential or volume is changed.
There is no interactive boundary between drain, graceful stop, release and up.
--probe is the same repository code executed read-only inside a candidate.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
import argparse
import base64
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import sqlite3
import stat
import subprocess
import sys
import time
from uuid import UUID, uuid4

SERVICES = ('backend', 'runner')
SHA = re.compile(r'[0-9a-f]{64}\Z')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}\Z')
ORIGINAL_177 = '5fce87469ef73b798d72a2e0cdc8c901a6c8a8e852083336fc72e49f710bf3ce'
OWNED_TASK = '54af16b2-3f21-4650-9364-d01adbfefde7'
OCI_REVISION = 'org.opencontainers.image.revision'
COMPOSE_REPLACE = 'com.docker.compose.replace'


class Refused(RuntimeError):
    """Only fixed, non-secret reason codes may cross the evidence boundary."""


def require(condition, code):
    if not condition:
        raise Refused(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def clock_namespace():
    return os.readlink('/proc/self/ns/time')


def clock_domain():
    """Read this process's Linux kernel clock identity without changing it."""
    return {'namespace': clock_namespace(),
            'offset_namespace': os.readlink('/proc/self/ns/time_for_children'),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'offsets': Path('/proc/self/timens_offsets').read_text()}


def zero_offset_clock(domain):
    """Require complete kernel evidence for the supported zero-offset clocks.

    Linux exposes time_for_children offsets relative to the initial namespace.
    First prove they describe this process's current namespace, where the
    offsets are frozen by the existing member process. Distinct inodes with
    the same boot ID and zero offsets can therefore compare the original
    CLOCK_MONOTONIC deadline directly. No offset conversion, receive-time lease
    or unsupported-kernel fallback is permitted by this deployment controller.
    """
    require(isinstance(domain, dict) and
            set(domain) == {'namespace', 'offset_namespace', 'boot_id', 'offsets'} and
            all(isinstance(value, str) for value in domain.values()) and
            re.fullmatch(r'time:\[[0-9]+\]', domain['namespace']) and
            domain['offset_namespace'] == domain['namespace'] and
            re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', domain['boot_id']) and
            len(domain['offsets']) <= 512, 'GATE_CLOCK_OR_DEADLINE_INVALID')
    rows = [line.split() for line in domain['offsets'].splitlines()]
    require(len(rows) == 2 and all(len(row) == 3 and row[1:] == ['0', '0'] for row in rows) and
            {row[0] for row in rows} == {'monotonic', 'boottime'},
            'GATE_CLOCK_OR_DEADLINE_INVALID')
    return domain


@dataclass(frozen=True)
class Plan:
    project_directory: str
    compose_files: list[str]
    candidate_compose_file: str
    env_file: str
    old_image: str
    candidate_image: str
    candidate_head: str
    manifest_file: str
    task_fingerprint: str
    owned_definition_hash: str
    definitions_hash: str
    original_fingerprint: str = ORIGINAL_177
    owned_task_id: str = OWNED_TASK
    project: str = 'quant-foundry-p01'
    drain_seconds: int = 300
    hold_seconds: int = 180
    stop_seconds: int = 40
    probe_seconds: int = 15
    start_seconds: int = 90
    release_seconds: int = 10
    scratch_bytes: int = 32 * 1024**3

    def validate(self):
        require(self.project == 'quant-foundry-p01', 'P01_PROJECT_REQUIRED')
        require(self.original_fingerprint == ORIGINAL_177 and
                self.owned_task_id == OWNED_TASK, 'R01_TASK_SCOPE_REQUIRED')
        require(all(SHA.fullmatch(x or '') for x in
            (self.task_fingerprint, self.owned_definition_hash,
             self.original_fingerprint, self.definitions_hash)), 'INVALID_FINGERPRINT')
        require(all(IMAGE.fullmatch(x or '') for x in
            (self.old_image, self.candidate_image)), 'IMMUTABLE_IMAGES_REQUIRED')
        require(self.old_image != self.candidate_image and
                re.fullmatch(r'[0-9a-f]{40}', self.candidate_head or ''),
                'EXACT_CANDIDATE_REQUIRED')
        require(isinstance(self.compose_files, list) and
                1 <= len(self.compose_files) <= 16 and
                len(set(self.compose_files)) == len(self.compose_files) and
                self.candidate_compose_file not in self.compose_files and
                any(Path(x).name == 'compose.p01.yaml' for x in self.compose_files),
                'P01_COMPOSE_FILES_REQUIRED')
        paths = [self.project_directory, self.env_file, self.manifest_file,
                 self.candidate_compose_file, *self.compose_files]
        require(all(isinstance(x, str) and Path(x).is_absolute() and
                    '..' not in Path(x).parts for x in paths), 'ABSOLUTE_PATHS_REQUIRED')
        for name, low, high in (('drain_seconds', 1, 600), ('hold_seconds', 1, 240),
                ('stop_seconds', 1, 45), ('probe_seconds', 1, 20),
                ('start_seconds', 1, 120), ('release_seconds', 1, 15),
                ('scratch_bytes', 1, 32 * 1024**3)):
            require(type(getattr(self, name)) is int and
                    low <= getattr(self, name) <= high, 'FINITE_BOUNDS_REQUIRED')
        require(self.hold_seconds >= 2 * self.stop_seconds +
                3 * self.probe_seconds + self.release_seconds + 5,
                'HOLD_CANNOT_COVER_SWITCH')


def check_snapshot(plan, snapshot, previous=None, *, idle=True, fenced=False):
    require(snapshot['task_count'] == 178 and
            snapshot['task_fingerprint'] == plan.task_fingerprint and
            snapshot['original_fingerprint'] == plan.original_fingerprint and
            snapshot['owned_definition_hash'] == plan.owned_definition_hash and
            snapshot['definitions_hash'] == plan.definitions_hash,
            'TASK_DEFINITIONS_CHANGED')
    if idle:
        require(not any(snapshot['activity'].values()), 'WORK_NOT_IDLE')
        require('resources' in snapshot, 'RESOURCE_PROOF_REQUIRED')
    if previous is not None:
        for field in ('definitions_hash', 'source_configs_hash'):
            require(snapshot[field] == previous[field], 'DEFINITIONS_OR_CONFIG_CHANGED')
        records = {r['id']: r for r in snapshot['records']}
        for old in previous['records']:
            new = records.get(old['id'])
            require(new is not None and new['identity_hash'] == old['identity_hash'],
                    'CAPTURED_RUN_RECORD_CHANGED')
            if fenced:
                require(new['record_hash'] == old['record_hash'], 'FENCED_RUN_RECORD_CHANGED')
        if fenced:
            require(snapshot['resources'] == previous['resources'], 'SEALED_RESOURCES_CHANGED')


def switch(plan, host, event):
    """Execute once; every post-stop error returns failure after one recovery.

    The gate object owns its child pipe. A drained stdout line is insufficient:
    every stop/release step checks the original deadline and live gate, and the
    next phase requires an actual successful release and child exit. Recovery
    never blindly replaces a candidate that could have accepted work.
    """
    gate = None
    stop_attempted = False
    captured = []
    sink = event
    evidence_errors = []
    def safe_event(stage, message, **fields):
        # Full disks, failed fsync and broken stdout are real deployment
        # failures. Recovery cannot depend on writing another event to the
        # same failed sink. Preserve its error state, attempt only a fixed
        # secret-free stderr code, and never let logging bypass recovery.
        try:
            sink(stage, message, evidence_error_count=len(evidence_errors), **fields)
            return True
        except Exception:
            evidence_errors.append(stage)
            try:
                os.write(2, b'R01_SWITCH_EVIDENCE_UNAVAILABLE\n')
            except OSError:
                pass
            return False
    def event(stage, message, **fields):
        # Normal switching stops on the first missing evidence receipt. The
        # exception/finally/recovery paths instead use safe_event directly and
        # remain executable even when all subsequent sink writes also fail.
        require(safe_event(stage, message, **fields), 'EVIDENCE_UNAVAILABLE')
    try:
        plan.validate()
        host.preflight()
        before = host.probe(captured, resources=False)
        check_snapshot(plan, before, idle=False)
        captured = [r['id'] for r in before['records']]
        event('preflight', 'P01 候选镜像和配置已核验，178 项任务及已有运行记录已捕获。',
              candidate_head=plan.candidate_head, candidate_image=plan.candidate_image,
              captured_records=before['records'])
        gate = host.gate()
        ready = gate.drained()
        require(ready['task_count'] == 178 and
                ready['task_fingerprint'] == plan.task_fingerprint and
                ready['accepted_runs'] == 0 and ready['backtests'] == 0,
                'DRAIN_PROOF_MISMATCH')
        gate.check(2 * plan.stop_seconds + 3 * plan.probe_seconds +
                   plan.release_seconds + 5)
        drained = host.probe(captured, resources=True)
        check_snapshot(plan, drained, before)
        captured = [r['id'] for r in drained['records']]
        event('drained', 'P01 已接受运行、回测和本地处理均为空闲，合法 sealed checkpoint 已只读核验。',
              release_deadline_monotonic=ready['release_deadline_monotonic'],
              clock_domain=ready.get('clock_domain'),
              host_clock_domain=getattr(gate, 'host_clock_domain', None),
              resources=drained['resources'], captured_records=drained['records'])
        for index, service in enumerate(SERVICES):
            host.require_services(plan.old_image, running=SERVICES[index:])
            gate.check((2 - index) * plan.stop_seconds +
                       2 * plan.probe_seconds + plan.release_seconds + 5)
            if service == 'runner':
                # The source-row gate does not fence backtest admission or
                # claiming. Backend exit therefore does not prove that the
                # surviving runner stayed idle. Re-read real activity and
                # fenced records/resources before its signal, then recheck
                # the same deadline after SQL and evidence writes return.
                runner_ready = host.probe(captured, resources=True)
                check_snapshot(plan, runner_ready, drained, fenced=True)
                captured = [r['id'] for r in runner_ready['records']]
                event('runner_idle', 'P01 backend 已退出，runner 停止前再次核验已接受运行、回测及本地处理为空闲。',
                      activity=runner_ready['activity'],
                      release_deadline_monotonic=ready['release_deadline_monotonic'])
                gate.check(plan.stop_seconds + plan.probe_seconds + plan.release_seconds + 5)
            # Set before issuing SIGTERM: even a CLI error may have delivered
            # the signal, so the failure path must synchronously inspect/recover.
            stop_attempted = True
            host.stop(service)
            event('stopped', 'P01 旧服务已通过 SIGTERM 自然退出，未发送强制终止信号。', service=service)
        gate.check(plan.probe_seconds + plan.release_seconds + 5)
        stopped = host.probe(captured, resources=True)
        check_snapshot(plan, stopped, drained, fenced=True)
        host.require_services(plan.old_image, running=())
        gate.check(plan.release_seconds + 2)
        gate.release()
        gate = None
        event('released', 'P01 准入锁已在原截止时间内显式释放，gate 已退出且退出码为零。')
        host.start(plan.candidate_image, SERVICES)
        host.verify_candidate()
        final = host.probe(captured, resources=False)
        check_snapshot(plan, final, stopped, idle=False)
        event('completed', 'P01 backend 和 runner 已切至确切候选镜像，健康、任务及记录保留核验成功。',
              candidate_head=plan.candidate_head, candidate_image=plan.candidate_image,
              captured_records=final['records'])
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, Refused) else 'SWITCH_CHECK_FAILED'
        safe_event('refused', 'P01 本次切换未完成，正在核验服务状态并执行至多一次有界恢复。', reason=reason)
        if gate is not None:
            close_gate(gate, safe_event)
            gate = None
        if stop_attempted:
            recover(plan, host, captured, safe_event, before)
        else:
            safe_event('unchanged', 'P01 在停服前拒绝切换，旧服务未收到停止命令。')
        return 2
    finally:
        if gate is not None:
            close_gate(gate, safe_event)


def recover(plan, host, captured, event, before):
    """At most one guarded drain and one old-image up; never retry a failure.

    An already started candidate can claim work as soon as the first gate is
    released. A fresh finite fence is therefore necessary before replacing it.
    If that fence or any idle proof fails, running processes are preserved;
    only genuinely absent/exited services may be recreated. This reports an
    incomplete recovery, rather than pretending that an active process stopped.
    """
    gate = None
    guarded = True
    try:
        states = host.states()
        if any(s['running'] and s['image'] == plan.candidate_image for s in states.values()):
            gate = host.gate()
            ready = gate.drained()
            gate.check(2 * plan.stop_seconds + 2 * plan.probe_seconds + plan.release_seconds + 5)
            proof = host.probe(captured, resources=True)
            check_snapshot(plan, proof, before)
            event('recovery_drained', 'P01 候选服务恢复前已通过唯一一次有界排空，实际截止和资源证据已记录。',
                  release_deadline_monotonic=ready['release_deadline_monotonic'],
                  clock_domain=ready.get('clock_domain'),
                  host_clock_domain=getattr(gate, 'host_clock_domain', None), resources=proof['resources'])
            for index, service in enumerate(SERVICES):
                state = host.states()[service]
                if state['running'] and state['image'] == plan.candidate_image:
                    gate.check((2 - index) * plan.stop_seconds + plan.probe_seconds +
                               plan.release_seconds + 5)
                    if service == 'runner':
                        # Recovery has the same unfenced backtest race as the
                        # normal path. A busy or over-deadline probe refuses
                        # this runner stop; the finally path releases the gate
                        # and only actually exited services may restart once.
                        runner_ready = host.probe(captured, resources=True)
                        check_snapshot(plan, runner_ready, proof, fenced=True)
                        captured = [r['id'] for r in runner_ready['records']]
                        event('recovery_runner_idle', 'P01 恢复路径在 backend 退出后再次核验 runner 空闲，现有运行记录和封存断点均保留。',
                              activity=runner_ready['activity'],
                              release_deadline_monotonic=ready['release_deadline_monotonic'])
                        gate.check(plan.stop_seconds + plan.release_seconds + 5)
                    host.stop(service)
                    event('recovery_stopped', 'P01 已空闲候选服务通过 SIGTERM 自然退出，未替换活跃处理进程。', service=service)
            gate.check(plan.release_seconds + 2)
            gate.release()
            gate = None
            event('recovery_released', 'P01 恢复排空的准入锁已显式释放，gate 已实际退出且退出码为零。')
    except Exception:
        guarded = False
    finally:
        if gate is not None:
            close_gate(gate, event)
    try:
        states = host.states()
        missing = tuple(s for s in SERVICES if states[s]['stopped'])
        if missing:
            # A running/paused/restarting/unknown service is never an up target.
            # There is exactly one restart invocation, scoped to exited/absent
            # services. Other candidate services, even if busy, remain intact.
            event('recovery_restart', 'P01 正在唯一一次有界启动旧镜像，仅恢复已退出或缺失的服务。',
                  services=missing, old_image=plan.old_image, start_seconds=plan.start_seconds)
            host.start(plan.old_image, missing)
        states = host.states()
        restored = all(s['running'] and s['image'] == plan.old_image for s in states.values())
        event('recovery', 'P01 有界恢复已结束，实际服务状态已记录；本次切换仍按失败返回。',
              guarded=guarded, restored_old=restored, services=states)
    except Exception:
        event('recovery_failed', 'P01 唯一恢复尝试失败，现存运行进程未被强制替换，需检查实际服务状态。')


def close_gate(gate, event):
    started = time.monotonic()
    try:
        exited, code = gate.close()
        event('gate_closed', 'P01 gate 的有限退出检查已完成，实际退出状态已记录。',
              exited=exited, exit_code=code, started_monotonic=started,
              finished_monotonic=time.monotonic(),
              release_deadline_monotonic=getattr(gate, 'deadline', None),
              close_deadline_monotonic=getattr(gate, 'close_deadline', None))
    except Exception:
        event('gate_close_failed', 'P01 gate 的退出检查失败，本次不能声称准入锁已释放。')


class Child:
    """Bounded output and polling without subprocess timeout's implicit kill."""

    def __init__(self, command, cwd=None, interactive=False):
        self.process = subprocess.Popen(command, cwd=cwd,
            stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.total = 0

    def read(self, timeout):
        for key, _ in self.selector.select(max(0, timeout)):
            data = os.read(key.fd, 65536)
            if not data:
                self.selector.unregister(key.fileobj)
                continue
            self.total += len(data)
            require(self.total <= 2 * 1024**2, 'COMMAND_OUTPUT_LIMIT')
            self.buffer += data

    def finish(self):
        self.selector.close()
        self.process.stdout.close()
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()


def command(argv, seconds, cwd=None):
    child = Child(argv, cwd)
    deadline = time.monotonic() + seconds
    try:
        while child.process.poll() is None:
            require(time.monotonic() < deadline, 'COMMAND_TIMEOUT')
            child.read(min(.1, max(0, deadline - time.monotonic())))
        child.read(0)
        require(child.process.returncode == 0, 'COMMAND_FAILED')
        return child.buffer.decode('utf-8')
    except Exception:
        # This is the local CLI, not a container signal. Never use kill(), nor
        # subprocess.run(timeout=...), which automatically escalates to kill.
        if child.process.poll() is None:
            child.process.terminate()
            try:
                child.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        raise
    finally:
        child.finish()


class Gate:
    def __init__(self, child, plan):
        self.child, self.plan = child, plan
        self.events = []
        self.deadline = None
        self.drain_deadline = time.monotonic() + plan.drain_seconds + 15

    def collect(self, wait=0):
        self.child.read(wait)
        while b'\n' in self.child.buffer:
            line, self.child.buffer = self.child.buffer.split(b'\n', 1)
            require(len(line) <= 65536, 'GATE_OUTPUT_LIMIT')
            try:
                value = json.loads(line)
            except ValueError:
                raise Refused('GATE_PROTOCOL_INVALID') from None
            require(isinstance(value, dict), 'GATE_PROTOCOL_INVALID')
            self.events.append(value)
            require(len(self.events) <= 4096, 'GATE_OUTPUT_LIMIT')
            require(value.get('stage') != 'refused', 'GATE_REFUSED')

    def drained(self):
        end = self.drain_deadline
        while time.monotonic() < end:
            self.collect(.1)
            for value in self.events:
                if value.get('stage') == 'drained':
                    deadline = value.get('release_deadline_monotonic')
                    remote_clock = zero_offset_clock(value.get('clock_domain'))
                    host_clock = zero_offset_clock(clock_domain())
                    now = time.monotonic()
                    require(type(deadline) in (int, float) and math.isfinite(deadline) and
                        value.get('monotonic_namespace') == remote_clock['namespace'] and
                        remote_clock['boot_id'] == host_clock['boot_id'] and
                        now < deadline <= now + self.plan.hold_seconds + 1,
                        'GATE_CLOCK_OR_DEADLINE_INVALID')
                    self.host_clock_domain = host_clock
                    self.deadline = deadline
                    self.check(0)
                    return value
            require(self.child.process.poll() is None, 'GATE_EXITED')
        raise Refused('DRAIN_DEADLINE_EXCEEDED')

    def check(self, seconds):
        self.collect()
        require(self.child.process.poll() is None, 'GATE_EXITED')
        require(self.deadline is not None and
                time.monotonic() + seconds < self.deadline, 'RELEASE_WINDOW_INSUFFICIENT')

    def release(self):
        self.check(self.plan.release_seconds)
        try:
            self.child.process.stdin.write(('release ' + self.plan.task_fingerprint + '\n').encode())
            self.child.process.stdin.flush()
            end = min(self.deadline, time.monotonic() + self.plan.release_seconds)
            while time.monotonic() < end:
                self.collect(.05)
                if self.child.process.poll() is not None:
                    self.collect()
                    require(self.child.process.returncode == 0 and
                            any(v.get('stage') == 'released' for v in self.events),
                            'RELEASE_NOT_CONFIRMED')
                    self.child.finish()
                    return
            raise Refused('RELEASE_NOT_CONFIRMED')
        except (BrokenPipeError, OSError):
            raise Refused('RELEASE_NOT_CONFIRMED') from None

    def close(self):
        # EOF is an explicit refusal, not a release. It makes the application's
        # finally rollback run. Give the same finite gate its existing maximum
        # lifetime, never renew it or signal a database holder forcibly.
        if self.child.process.stdin is not None and not self.child.process.stdin.closed:
            self.child.process.stdin.close()
        # After drained, EOF needs no further accepted-work wait. At most five
        # seconds are allowed for ordinary polling/SQL to return. In particular
        # this must not re-open the original drain window after services stop.
        # A child that remains alive is explicitly recorded, never called a
        # released gate; bounded recovery proceeds without force-killing it.
        now = time.monotonic()
        end = min(now + 5, self.deadline + 5) if self.deadline is not None else self.drain_deadline + 5
        self.close_deadline = end
        try:
            while self.child.process.poll() is None and time.monotonic() < end:
                self.child.read(.1)
                self.child.buffer = b''
            code = self.child.process.poll()
            return code is not None, code
        finally:
            self.child.finish()


def image_only_configs(old, candidate):
    original, replacement = copy.deepcopy(old), copy.deepcopy(candidate)
    require(set(original.get('services', {})) == {'frontend', *SERVICES} and
            set(replacement.get('services', {})) == {'frontend', *SERVICES},
            'P01_SERVICES_REQUIRED')
    for service in SERVICES:
        original['services'][service].pop('image', None)
        replacement['services'][service].pop('image', None)
    require(original == replacement, 'COMPOSE_CHANGE_BEYOND_IMAGE')
    return digest(original)


def service_spec(container, *, verified_revision=None, verified_replacement=None):
    # Compose derives these three metadata labels from image/config file input.
    # All application labels and the other runtime configuration stay equal.
    config = copy.deepcopy(container['Config'])
    config.pop('Image', None)
    if verified_revision is not None:
        # Image provenance is allowed to follow the exact reviewed image/head,
        # not become an application-label exemption. The caller first proves
        # immutable image identity and obtains its OCI revision from the daemon.
        # Without that proof these labels remain in the ordinary strict hash.
        labels = config.get('Labels', {})
        require(re.fullmatch(r'[0-9a-f]{40}', verified_revision) and
                labels.get(OCI_REVISION) == verified_revision, 'CONTAINER_REVISION_MISMATCH')
        require(labels.get(COMPOSE_REPLACE) == verified_replacement and
                (verified_replacement is None or
                 isinstance(verified_replacement, str) and len(verified_replacement) <= 256),
                'CONTAINER_REPLACEMENT_MISMATCH')
        labels.pop(OCI_REVISION)
        labels.pop(COMPOSE_REPLACE, None)
    if config.get('Hostname') == container.get('Id', '')[:12]:
        config.pop('Hostname')
    for key in ('com.docker.compose.config-hash', 'com.docker.compose.image',
                'com.docker.compose.project.config_files'):
        config.get('Labels', {}).pop(key, None)
    config['Env'] = sorted(config.get('Env') or [])
    host = copy.deepcopy(container['HostConfig'])
    # Container identifiers may change; the trusted mounts, limits and network
    # remain the same. The daemon log file path is not operator configuration.
    host.pop('ContainerIDFile', None)
    mounts = sorted([{k: m.get(k) for k in ('Type', 'Name', 'Source', 'Destination', 'Mode', 'RW', 'Propagation')}
              for m in container['Mounts']], key=lambda m: m['Destination'])
    return digest({'config': config, 'host': host, 'mounts': mounts})


class DockerHost:
    def __init__(self, plan):
        self.plan = plan
        self.baseline = {}
        self.compose_hashes = {}
        self.image_revisions = {}
        self.service_ids = {}
        self.replacement_proofs = {}
        self.created_predecessors = {}
        self.stopped_ids = set()

    def compose(self, candidate=False):
        p = self.plan
        files = p.compose_files + ([p.candidate_compose_file] if candidate else [])
        return ['docker', 'compose', '--project-directory', p.project_directory,
                '--env-file', p.env_file, '-p', p.project, *sum((['-f', x] for x in files), [])]

    def run(self, args, seconds=10):
        return command(args, seconds, self.plan.project_directory)

    def config(self, candidate):
        return json.loads(self.run(self.compose(candidate) + ['config', '--format', 'json']))

    def image_revision(self, image, expected_image, *, expected_revision=None):
        info = json.loads(self.run(['docker', 'image', 'inspect', image]))[0]
        require(info['Id'] == expected_image, 'IMAGE_IDENTITY_MISMATCH')
        revision = (info.get('Config', {}).get('Labels') or {}).get(OCI_REVISION)
        require(isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{40}', revision),
                'IMAGE_REVISION_REQUIRED')
        require(expected_revision is None or revision == expected_revision,
                'CANDIDATE_IMAGE_REVISION_MISMATCH')
        require(self.image_revisions.get(expected_image, revision) == revision, 'IMAGE_REVISION_CHANGED')
        self.image_revisions[expected_image] = revision
        return revision

    def verified_spec(self, container, service):
        require(service in SERVICES, 'INVALID_SERVICE_SCOPE')
        revision = self.image_revisions.get(container['Image'])
        require(revision is not None, 'UNVERIFIED_SERVICE_IMAGE')
        labels = container['Config'].get('Labels') or {}
        require(labels.get(OCI_REVISION) == revision, 'CONTAINER_REVISION_MISMATCH')
        identity, replacement = container['Id'], labels.get(COMPOSE_REPLACE)
        require(SHA.fullmatch(identity) and
                (replacement is None or isinstance(replacement, str) and len(replacement) <= 256),
                'CONTAINER_REPLACEMENT_MISMATCH')
        if identity not in self.replacement_proofs:
            if service in self.service_ids:
                # Older Compose versions name the actual predecessor ID; 5.4
                # writes a logical service/replica reference instead. That name
                # is never independent provenance. Accept it only for the exact
                # new ID observed after our own bounded start, tied to its
                # actually observed/stopped predecessor and requested image.
                predecessor = self.service_ids[service]
                created = self.created_predecessors.get(identity)
                require(predecessor in self.stopped_ids and
                        (replacement == predecessor or
                         replacement == service + '-1' and
                         created == (service, predecessor, container['Image'])),
                        'CONTAINER_REPLACEMENT_MISMATCH')
            else:
                # Older Compose may have left a literal backend-1/runner-1.
                # Pin that exact initial value only to this original old-image
                # container ID. New replacements must name an actually stopped
                # predecessor; a logical name alone is never new proof.
                require(container['Image'] == self.plan.old_image, 'INITIAL_SERVICE_IMAGE_CHANGED')
            self.replacement_proofs[identity] = replacement
        require(replacement == self.replacement_proofs[identity], 'CONTAINER_REPLACEMENT_MISMATCH')
        self.service_ids[service] = identity
        return service_spec(container, verified_revision=revision,
                            verified_replacement=self.replacement_proofs[identity])

    def preflight(self):
        require(sys.platform == 'linux', 'LINUX_HOST_REQUIRED')
        zero_offset_clock(clock_domain())
        old, new = self.config(False), self.config(True)
        image_only_configs(old, new)
        for service in SERVICES:
            image = old['services'][service]['image']
            self.image_revision(image, self.plan.old_image)
            require(new['services'][service]['image'] == self.plan.candidate_image,
                    'CANDIDATE_COMPOSE_IMAGE_NOT_PINNED')
        self.image_revision(self.plan.candidate_image, self.plan.candidate_image,
                            expected_revision=self.plan.candidate_head)
        require(Path(self.plan.manifest_file).stat().st_size <= 2 * 1024**2, 'MANIFEST_TOO_LARGE')
        manifest = json.loads(Path(self.plan.manifest_file).read_text())
        require(manifest['head'] == self.plan.candidate_head and
                manifest['image'] == self.plan.candidate_image,
                'CANDIDATE_MANIFEST_IDENTITY_MISMATCH')
        files = manifest['files']
        require(isinstance(files, list) and 1 <= len(files) <= 8192 and all(
            set(f) == {'path', 'sha256'} and isinstance(f['path'], str) and
            re.fullmatch(r'app/[A-Za-z0-9_./@-]+', f['path']) and
            '..' not in Path(f['path']).parts and SHA.fullmatch(f['sha256']) for f in files),
            'COMPLETE_APP_MANIFEST_REQUIRED')
        require(len({f['path'] for f in files}) == len(files), 'DUPLICATE_MANIFEST_PATH')
        # This candidate command has neither network nor writable application
        # filesystem. It imports no app, touches no credentials and hashes the
        # complete app tree, including non-Python verification data/templates.
        code = ("import pathlib,hashlib,json; r=pathlib.Path('/app'); "
                "print(json.dumps([{'path':p.relative_to(r).as_posix(),"
                "'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} "
                "for p in sorted((r/'app').rglob('*')) if p.is_file() "
                "and '__pycache__' not in p.parts]))")
        actual = json.loads(self.run(['docker', 'run', '--rm', '--pull', 'never',
            '--network', 'none', '--read-only', '--entrypoint', 'python',
            self.plan.candidate_image, '-c', code], self.plan.probe_seconds))
        require(sorted(actual, key=lambda f: f['path']) == sorted(files, key=lambda f: f['path']),
                'CANDIDATE_APP_CONTENT_MISMATCH')
        self.compose_hashes = {False: digest(old), True: digest(new)}
        containers = self.containers()
        self.require_services(self.plan.old_image, running=SERVICES)
        self.baseline = {s: self.verified_spec(containers[s], s) for s in SERVICES}

    def containers(self, deadline=None):
        result = {}
        def remaining():
            seconds = 10 if deadline is None else min(10, deadline - time.monotonic())
            require(seconds > 0, 'COMMAND_TIMEOUT')
            return seconds
        for service in SERVICES:
            # Compose ps --all includes one-offs. In particular the candidate
            # gate is a backend one-off and must never be selected as a service.
            ids = self.run(['docker', 'ps', '--all', '--filter',
                'label=com.docker.compose.project=' + self.plan.project,
                '--filter', 'label=com.docker.compose.service=' + service,
                '--filter', 'label=com.docker.compose.oneoff=False',
                '--format', '{{.ID}}'], remaining()).split()
            require(len(ids) <= 1, 'SERVICE_REPLICAS_UNEXPECTED')
            if ids:
                container = json.loads(self.run(['docker', 'inspect', ids[0]], remaining()))[0]
                labels = container['Config'].get('Labels', {})
                require(labels.get('com.docker.compose.project') == self.plan.project and
                        labels.get('com.docker.compose.service') == service and
                        labels.get('com.docker.compose.oneoff') == 'False', 'SERVICE_LABELS_CHANGED')
                result[service] = container
        return result

    def states(self, deadline=None):
        containers = self.containers(deadline)
        return self.state_values(containers)

    @staticmethod
    def state_values(containers):
        return {s: {'image': containers[s]['Image'] if s in containers else None,
                    'running': s in containers and containers[s]['State']['Running'] and
                               not containers[s]['State'].get('Paused', False) and
                               not containers[s]['State'].get('Restarting', False),
                    'stopped': s not in containers or containers[s]['State']['Status'] in ('created', 'exited'),
                    'status': containers[s]['State']['Status'] if s in containers else 'absent'}
                for s in SERVICES}

    def require_services(self, image, running):
        containers = self.containers()
        states = self.state_values(containers)
        require(all(states[s]['image'] == image and
                    (states[s]['running'] if s in running else states[s]['stopped'])
                    for s in SERVICES if s in running or not running), 'SERVICE_IMAGE_OR_STATE_CHANGED')
        for service, container in containers.items():
            runtime_spec = self.verified_spec(container, service)
            if service in self.baseline:
                require(runtime_spec == self.baseline[service], 'RUNTIME_CONFIG_CHANGED')

    def gate(self):
        p = self.plan
        child = Child(self.compose(True) + ['run', '--rm', '--no-deps', '--pull', 'never',
            '--interactive', '--no-TTY', '--name', 'r01-gate-' + uuid4().hex,
            'backend', 'python', '-m', 'app.scheduling.process_drain',
            '--expect-task-fingerprint', p.task_fingerprint,
            '--drain-seconds', str(p.drain_seconds), '--hold-seconds', str(p.hold_seconds)],
            p.project_directory, interactive=True)
        return Gate(child, p)

    def probe(self, captured, resources):
        request = {'owned_task_id': self.plan.owned_task_id, 'captured': captured,
                   'resources': resources, 'scratch_bytes': self.plan.scratch_bytes}
        encoded = base64.b64encode(json.dumps(request).encode()).decode()
        output = self.run(self.compose(True) + ['run', '--rm', '--no-deps', '--pull', 'never',
            '--no-TTY', '-v', str(Path(__file__).resolve()) + ':/opt/r01_service_switch.py:ro',
            'backend', 'python', '/opt/r01_service_switch.py', '--probe', encoded], self.plan.probe_seconds)
        result = json.loads(output)
        require(result.get('ok') is True, 'READONLY_PROBE_REFUSED')
        return result['snapshot']

    def stop(self, service):
        end = time.monotonic() + self.plan.stop_seconds
        container = self.containers(end).get(service)
        require(container is not None and container['State']['Running'] and
                not container['State'].get('Paused') and not container['State'].get('Restarting') and
                container['Image'] in (self.plan.old_image, self.plan.candidate_image),
                'STOP_TARGET_CHANGED')
        self.verified_spec(container, service)
        # Explicit SIGTERM only. Docker's kill-with-signal marks manual stop and
        # does not run stop's grace-timeout -> SIGKILL path. Never omit --signal.
        # Compose kill also includes backend one-offs, so use the exact already
        # verified non-one-off service ID; the admission gate must remain alive.
        self.run(['docker', 'kill', '--signal', 'SIGTERM', container['Id']],
                 min(10, max(.01, end - time.monotonic())))
        while time.monotonic() < end:
            remaining = self.containers(end).get(service)
            require(remaining is None or remaining['Id'] == container['Id'], 'STOP_TARGET_CHANGED')
            if remaining is None or self.state_values({service: remaining})[service]['stopped']:
                self.stopped_ids.add(container['Id'])
                return
            time.sleep(.1)
        raise Refused('GRACEFUL_STOP_NOT_COMPLETE')

    def start(self, image, services):
        require(bool(services) and set(services) <= set(SERVICES), 'INVALID_SERVICE_SCOPE')
        candidate = image == self.plan.candidate_image
        require(digest(self.config(candidate)) == self.compose_hashes[candidate], 'COMPOSE_CHANGED_DURING_SWITCH')
        # Only stopped/absent targets may be recreated. A bounded recovery must
        # not let Compose implicitly stop a surviving candidate with new work.
        containers = self.containers()
        states = self.state_values(containers)
        require(all(states[s]['stopped'] for s in services), 'START_TARGET_NOT_STOPPED')
        for service in services:
            if service in containers:
                # A partial start can exit by itself, or SIGTERM can finish
                # after stop's finite wait refused. Prove its actual stopped
                # immutable image/revision/ID here before a restoration can
                # use it as the replacement predecessor. Running or forged
                # targets cannot gain this proof through a failed stop call.
                self.verified_spec(containers[service], service)
                self.stopped_ids.add(containers[service]['Id'])
        predecessors = {s: self.service_ids.get(s) for s in services}
        started_deadline = time.monotonic() + self.plan.start_seconds + 15
        try:
            self.run(self.compose(candidate) + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                '--timeout', '-1', '--wait', '--wait-timeout', str(self.plan.start_seconds), *services],
                self.plan.start_seconds + 15)
        finally:
            # A partial up can create a real candidate before health refuses.
            # Capture that exact observed ID even on failure so one bounded
            # recovery can inspect it. This read grants no stop permission;
            # all image/revision, runtime, idle and deadline guards still apply.
            # Never mint this receipt from arbitrary later verified_spec calls.
            after = self.containers(deadline=started_deadline)
            for service, predecessor in predecessors.items():
                current = after.get(service)
                if current is None or current['Id'] == predecessor:
                    continue
                require(predecessor in self.stopped_ids and
                        SHA.fullmatch(current['Id']) and current['Image'] == image,
                        'CONTAINER_REPLACEMENT_MISMATCH')
                self.created_predecessors[current['Id']] = (service, predecessor, image)

    def verify_candidate(self):
        self.require_services(self.plan.candidate_image, running=SERVICES)
        for service, container in self.containers().items():
            require(self.verified_spec(container, service) == self.baseline[service], 'RUNTIME_CONFIG_CHANGED')
            require(service != 'backend' or container['State'].get('Health', {}).get('Status') == 'healthy',
                    'BACKEND_NOT_HEALTHY')


def database_snapshot(engine, owned_task_id, captured):
    """Finite read-only metadata query; secret-bearing rows leave only hashes."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    require(str(UUID(owned_task_id)) == owned_task_id and isinstance(captured, list) and
            len(captured) <= 1024 and all(str(UUID(x)) == x for x in captured), 'INVALID_CAPTURE_SCOPE')
    with Session(engine) as session:
        session.execute(text('SET TRANSACTION READ ONLY'))
        session.execute(text("SET LOCAL statement_timeout='5s'"))
        session.execute(text("SET LOCAL lock_timeout='1s'"))
        rows = session.execute(text('SELECT row_to_json(t) AS row FROM '
                                    '(SELECT * FROM scheduled_tasks ORDER BY id LIMIT 179) t')).scalars().all()
        require(len(rows) == 178, 'TASK_COUNT_CHANGED')
        owned = [r for r in rows if r['id'] == owned_task_id]
        require(len(owned) == 1, 'OWNED_TASK_MISSING')
        simple = [{k: r[k] for k in ('id', 'state', 'version')} for r in rows]
        configs = session.execute(text('SELECT row_to_json(t) FROM '
            '(SELECT * FROM data_source_configs ORDER BY key LIMIT 65) t')).scalars().all()
        require(len(configs) <= 64, 'CONFIG_QUERY_BOUND')
        activity = dict(session.execute(text("""SELECT
            (SELECT count(*) FROM task_runs WHERE status='running') AS accepted_runs,
            (SELECT count(*) FROM backtest_runs WHERE finished_at IS NULL) AS backtests,
            (SELECT count(*) FROM task_runs WHERE status IN ('queued','running')
             AND task_type LIKE 'data_store.%') AS processors""")).mappings().one())
        records = session.execute(text("""SELECT row_to_json(r) FROM task_runs r
            WHERE status IN ('queued','running') OR id::text = ANY(:ids)
            ORDER BY id LIMIT 1025"""), {'ids': captured}).scalars().all()
        require(len(records) <= 1024, 'RUN_CAPTURE_BOUND')
        immutable = ('id', 'task_id', 'task_version', 'task_type', 'trigger_type',
                     'parameters', 'parameter_version', 'priority', 'scheduled_at', 'created_at')
        result = {'task_count': len(rows), 'task_fingerprint': digest(simple),
            'original_fingerprint': digest([r for r in simple if r['id'] != owned_task_id]),
            'owned_definition_hash': digest(owned[0]), 'definitions_hash': digest(rows),
            'source_configs_hash': digest(configs), 'activity': activity,
            'records': [{'id': r['id'], 'record_hash': digest(r),
                         'identity_hash': digest({k: r.get(k) for k in immutable})} for r in records]}
        session.rollback()
        return result


def storage_snapshot(root, scratch_bytes):
    """Inspect an inactive existing lock namespace and charged seals in place.

    Never construct CurrentStore/Budget, create locks, recover SQLite journals,
    rewrite idle quota or sweep files. Existing flock files are opened read-only
    and held nonblocking during metadata inspection. A pending quota is valid
    only for an unambiguous sealed pipeline spool within its declared charge.
    """
    from app.data_store.locking import _directory, _filesystem_name
    with ExitStack() as stack:
        root_fd = _directory(Path(root))
        stack.callback(os.close, root_fd)
        require(_filesystem_name(root_fd) in ('ext4', 'xfs', 'btrfs'), 'UNSUPPORTED_FILESYSTEM')
        def directory(name, parent):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            info = os.fstat(fd)
            require(info.st_dev == os.fstat(root_fd).st_dev and not info.st_mode & stat.S_IWOTH,
                    'UNSAFE_STORAGE_PATH')
            stack.callback(os.close, fd)
            return fd
        def regular(name, parent):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                    info.st_dev == os.fstat(root_fd).st_dev and not info.st_mode & stat.S_IWOTH,
                    'UNSAFE_STORAGE_PATH')
            return fd, info
        lock_fd = directory('.locks', root_fd)
        names = sorted(os.listdir(lock_fd))
        require(len(names) <= 4096, 'LOCK_QUERY_BOUND')
        for name in names:
            require(re.fullmatch(r'[0-9a-f]{64}\.[a-z0-9-]+\.lock', name), 'UNKNOWN_LOCK_PATH')
            fd, _ = regular(name, lock_fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused('HELD_STORAGE_LOCK') from None
        scratch_fd = directory('.scratch', root_fd)
        slots = sorted(os.listdir(scratch_fd))
        require(len(slots) <= 128 and all(re.fullmatch(r'[0-9]{3}', s) and int(s) < 128 for s in slots),
                'SCRATCH_QUERY_BOUND')
        retained = []
        owners = set()
        for slot in slots:
            slot_fd = directory(slot, scratch_fd)
            contents = set(os.listdir(slot_fd))
            require(contents <= {'quota', 'spill'} and 'spill' in contents, 'UNKNOWN_SCRATCH_CONTENT')
            spill_fd = directory('spill', slot_fd)
            spill = set(os.listdir(spill_fd))
            if 'quota' not in contents:
                require(not spill, 'UNCHARGED_SCRATCH_CONTENT')
                continue
            quota_fd, info = regular('quota', slot_fd)
            require(info.st_size <= 256, 'INVALID_QUOTA')
            quota = json.loads(os.read(quota_fd, 257))
            require(set(quota) <= {'kind', 'bytes', 'pending'} and
                    quota.get('kind') in ('read', 'write') and type(quota.get('bytes')) is int and
                    0 < quota['bytes'] <= scratch_bytes, 'INVALID_QUOTA')
            owner = quota.get('pending')
            if owner is None:
                require(not spill, 'UNSEALED_SCRATCH_CONTENT')
                continue
            require(quota['kind'] == 'read' and isinstance(owner, str) and
                    re.fullmatch(r'pipeline\.E[0-9]{2}(?:\.selected)?', owner) and
                    owner not in owners and spill == {'lfd02-merge.sqlite'}, 'UNKNOWN_PENDING_QUOTA')
            owners.add(owner)
            spool_fd, spool_info = regular('lfd02-merge.sqlite', spill_fd)
            require(spool_info.st_size <= quota['bytes'], 'SPOOL_EXCEEDS_QUOTA')
            with closing(sqlite3.connect(f'file:/proc/self/fd/{spool_fd}?mode=ro&immutable=1', uri=True)) as db:
                db.execute('PRAGMA query_only=ON')
                db.execute('PRAGMA cache_size=-4096')
                row = db.execute('SELECT CASE WHEN length(body)<=65536 THEN body END '
                                 'FROM progress WHERE id=1').fetchone()
                progress = json.loads(row[0]) if row and row[0] else {}
            require(isinstance(progress, dict) and progress.get('scan_complete') is True and
                    isinstance(progress.get('identity'), str) and SHA.fullmatch(progress['identity']) and
                    isinstance(progress.get('source_selection'), str) and SHA.fullmatch(progress['source_selection']),
                    'INVALID_SEALED_CHECKPOINT')
            retained.append({'slot': slot, 'owner': owner, 'quota_bytes': quota['bytes'],
                'spool_bytes': spool_info.st_size, 'spool_inode': spool_info.st_ino,
                'spool_mtime_ns': spool_info.st_mtime_ns, 'progress_hash': digest(progress)})
        require(sum(r['spool_bytes'] for r in retained) <= scratch_bytes, 'SCRATCH_BUDGET_EXCEEDED')
        return {'held_locks': 0, 'lock_files': len(names), 'retained_seals': retained}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--probe')
    args = parser.parse_args(argv)
    if args.probe:
        try:
            require(args.plan is None and args.evidence is None and len(args.probe) <= 1024**2,
                    'INVALID_PROBE_REQUEST')
            sys.path.insert(0, '/app')
            from app.db.session import get_engine
            from app.core.config import get_settings
            request = json.loads(base64.b64decode(args.probe, validate=True))
            snapshot = database_snapshot(get_engine(), request['owned_task_id'], request['captured'])
            if request['resources']:
                snapshot['resources'] = storage_snapshot(get_settings().data_store_root, request['scratch_bytes'])
            print(json.dumps({'ok': True, 'snapshot': snapshot}), flush=True)
            return 0
        except Exception:
            print(json.dumps({'ok': False, 'reason': 'READONLY_PROBE_REFUSED'}), flush=True)
            return 2
    if args.plan is None or args.evidence is None:
        parser.error('Require a reviewed --plan and a new --evidence path')
    require(args.plan.stat().st_size <= 1024**2, 'PLAN_TOO_LARGE')
    plan = Plan(**json.loads(args.plan.read_text()))
    # Exclusive creation protects previous evidence. Output contains only
    # fixed messages, image/head hashes, record hashes and safe resource facts.
    with args.evidence.open('x', encoding='utf-8') as evidence:
        os.chmod(args.evidence, 0o600)
        def event(stage, message, **fields):
            value = json.dumps({'stage': stage, 'message': message,
                'monotonic': time.monotonic(), 'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                **fields}, ensure_ascii=False)
            evidence.write(value + '\n')
            evidence.flush()
            os.fsync(evidence.fileno())
            print(value, flush=True)
        return switch(plan, DockerHost(plan), event)


if __name__ == '__main__':
    raise SystemExit(main())
