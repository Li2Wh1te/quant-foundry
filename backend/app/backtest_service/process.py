"""Trusted Docker API and daemon-host mapping. Workers never receive this module."""

from __future__ import annotations

import http.client
import json
import re
import socket
from pathlib import Path

from .runtime_contracts import RuntimeFault, WorkerLimits

LABEL = "io.quantfoundry.s3.worker"


class _Connection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class DockerDaemon:
    def __init__(self, socket_path="/var/run/docker.sock"):
        if socket_path != "/var/run/docker.sock":
            raise RuntimeFault(
                "CAPABILITY_UNAVAILABLE", "只允许部署已授权的本机Docker端点"
            )
        self.socket_path = socket_path

    def request(self, method, path, body=None, *, missing=False, stopped=False):
        connection = _Connection(self.socket_path)
        try:
            data = (
                None
                if body is None
                else json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
            )
            if data is not None and len(data) > 1024 * 1024:
                raise RuntimeFault("RESOURCE_LIMIT", "Docker控制请求过大")
            connection.request(
                method, "/v1.45" + path, data, {"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            encoded = response.read(1024 * 1024 + 1)
            if response.status == 404 and missing:
                return None
            if response.status == 409 and stopped:
                return None
            if response.status >= 300 or len(encoded) > 1024 * 1024:
                raise RuntimeFault(
                    "CAPABILITY_UNAVAILABLE", "Docker操作被拒绝；未降级执行策略"
                )
            return json.loads(encoded) if encoded else {}
        except (OSError, ValueError, http.client.HTTPException):
            raise RuntimeFault(
                "CAPABILITY_UNAVAILABLE", "Docker隔离不可用；未降级执行策略"
            ) from None
        finally:
            connection.close()

    def preflight(self, image):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise RuntimeFault(
                "CAPABILITY_UNAVAILABLE", "worker必须使用预构建固定镜像ID"
            )
        info = self.request("GET", "/info")
        if (
            info.get("OSType") != "linux"
            or not all(
                info.get(x)
                for x in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit")
            )
            or not any(
                x.startswith("name=seccomp") for x in info.get("SecurityOptions", [])
            )
        ):
            raise RuntimeFault(
                "CAPABILITY_UNAVAILABLE", "daemon不能落实所需资源与seccomp隔离"
            )
        manifest = self.request("GET", "/images/" + image + "/json")
        if manifest["Config"].get("Volumes"):
            raise RuntimeFault("CAPABILITY_UNAVAILABLE", "worker镜像不能声明额外挂载卷")

    def inspect(self, name):
        return self.request("GET", "/containers/" + name + "/json", missing=True)

    def stop_remove(self, name, claim):
        row = self.inspect(name)
        if row is None:
            return
        expected = labels(claim)
        if any(row["Config"]["Labels"].get(k) != v for k, v in expected.items()):
            raise RuntimeFault("STALE_CLAIM", "拒绝操作不属于本claim的容器")
        if row["State"]["Running"]:
            self.request(
                "POST",
                "/containers/" + name + "/kill?signal=KILL",
                missing=True,
                stopped=True,
            )
        row = self.inspect(name)
        if row is not None and row["State"]["Running"]:
            raise RuntimeFault("CAPABILITY_UNAVAILABLE", "旧worker尚未确认终止")
        self.request("DELETE", "/containers/" + name, missing=True)


def labels(claim):
    return {
        LABEL: "1",
        LABEL + ".run": claim.run_id,
        LABEL + ".token": claim.token,
        LABEL + ".fence": str(claim.fence),
    }


def container_name(claim):
    import hashlib

    return "qf-s3-" + hashlib.sha256(claim.run_id.encode()).hexdigest()[:32]


def attempt_name(claim):
    import hashlib

    return (
        "qf-s3-"
        + hashlib.sha256(
            (claim.run_id + ":" + str(claim.fence) + ":" + claim.token).encode()
        ).hexdigest()[:32]
    )


class WorkRoot:
    def __init__(self, runner_path, daemon_host_path):
        self.runner = Path(runner_path)
        self.host = Path(daemon_host_path)
        for root in (self.runner, self.host):
            if (
                not root.is_absolute()
                or str(root) == "/"
                or ".." in root.parts
                or any(c in str(root) for c in (",", ":", "\n", "\x00"))
            ):
                raise RuntimeFault("INVALID_CONTRACT", "工作根映射无效")
        self._identity = self.runner.stat()
        if self.runner.is_symlink() or not self.runner.is_dir():
            raise RuntimeFault("INVALID_CONTRACT", "工作根必须为已有可信目录")

    def host_path(self, relative):
        self.check()
        relative = Path(relative)
        if (
            relative.is_absolute()
            or any(p in ("..", ".") for p in relative.parts)
            or len(relative.parts) != 2
        ):
            raise RuntimeFault("INVALID_CONTRACT", "只允许本attempt代码或IPC子目录")
        path = self.runner
        for part in relative.parts:
            path /= part
            if path.is_symlink() or not path.is_dir():
                raise RuntimeFault("INVALID_CONTRACT", "工作目录替换或路径注入被拒绝")
        return str(self.host / relative)

    def check(self):
        current = self.runner.stat()
        if self.runner.is_symlink() or (current.st_dev, current.st_ino) != (
            self._identity.st_dev,
            self._identity.st_ino,
        ):
            raise RuntimeFault("INVALID_CONTRACT", "可信工作根身份发生变化")


class WorkerProcess:
    def __init__(self, daemon, root, image):
        self.daemon, self.root, self.image = daemon, root, image
        daemon.preflight(image)

    def start(self, claim, relative, limits: WorkerLimits):
        name = container_name(claim)
        mounts = [
            dict(
                Type="bind",
                Source=self.root.host_path(Path(relative) / p),
                Target="/run/qf/" + p,
                ReadOnly=True,
                BindOptions=dict(Propagation="rprivate", NonRecursive=True),
            )
            for p in ("code", "ipc")
        ]
        body = dict(
            Image=self.image,
            User="65532:65532",
            WorkingDir="/run/qf/code",
            Entrypoint=["python", "-I", "-m", "quantfoundry._worker"],
            Cmd=["--supervised"],
            Env=[
                "PYTHONHASHSEED=0",
                "OPENBLAS_NUM_THREADS=1",
                "OMP_NUM_THREADS=1",
                "MKL_NUM_THREADS=1",
                "NUMEXPR_NUM_THREADS=1",
                "VECLIB_MAXIMUM_THREADS=1",
                "PYTHONDONTWRITEBYTECODE=1",
            ],
            Labels=labels(claim),
            NetworkDisabled=True,
            HostConfig=dict(
                NetworkMode="none",
                ReadonlyRootfs=True,
                CapDrop=["ALL"],
                SecurityOpt=["no-new-privileges"],
                PidsLimit=limits.pids,
                Memory=limits.memory_bytes,
                MemorySwap=limits.memory_bytes,
                NanoCpus=limits.cpu_millis * 1_000_000,
                ShmSize=1024 * 1024,
                IpcMode="private",
                CgroupnsMode="private",
                Mounts=mounts,
                Tmpfs={
                    "/tmp": f"rw,noexec,nosuid,nodev,size={limits.tmp_bytes},mode=1777"
                },
                Ulimits=[
                    dict(Name=n, Soft=v, Hard=v)
                    for n, v in [
                        ("nofile", 128),
                        ("fsize", limits.file_bytes),
                        ("core", 0),
                        ("cpu", limits.wall_seconds),
                    ]
                ],
                LogConfig=dict(Type="none"),
            ),
        )
        created = self.daemon.request("POST", "/containers/create?name=" + name, body)
        try:
            self.daemon.request("POST", "/containers/" + created["Id"] + "/start")
        except BaseException:
            self.daemon.stop_remove(name, claim)
            raise
        return name
