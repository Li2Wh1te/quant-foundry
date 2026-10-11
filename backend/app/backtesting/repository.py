"""Compatibility import for retained callers; implementation moved to app.backtest_service.accounts.repository."""
import sys
from app.backtest_service.accounts import repository as _module
sys.modules[__name__] = _module
