import unittest
from datetime import date, datetime, timezone

from app.strategy_protocol.context import DecisionContext, DeterministicClockDTO
from app.strategy_protocol.contract import InvalidDecisionPayloadError
from app.strategy_protocol.synthetic import synthetic_session_dates
from app.strategy_protocol.checker import ContractCheckResult


class SyntheticNamedSessionTests(unittest.TestCase):
    def test_named_sessions_skip_weekends(self):
        sessions = synthetic_session_dates(date(2026, 8, 21))
        self.assertEqual(sessions, tuple(date(2026, 8, d) for d in (17, 18, 19, 20, 21)))
        self.assertTrue(all(day.weekday() < 5 for day in sessions))

    def test_context_rejects_cutoff_after_decision(self):
        aware = datetime(2026, 8, 21, 15, tzinfo=timezone.utc)
        with self.assertRaises(InvalidDecisionPayloadError):
            DecisionContext(
                step_sequence=1,
                session_date=date(2026, 8, 21),
                decision_time=aware,
                data_cutoff=aware.replace(hour=16),
                timezone="UTC",
                clock=DeterministicClockDTO(aware, date(2026, 8, 21)),
                portfolio=object(),
                previous_step=object(),
                data=object(),
                universe=object(),
            )

    def test_failure_result_exposes_canonical_evidence_aliases(self):
        result = ContractCheckResult(
            ok=False, failure_phase="strategy_contract_check", line=7,
            technical="trace", message="策略失败"
        )
        self.assertEqual(result.source_line, 7)
        self.assertEqual(result.technical_detail, "trace")


if __name__ == "__main__":
    unittest.main()
