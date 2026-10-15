"""D13 gaps only: compose compatibility, never a deployment or a provider."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory


def check_compose(repo, images, output):
    records = []
    with TemporaryDirectory(prefix='qf-s2-compose-') as directory:
        env_file = Path(directory) / '.env'
        values = dict(QF_DATABASE_USER='postgres', QF_DATABASE_PASSWORD='isolated-only',
            QF_DATABASE_NAME='qf_s2_market_test', QF_DATABASE_PORT='55432',
            QF_S2_BACKEND_IMAGE=images['backend']['id'], QF_S2_FRONTEND_IMAGE=images['frontend']['id'],
            QF_S2_TRUSTED_UID='999', QF_S2_TRUSTED_GID='999',
            QF_S3_BACKEND_IMAGE='sha256:' + '1' * 64, QF_S3_WORKER_IMAGE='sha256:' + '2' * 64,
            QF_S3_TRUSTED_UID='999', QF_S3_TRUSTED_GID='999', QF_S3_DAEMON_SOCKET_GID='123',
            QF_S3_HOST_RESULT_ROOT=directory + '/results', QF_S3_HOST_WORK_ROOT=directory + '/work',
            QF_S3_WORKER_LIMITS='{"memory_bytes":536870912}', QF_S3_GROUP_BUDGET='null')
        for accepted in ('true', 'false'):
            values['QF_S3_ACCEPT_NEW_RUNS'] = accepted
            env_file.write_text(''.join(key + '=' + value + '\n' for key, value in values.items()))
            for s3 in (False, True):
                files = ['compose.yaml'] + (['ops/s3-d18/compose.s3.yaml'] if s3 else [])
                def render(paths):
                    command = ['docker', 'compose', '--project-directory', directory, '--env-file', str(env_file)]
                    for path in paths:
                        command += ['-f', str(repo / path)]
                    result = subprocess.run(command + ['config', '--no-env-resolution', '--format', 'json'],
                        env={**os.environ, **values}, capture_output=True, text=True, timeout=30)
                    assert result.returncode == 0, result.stderr[-2000:]
                    return json.loads(result.stdout)
                before = render(files)
                after = render(files + ['ops/s2-batch2/compose.market-release.yaml'])
                expected = deepcopy(before)
                expected['services']['backend'].update(image=images['backend']['id'], pull_policy='never', user='999:999')
                expected['services']['frontend'].update(image=images['frontend']['id'], pull_policy='never')
                assert after == expected, 'S2 overlay changed existing configuration beyond the paired images/verified UID'
                records.append(dict(s3_overlay=s3, accepting=accepted, preserved=True))
    output.write_text(json.dumps(records, indent=2) + '\n')
    return records
