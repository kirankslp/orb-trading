import unittest

from budget_plan import build_intraday_budget_plan


def recommendation(symbol, entry, bias="LONG", stop=None, target=None):
    return {
        "symbol": symbol, "name": symbol, "bias": bias, "confidence": "Medium",
        "last_price": entry - 2,
        "action": f"{bias} plan",
        "plan": {"order_type": "BUY LIMIT", "trigger": entry,
                 "stop": stop or entry - 10, "target": target or entry + 20},
    }


class BudgetPlanTests(unittest.TestCase):
    def test_default_budget_splits_across_two_whole_share_positions(self):
        plan = build_intraday_budget_plan([
            recommendation("A", 1000), recommendation("B", 600), recommendation("C", 200)
        ])
        self.assertEqual(plan["budget"], 10000)
        self.assertEqual([row["quantity"] for row in plan["rows"]], [5, 8])
        self.assertLessEqual(plan["deployed"], plan["budget"])
        self.assertAlmostEqual(plan["unused_cash"], 200)

    def test_wait_rows_are_not_allocated(self):
        plan = build_intraday_budget_plan([recommendation("WAIT", 100, bias="WAIT")])
        self.assertEqual(plan["rows"], [])
        self.assertEqual(plan["unused_cash"], 10000)

    def test_single_expensive_affordable_candidate_can_use_full_budget(self):
        plan = build_intraday_budget_plan([
            recommendation("EXPENSIVE", 7000), recommendation("TOO-DEAR", 12000)
        ])
        self.assertEqual(len(plan["rows"]), 1)
        self.assertEqual(plan["rows"][0]["quantity"], 1)
        self.assertEqual(plan["rows"][0]["slot_budget"], 10000)

    def test_budget_bounds_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "between"):
            build_intraday_budget_plan([], 100)
        with self.assertRaisesRegex(ValueError, "number"):
            build_intraday_budget_plan([], "many")


if __name__ == "__main__":
    unittest.main()
