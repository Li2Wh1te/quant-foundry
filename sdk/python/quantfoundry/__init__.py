"""Rust strategy API and HTTP clients for the single S3 run service."""
import json
from . import _native
from .client import BacktestClient, BacktestError, RunConfig
from ._results import RunResults, ResultPage
from .numeric import decimal_input, nanoseconds

ContractError = _native.ContractError
__version__ = _native.__version__

def capabilities() -> dict:
    return json.loads(_native.capabilities_json())

__all__ = ["RunConfig", "BacktestClient", "BacktestError", "ContractError", "decimal_input", "nanoseconds", "capabilities", "RunResults", "ResultPage"]
