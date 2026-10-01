"""Real native-input/CLI continuation on an isolated supported current store."""
from datetime import UTC, datetime, timedelta
import json
import os

import pytest

from tests.test_data_store_kernel import database, limits, store, KEY
from tests.test_data_store_domain_samples import ready
from tests.test_data_store_failure_isolation import many_calendar, calendar_rows, B


pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1',
                               reason='isolated PostgreSQL and supported filesystem required')


def test_cli_drains_real_bounded_native_work_without_claiming_acceptance(ready, monkeypatch, tmp_path, capsys):
    from app.data_store.closeout import main
    from app.db import session
    many_calendar(ready, 3)
    monkeypatch.setattr(session, 'get_engine', lambda: ready.catalog.engine)
    monkeypatch.setenv('QF_CURSOR_SIGNING_KEY', KEY.decode())
    state = tmp_path / 'closeout.json'
    deadline = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    args = ['--root', str(ready.files.root), '--state', str(state), '--entry', B.id,
            '--deadline', deadline, '--max-passes', '1', '--max-attempts', '8']
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['processing_finished'] and not report['acceptance_complete']
    saved = json.loads(state.read_text())
    assert saved['jobs'][B.id]['status'] == 'done'
    assert saved['jobs'][B.id]['attempts'] >= 3
    assert not saved['supplier_collection_executed']
    assert len(calendar_rows(ready, B)) == 3
    assert main(args + ['--resume']) == 0
    capsys.readouterr()
    assert json.loads(state.read_text())['jobs'][B.id]['attempts'] == saved['jobs'][B.id]['attempts']
