"""Web cannot acquire Docker authority through the current readiness check."""
import hashlib
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.backtest_service import runtime_readiness as ready
from app.backtest_service.runtime_contracts import RuntimeFault
from app.backtest_service.service import FormalProvider

KEY = b'explicit-isolated-runtime-readiness-key'
IMAGE = 'sha256:' + 'a'*64


def process(*, refused=False):
    calls = []
    def checked(image):
        calls.append(image)
        if refused:
            raise RuntimeFault('CAPABILITY_UNAVAILABLE','explicit isolated daemon refusal')
    return SimpleNamespace(image=IMAGE, root=SimpleNamespace(check=lambda:None),
        daemon=SimpleNamespace(preflight=checked),calls=calls)


def resign(root, update):
    path=root/ready.NAME
    value=json.loads(path.read_text());update(value['payload'])
    raw=json.dumps(value['payload'],sort_keys=True,separators=(',',':')).encode()
    value['signature']=hmac.new(KEY,raw,hashlib.sha256).hexdigest()
    path.write_text(json.dumps(value))


def test_missing_current_check_refused_then_web_reads_without_daemon_access(tmp_path):
    settings=SimpleNamespace(s3_result_root=tmp_path,s3_worker_image=IMAGE,
        cursor_signing_key=SimpleNamespace(get_secret_value=lambda:KEY.decode()))
    web=FormalProvider(settings)
    with pytest.raises(RuntimeFault):web._preflight_process()
    actual=process();publisher=ready.RuntimeReadiness(tmp_path,KEY,actual)
    publisher.refresh()
    with patch('app.backtest_service.process.DockerDaemon.request',side_effect=AssertionError('web accessed Docker')):
        web._preflight_process()
    assert actual.calls==[IMAGE]
    publisher.close()
    with pytest.raises(RuntimeFault):web._preflight_process()


@pytest.mark.parametrize('kind',['expired','future','other_boot','other_image','tampered','unknown_field'])
def test_stale_changed_and_unauthenticated_checks_never_admit(tmp_path,kind):
    publisher=ready.RuntimeReadiness(tmp_path,KEY,process());publisher.refresh()
    if kind=='expired':resign(tmp_path,lambda p:p.update(monotonic_ns=str(int(p['monotonic_ns'])-ready.TTL_NS-1)))
    elif kind=='future':resign(tmp_path,lambda p:p.update(monotonic_ns=str(int(p['monotonic_ns'])+ready.TTL_NS)))
    elif kind=='other_boot':resign(tmp_path,lambda p:p.update(boot='different-host-boot'))
    elif kind=='other_image':resign(tmp_path,lambda p:p.update(worker_image='sha256:'+'b'*64))
    elif kind=='unknown_field':resign(tmp_path,lambda p:p.update(extra='not-an-admission-field'))
    else:
        path=tmp_path/ready.NAME;value=json.loads(path.read_text());value['payload']['publisher']='b'*32
        path.write_text(json.dumps(value))
    with pytest.raises(RuntimeFault) as blocked:ready.check_runtime_ready(tmp_path,KEY,IMAGE)
    assert blocked.value.code=='CAPABILITY_UNAVAILABLE'


@pytest.mark.parametrize('kind',['symlink','fifo','oversize'])
def test_unknown_file_types_and_oversize_are_refused_without_following_or_waiting(tmp_path,kind):
    path=tmp_path/ready.NAME
    if kind=='symlink':
        target=tmp_path/'protected';target.write_text('unchanged');path.symlink_to(target)
    elif kind=='fifo':os.mkfifo(path,0o600)
    else:path.write_bytes(b' '*4097);path.chmod(0o600)
    with pytest.raises(RuntimeFault):ready.check_runtime_ready(tmp_path,KEY,IMAGE)
    if kind=='symlink':assert target.read_text()=='unchanged' and path.is_symlink()


def test_actual_check_failure_does_not_publish_and_newer_publisher_survives_stop(tmp_path):
    failed=ready.RuntimeReadiness(tmp_path,KEY,process(refused=True))
    with pytest.raises(RuntimeFault):failed.refresh()
    assert not (tmp_path/ready.NAME).exists()
    first=ready.RuntimeReadiness(tmp_path,KEY,process());first.refresh()
    second=ready.RuntimeReadiness(tmp_path,KEY,process());second.refresh()
    first.close();ready.check_runtime_ready(tmp_path,KEY,IMAGE)
    assert json.loads((tmp_path/ready.NAME).read_text())['payload']['publisher']==second.publisher
    second.close();assert not (tmp_path/ready.NAME).exists()


@pytest.mark.parametrize('kind',['unknown','symlink','hardlink'])
def test_publication_does_not_replace_unknown_or_linked_current_object(tmp_path,kind):
    path=tmp_path/ready.NAME
    if kind=='unknown':path.write_text('protected unknown data');path.chmod(0o600)
    elif kind=='symlink':
        target=tmp_path/'protected';target.write_text('protected unknown data');path.symlink_to(target)
    else:
        publisher=ready.RuntimeReadiness(tmp_path,KEY,process());publisher.refresh()
        target=tmp_path/'protected';target.hardlink_to(path)
    before=path.read_bytes()
    with pytest.raises(RuntimeFault):ready.RuntimeReadiness(tmp_path,KEY,process()).refresh()
    assert path.read_bytes()==before
    if kind=='symlink':assert path.is_symlink() and target.read_text()=='protected unknown data'
