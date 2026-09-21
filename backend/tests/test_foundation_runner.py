"""The optional coordinator must preserve the backtest entry and child isolation."""
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch
from app.runner.__main__ import main
from app.runner.coordinator import run
from app.data_foundation.bars import validate_bar
from app.data_foundation.canonical import FoundationError
import pytest


def test_disabled_worker_runs_original_backtest_path():
    with patch('app.runner.__main__.get_settings', return_value=SimpleNamespace(foundation_worker_enabled=False)), \
         patch('app.runner.__main__.backtest_main') as original, patch('app.runner.coordinator.main') as coordinator:
        main()
        original.assert_called_once_with()
        coordinator.assert_not_called()


def test_foundation_exit_does_not_terminate_backtest_until_stop():
    stop=Event()
    backtest=Mock();backtest.poll.return_value=None
    foundation=Mock();foundation.poll.side_effect=[1,1]
    replacement=Mock();replacement.poll.return_value=None
    created=[]
    def spawn(command):
        process=[backtest,foundation,replacement][len(created)]
        created.append(command)
        if len(created)==3:
            backtest.terminate.assert_not_called()
            stop.set()
        return process
    ticks=iter([0,2,4,6,8,10,12])
    run(stop,spawn=spawn,clock=lambda:next(ticks))
    assert len(created)==3 and 'backtest_main' in created[0][-1]
    assert created[1][-1]=='app.data_foundation.worker'
    backtest.terminate.assert_called_once()
    replacement.terminate.assert_called_once()


@pytest.mark.parametrize('bad',['NaN','Infinity','-1','0','1.00000000001'])
def test_core_prices_fail_closed_before_numeric_rounding(bad):
    with pytest.raises(FoundationError):validate_bar(dict(open=bad,high=bad,low=bad,close=bad,volume=None,turnover=None))
