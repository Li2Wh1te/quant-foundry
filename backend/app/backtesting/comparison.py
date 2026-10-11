"""Compatibility import for retained callers; implementation moved to app.backtest_service.history.comparison."""
import sys
from app.backtest_service.history import comparison as _module
sys.modules[__name__] = _module
