import unittest
from datetime import date
from app.backtesting.preflight import evaluate_corporate_action_eligibility


class CorporateActionEligibilityTest(unittest.TestCase):
    def test_formal_quantity_blocks(self):
        result = evaluate_corporate_action_eligibility(coverage_status="complete", action_type="split")
        self.assertEqual(result.status, "blocked")

    def test_partial_coverage_filters(self):
        result = evaluate_corporate_action_eligibility(coverage_status="partial")
        self.assertEqual((result.status, result.code), ("filter", "corporate_action_coverage_incomplete"))

    def test_internal_fixture_range(self):
        result = evaluate_corporate_action_eligibility(
            coverage_status="complete", action_type="split", profile="internal_link_acceptance",
            fixture_start=date(2020, 1, 1), fixture_end=date(2020, 12, 31),
            requested_start=date(2020, 2, 1), requested_end=date(2020, 3, 1),
        )
        self.assertEqual(result.status, "eligible")


if __name__ == "__main__":
    unittest.main()
