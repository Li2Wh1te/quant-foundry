"""Required D13 regressions run with the installed wheel in S3 CI."""
import importlib.util
import os
from pathlib import Path
import sys
import pytest

if os.environ.get("QF_S3_D13_REQUIRED") != "1":
    pytest.skip("D13 real private DB/wheel suite runs in S3 CI", allow_module_level=True)
path = Path(__file__).resolve().parents[2]/"tests/s3_runservice_suite.py"
loader = importlib.util.spec_from_file_location("s3_runservice_suite", path)
module = importlib.util.module_from_spec(loader); sys.modules[loader.name] = module
loader.loader.exec_module(module)
D13Tests = module.D13Tests
