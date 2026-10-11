"""Compatibility import for retained callers; implementation moved to app.backtest_service.accounts.schemas."""
import sys
from app.backtest_service.accounts import schemas as _module
sys.modules[__name__] = _module
