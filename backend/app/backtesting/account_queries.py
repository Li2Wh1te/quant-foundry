"""Compatibility import for retained callers; implementation moved to app.backtest_service.accounts.queries."""
import sys
from app.backtest_service.accounts import queries as _module
sys.modules[__name__] = _module
