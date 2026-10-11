"""D14 required real service regressions use the separately installed wheel."""
import importlib.util
import os
from pathlib import Path
import sys
import pytest

if os.environ.get("QF_S3_D14_REQUIRED") != "1":
    pytest.skip("D14 private DB/wheel suite runs in S3 CI", allow_module_level=True)
path=Path(__file__).resolve().parents[2]/"tests/s3_cli_suite.py"
sys.path.insert(0,str(path.parent))
loader=importlib.util.spec_from_file_location("s3_cli_suite",path)
module=importlib.util.module_from_spec(loader);sys.modules[loader.name]=module
loader.loader.exec_module(module)
D14Tests=module.D14Tests
