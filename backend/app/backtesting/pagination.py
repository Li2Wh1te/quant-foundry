"""Compatibility import for retained callers; implementation moved to app.backtest_service.history.pagination."""
import sys
from app.backtest_service.history import pagination as _module
sys.modules[__name__] = _module
