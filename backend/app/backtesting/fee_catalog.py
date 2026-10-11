"""Compatibility import for retained callers; implementation moved to app.backtest_service.accounts.fee_catalog."""
import sys
from app.backtest_service.accounts import fee_catalog as _module
sys.modules[__name__] = _module
