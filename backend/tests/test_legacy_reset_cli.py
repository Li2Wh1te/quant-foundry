"""Reject ambiguous retention choices before opening any database connection."""
import pytest

from app.legacy_reset.__main__ import main


@pytest.mark.parametrize('extra', [
    [],
    ['--rescue','/tmp/rescue.gz'],
    ['--manifest','/tmp/manifest.json'],
    ['--discard-legacy-originals','--rescue','/tmp/rescue.gz'],
    ['--discard-legacy-originals','--manifest','/tmp/manifest.json'],
])
def test_apply_requires_one_explicit_retention_policy(extra):
    # Parsing must fail before loading settings or touching a plan/database.
    with pytest.raises(SystemExit) as error:
        main(['apply','--expect-database','synthetic','--plan','/tmp/nonexistent-plan.json',
              '--sha256','synthetic',*extra])
    assert error.value.code==2
