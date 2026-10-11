"""Compatibility import for retained callers; implementation moved to app.backtest_service.history.result_schemas."""
import sys
from app.backtest_service.history import result_schemas as _module
sys.modules[__name__] = _module
