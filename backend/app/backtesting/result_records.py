"""Compatibility import for retained callers; implementation moved to app.backtest_service.history.result_records."""
import sys
from app.backtest_service.history import result_records as _module
sys.modules[__name__] = _module
