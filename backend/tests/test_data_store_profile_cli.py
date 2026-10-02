"""The synthetic profile CLI cannot enter production or unbounded work."""
from pathlib import Path
import runpy

import pytest


@pytest.mark.parametrize('environment,host,database,rows', [
    ('production', '127.0.0.1', 'lfd01_test', '20'),
    ('test', '10.173.158.222', 'lfd01_test', '20'),
    ('test', '127.0.0.1', 'quant_foundry', '20'),
    ('test', '127.0.0.1', 'lfd01_test', '0'),
    ('test', '127.0.0.1', 'lfd01_test', '250001'),
])
def test_profile_refuses_unsafe_runtime_before_database_access(
        monkeypatch, tmp_path, environment, host, database, rows):
    # Both the repository checkout and the isolated acceptance image ship the
    # same CLI. No fallback script or production application runtime is loaded.
    root = Path(__file__).resolve().parents[2]
    script = root / 'scripts' / 'profile_current_verification.py'
    if not script.exists():
        script = root / 'profile_current_verification.py'
    monkeypatch.syspath_prepend(str(script.parent))
    module = runpy.run_path(str(script))
    output = tmp_path / 'result.json'
    monkeypatch.setenv('QF_ENVIRONMENT', environment)
    monkeypatch.setenv('QF_DATABASE_HOST', host)
    monkeypatch.setenv('QF_DATABASE_NAME', database)
    monkeypatch.setattr('sys.argv', [str(script), '--root', str(tmp_path),
                                  '--output', str(output), '--entry', 'E69', '--rows', rows])

    def database_access_forbidden(*args, **kwargs):
        pytest.fail('Rejected diagnostic must not open a database')

    monkeypatch.setitem(module['main'].__globals__, 'isolated', database_access_forbidden)
    with pytest.raises(SystemExit) as caught:
        module['main']()
    assert caught.value.code == 2
    assert not output.exists()
