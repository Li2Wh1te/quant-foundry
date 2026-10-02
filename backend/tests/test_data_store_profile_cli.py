"""The synthetic profile CLI cannot enter production or unbounded work."""
from pathlib import Path
import json
import os
import runpy

import pytest
from tests.test_data_store_kernel import database


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


def test_matched_current_requires_a_reviewed_physical_input(monkeypatch,tmp_path):
    root=Path(__file__).resolve().parents[2]
    script=root/'scripts'/'profile_current_verification.py'
    if not script.exists():script=root/'profile_current_verification.py'
    monkeypatch.syspath_prepend(str(script.parent))
    module=runpy.run_path(str(script))
    for name,value in [('QF_ENVIRONMENT','test'),('QF_DATABASE_HOST','postgres'),('QF_DATABASE_NAME','lfd01_test')]:
        monkeypatch.setenv(name,value)
    monkeypatch.setattr('sys.argv',[str(script),'--root',str(tmp_path),'--output',str(tmp_path/'result.json'),
                                  '--entry','E69','--current','matched'])
    def forbidden(*args,**kwargs):pytest.fail('Rejected fixture must not open a database')
    monkeypatch.setitem(module['main'].__globals__,'isolated',forbidden)
    with pytest.raises(SystemExit) as caught:module['main']()
    assert caught.value.code==2


@pytest.mark.parametrize('current',['empty','matched'])
@pytest.mark.parametrize('entry',['E69','E70'])
@pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED')!='1',reason='isolated PostgreSQL required')
def test_physical_fixture_verifies_actual_current_or_reports_every_missing_object(
        database,monkeypatch,tmp_path,current,entry):
    # This positive end-to-end check uses a disposable schema, the real native
    # table, formal complete-scan commits and real Parquet verification. An
    # empty-current diagnostic must never manufacture a successful comparison.
    root=Path(__file__).resolve().parents[2]
    script=root/'scripts'/'profile_current_verification.py'
    if not script.exists():script=root/'profile_current_verification.py'
    monkeypatch.syspath_prepend(str(script.parent))
    module=runpy.run_path(str(script))
    monkeypatch.setenv('QF_ENVIRONMENT','test')
    output=tmp_path/'result.json'
    arguments=[str(script),'--root',str(tmp_path),'--output',str(output),
               '--entry',entry,'--rows','257','--input','postgres','--current',current]
    # Exercise detailed CPU attribution on the matched fixture while retaining
    # the wall-time-only empty-current path and both actual disposition checks.
    if current=='empty':arguments.append('--no-profile')
    monkeypatch.setattr('sys.argv',arguments)
    module['main']()
    result=json.loads(output.read_text())
    assert result['snapshot_complete'] and not result['production_acceptance']
    assert result['scratch_released'] and result['isolated_schema_and_store_removed']
    assert result['fixture']['before']==result['fixture']['after']
    assert result['fixture']['schema_columns']==len(module['BY_ID'][entry].spec.schema)
    assert result['verification_budget_seconds']==300
    assert result['result']['source_rows']==result['result']['expected_objects']==257
    assert {'native_snapshot','current_files','missing_objects'} <= result['result']['phase_seconds'].keys()
    if current=='matched':
        assert result['fixture']['build_result']['complete']
        assert result['fixture']['before']['files']>1
        assert result['fixture']['before']['rows']==257
        assert result['result']['complete'] and result['result']['current_objects']==257
        assert result['result']['missing_objects']==result['result']['mismatched_objects']==0
        assert 'value_hash' in result['profile_self']
        assert 'native_json' in result['profile_callers']
    else:
        assert not result['result']['complete'] and result['result']['missing_objects']==257
        assert result['result']['current_objects']==result['fixture']['before']['rows']==0
        assert not result['profile_self'] and not result['profile_callers']


@pytest.mark.parametrize('entry,rows', [('E50', '1'), ('E69', '4000001'), ('E70','4000001')])
def test_physical_fixture_cannot_select_an_unreviewed_table_or_unbounded_rows(monkeypatch, tmp_path, entry, rows):
    root = Path(__file__).resolve().parents[2]
    script = root / 'scripts' / 'profile_current_verification.py'
    if not script.exists():
        script = root / 'profile_current_verification.py'
    monkeypatch.syspath_prepend(str(script.parent))
    module = runpy.run_path(str(script))
    output = tmp_path/'result.json'
    for name,value in [('QF_ENVIRONMENT','test'),('QF_DATABASE_HOST','postgres'),('QF_DATABASE_NAME','lfd01_test')]:
        monkeypatch.setenv(name,value)
    monkeypatch.setattr('sys.argv', [str(script),'--root',str(tmp_path),'--output',str(output),
                                   '--entry',entry,'--input','postgres','--rows',rows])
    def forbidden(*args, **kwargs):
        pytest.fail('Unsafe physical fixture must not open a database')
    monkeypatch.setitem(module['main'].__globals__,'isolated',forbidden)
    with pytest.raises(SystemExit) as caught:
        module['main']()
    assert caught.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize('option,value',[
    ('--verification-seconds','0'),('--verification-seconds','3601'),
    ('--fixture-seconds','0'),('--fixture-seconds','3601'),
])
def test_profile_deadlines_remain_bounded_before_database_access(monkeypatch,tmp_path,option,value):
    root=Path(__file__).resolve().parents[2]
    script=root/'scripts'/'profile_current_verification.py'
    if not script.exists():script=root/'profile_current_verification.py'
    monkeypatch.syspath_prepend(str(script.parent))
    module=runpy.run_path(str(script))
    for name,setting in [('QF_ENVIRONMENT','test'),('QF_DATABASE_HOST','postgres'),('QF_DATABASE_NAME','lfd01_test')]:
        monkeypatch.setenv(name,setting)
    monkeypatch.setattr('sys.argv',[str(script),'--root',str(tmp_path),'--output',str(tmp_path/'result.json'),
        '--entry','E70','--input','postgres',option,value])
    def forbidden(*args,**kwargs):pytest.fail('Unbounded diagnostic must not open a database')
    monkeypatch.setitem(module['main'].__globals__,'isolated',forbidden)
    with pytest.raises(SystemExit) as caught:module['main']()
    assert caught.value.code==2
