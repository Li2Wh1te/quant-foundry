from app.backtesting.runner_failure import build_failure_evidence


def test_failure_evidence_keeps_location_and_redacts_diagnostics():
    try:
        exec(compile("raise ValueError('token=secret-value')", "published_strategy", "exec"), {})  # noqa: S102
    except ValueError as exc:
        evidence = build_failure_evidence(exc, default_phase="backtest_execution")
    else:  # pragma: no cover
        raise AssertionError("the synthetic strategy must fail")

    assert evidence["failure_phase"] == "backtest_execution"
    assert evidence["error_type"] == "ValueError"
    assert evidence["source_line"] == 1
    assert evidence["desensitized"] is True
    assert "<redacted>" in evidence["message"]
    assert "secret-value" not in evidence["technical_detail"]
