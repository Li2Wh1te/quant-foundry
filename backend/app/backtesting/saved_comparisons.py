"""Compatibility import for retained callers; implementation moved to app.backtest_service.history.saved_comparisons."""
import sys
from app.backtest_service.history import saved_comparisons as _module
sys.modules[__name__] = _module
