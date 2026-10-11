"""Compatibility import for retained callers; implementation moved to app.backtest_service.accounts.service."""
import sys
from app.backtest_service.accounts import service as _module
sys.modules[__name__] = _module
