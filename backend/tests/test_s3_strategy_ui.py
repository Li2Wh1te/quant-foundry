"""D15 templates and synchronous validation reuse D13's nonexecuting parser."""
import ast
from pathlib import Path
import re
from app.strategies.validation import validate_s3_source, validate_strategy_draft
ROOT = Path(__file__).resolve().parents[2]

def test_new_templates_import_new_sdk_and_have_only_s3_callbacks():
    templates = (ROOT / "frontend/src/pages/strategies/templates.ts").read_text()
    sources = [ast.literal_eval(s) for s in re.findall(r"source: ('(?:[^'\\]|\\.)*')", templates)]
    sources.append((ROOT / "examples/strategies/etf_rotation.py").read_text())
    assert len(sources) == 3
    for source in sources:
        assert "from quantfoundry.api import" in source
        assert validate_s3_source(source).valid
        assert validate_strategy_draft(source, parameter_schema={}, default_parameters={}).valid
        assert not any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in ast.parse(source).body)

def test_s3_parser_never_executes_imports_and_preserves_legacy_draft_validation():
    assert validate_s3_source("import nonexistent_module\nraise RuntimeError('must not execute')\ndef initialize(context): pass\n").valid
    for source in ("async def handle_data(c,d): pass", "def initialize(c, extra): pass", "@unknown_decorator\ndef initialize(c): pass", "def initialize(c): pass\ndef initialize(c): pass", "def run(context, parameters): pass"):
        assert not validate_s3_source(source).valid
    assert validate_strategy_draft("def run(context, parameters): pass", parameter_schema={}, default_parameters={}).valid
