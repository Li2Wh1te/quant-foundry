"""S3 contracts and strategy SDK; production submission still needs D12/D13."""
import json
from . import _native
from .client import BacktestClient, RunConfig
from ._results import RunResults, ResultPage
from .numeric import decimal_input, nanoseconds

ContractError = _native.ContractError
__version__ = _native.__version__

def capabilities() -> dict:
    return json.loads(_native.capabilities_json())

__all__ = ["RunConfig", "BacktestClient", "ContractError", "decimal_input", "nanoseconds", "capabilities", "RunResults", "ResultPage"]
