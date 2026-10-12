"""Start/stop the actual release entrypoints against a new private empty DB.

No published ports, supplier calls, production configuration or shared volume.
Every resource is created here with an exact name and removed by that name.
"""

import argparse
import json
from pathlib import Path
import os
import subprocess
import tempfile
import time
from uuid import uuid4


def docker(*args, timeout=120, allowed_failure=False):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode and not allowed_failure:
        raise RuntimeError(
            "private release startup command failed: " + result.stderr[-4000:]
        )
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    assert manifest["images_installed_verified"]
    image = manifest["images"]["backend"]["id"]
    suffix = uuid4().hex[:12]
    prefix = "qf-s3-release-test-" + suffix
    containers = []
    volumes = []
    with tempfile.TemporaryDirectory(prefix="qf-s3-release-start-") as directory:
        socket_root = Path(directory) / "pg-socket"
        socket_root.mkdir(mode=0o777)
        os.chmod(
            socket_root, 0o777
        )  # Only this freshly created private socket directory.
        pg = prefix + "-pg"
        api = prefix + "-api"
        runner = prefix + "-runner"
        label = "io.quantfoundry.s3.private-release-test=" + suffix
        env = dict(
            QF_ENVIRONMENT="test",
            QF_API_TOKEN="a" * 64,
            QF_CURSOR_SIGNING_KEY="b" * 64,
            QF_DATABASE_HOST="/qf-db",
            QF_DATABASE_PORT="5432",
            QF_DATABASE_USER="postgres",
            QF_DATABASE_PASSWORD="private-release-test",
            QF_DATABASE_NAME="qf_release_test",
            QF_DATA_SOURCE_ENCRYPTION_KEY="e" * 64,
            QF_SCHEDULER_ENABLED="false",
            QF_LOG_DIR="/tmp/logs",
            QF_SERVER_HOST="127.0.0.1",
            QF_S3_ACCEPT_NEW_RUNS="false",
            QF_S3_WORKER_IMAGE=manifest["images"]["worker"]["id"],
        )
        env_args = [v for k, value in env.items() for v in ("--env", k + "=" + value)]
        base = [
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            "--pids-limit",
            "64",
            "--label",
            label,
            "--mount",
            f"type=bind,src={socket_root},dst=/qf-db,readonly",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777",
            *env_args,
        ]
        try:
            # Reuses the standard test dependency already used by required CI.
            docker("pull", "postgres:17")
            docker(
                "run",
                "-d",
                "--name",
                pg,
                "--network",
                "none",
                "--cpus",
                "1",
                "--memory",
                "1g",
                "--memory-swap",
                "1g",
                "--label",
                label,
                "--mount",
                f"type=bind,src={socket_root},dst=/var/run/postgresql",
                "--env",
                "POSTGRES_DB=qf_release_test",
                "--env",
                "POSTGRES_PASSWORD=private-release-test",
                "postgres:17",
            )
            containers.append(pg)
            for _ in range(60):
                if docker(
                    "exec",
                    pg,
                    "pg_isready",
                    "-U",
                    "postgres",
                    "-d",
                    "qf_release_test",
                    allowed_failure=True,
                ).endswith("accepting connections"):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("private DB did not become ready")
            migration = prefix + "-migration"
            containers.append(migration)
            docker(
                "run",
                "--name",
                migration,
                *base,
                "--entrypoint",
                "python",
                image,
                "-m",
                "alembic",
                "-c",
                "/app/alembic.ini",
                "upgrade",
                "head",
            )
            # These commands have completed and their effects live in the
            # private DB/volumes. Release their private container roots before
            # starting long-lived entrypoints (also bounded on VFS daemons).
            docker("rm", migration)
            containers.remove(migration)
            for name in (prefix + "-work", prefix + "-results"):
                docker("volume", "create", "--label", label, name)
                volumes.append(name)
            work_host = json.loads(docker("volume", "inspect", volumes[0]))[0][
                "Mountpoint"
            ]
            # Empty volume ownership is initialized from this release image,
            # with the same UID as the image's actual API/runner user.
            containers.append(prefix + "-volume-init")
            docker(
                "run",
                "--name",
                containers[-1],
                *base,
                "--mount",
                f"type=volume,src={volumes[0]},dst=/qf-work",
                "--mount",
                f"type=volume,src={volumes[1]},dst=/app/data/s3-results",
                "--entrypoint",
                "python",
                image,
                "-c",
                'from pathlib import Path; assert Path("/qf-work").stat().st_uid==65532; assert Path("/app/data/s3-results").stat().st_uid==65532',
            )
            initialized = containers[-1]
            docker("rm", initialized)
            containers.remove(initialized)
            result_mount = [
                "--mount",
                f"type=volume,src={volumes[1]},dst=/app/data/s3-results",
            ]
            docker("run", "-d", "--name", api, *base, *result_mount, image)
            containers.append(api)
            ready = 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/readyz",timeout=2)'
            for _ in range(60):
                state = json.loads(docker("inspect", api))[0]["State"]
                if not state["Running"]:
                    raise RuntimeError("private release API stopped before readiness")
                attempt = subprocess.run(
                    ["docker", "exec", api, "python", "-c", ready],
                    capture_output=True,
                    timeout=10,
                )
                if attempt.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("private release API readiness budget exhausted")
            capabilities = json.loads(
                docker(
                    "exec",
                    api,
                    "python",
                    "-c",
                    'import json,urllib.request; r=urllib.request.Request("http://127.0.0.1:8000/api/admin/backtests/capabilities",headers={"Authorization":"Bearer "+"a"*64}); print(urllib.request.urlopen(r,timeout=3).read().decode())',
                )
            )
            assert capabilities["engine"]["installed"]
            assert all(not x["available"] for x in capabilities["actual_data"])
            docker(
                "run",
                "-d",
                "--name",
                runner,
                *base,
                *result_mount,
                "--group-add",
                str(Path("/var/run/docker.sock").stat().st_gid),
                "--mount",
                "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
                "--mount",
                f"type=volume,src={volumes[0]},dst=/qf-work",
                "--env",
                "QF_S3_WORK_ROOT=/qf-work",
                "--env",
                "QF_S3_DAEMON_WORK_ROOT=" + work_host,
                "--env",
                "QF_S3_WORKER_IMAGE=" + manifest["images"]["worker"]["id"],
                image,
                "python",
                "-m",
                "app.runner",
            )
            containers.append(runner)
            time.sleep(2)
            assert json.loads(docker("inspect", runner))[0]["State"]["Running"]
            docker("exec", api, "python", "-c",
                'from pathlib import Path; from app.core.config import get_settings; '
                'from app.backtest_service.runtime_readiness import check_runtime_ready; '
                's=get_settings(); assert not Path("/var/run/docker.sock").exists(); '
                'check_runtime_ready(s.s3_result_root,s.cursor_signing_key.get_secret_value().encode(),s.s3_worker_image)')
            stopped = {}
            for name in (runner, api):
                docker("stop", "--time", "15", name)
                stopped[name] = int(docker("wait", name))
                assert stopped[name] in (0, 143), stopped
            manifest["entrypoint_smoke"] = dict(
                api_ready=True,
                runner_started=True,
                web_runtime_preflight_without_docker=True,
                stopped_exit_codes=stopped,
                admission_paused=True,
                formal_data_available=False,
                empty_database_migrated=True,
                production_executed=False,
            )
            args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
            print(json.dumps(manifest["entrypoint_smoke"]))
        finally:
            docker(
                "exec",
                "--user",
                "postgres",
                pg,
                "rm",
                "-f",
                "--",
                "/var/run/postgresql/.s.PGSQL.5432",
                "/var/run/postgresql/.s.PGSQL.5432.lock",
                allowed_failure=True,
            )
            for name in reversed(containers):
                docker("rm", "-fv", name, allowed_failure=True)
            for name in reversed(volumes):
                docker("volume", "rm", name, allowed_failure=True)


if __name__ == "__main__":
    main()
