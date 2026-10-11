"""Compatibility import for retained callers; implementation moved to app.backtest_service.history.storage_models."""
import sys
from app.backtest_service.history import storage_models as _module
sys.modules[__name__] = _module
