"""Compatibility import for retained callers; implementation moved to app.backtest_service.history.router."""
import sys
from app.backtest_service.history import router as _module
sys.modules[__name__] = _module
