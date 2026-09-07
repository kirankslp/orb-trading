import unittest

from strategy_analysis import execution_plan, momentum_metrics, unified_recommendations


def candles(start=100.0, step=1.0, count=40):
    rows = []
    for index in range(count):
        close = start + index * step
        rows.append({"open": close - step / 2, "high": close + 1, "low": close - 1,
                     "close": close, "volume": 1000 + index * 20})
    return rows


class StrategyAnalysisTests(unittest.TestCase):
    def test_momentum_detects_persistent_uptrend(self):
        result = momentum_metrics("UP", "Uptrend", candles())
        self.assertEqual(result["direction"], "Bullish")
        self.assertGreater(result["score"], 60)
        self.assertGreater(result["return_20d"], 0)

    def test_execution_uses_stop_limit_for_confirmed_momentum(self):
        result = execution_plan({"symbol": "UP", "direction": "Bullish", "score": 80,
                                 "close": 140, "sma6": 137, "atr": 2,
                                 "volume_ratio": 1.4}, budget=5000)
        self.assertEqual(result["order_type"], "BUY STOP-LIMIT")
        self.assertLess(result["stop"], result["trigger"])
        self.assertGreater(result["target"], result["trigger"])

    def test_consensus_requires_sma_momentum_agreement(self):
        scan = [{"symbol": "UP", "name": "Up", "direction": "Bullish"},
                {"symbol": "MIX", "name": "Mixed", "direction": "Bearish"}]
        momentum = [
            {"symbol": "UP", "name": "Up", "direction": "Bullish", "score": 80,
             "close": 140, "sma6": 137, "atr": 2, "volume_ratio": 1.4},
            {"symbol": "MIX", "name": "Mixed", "direction": "Bullish", "score": 70,
             "close": 90, "sma6": 88, "atr": 1, "volume_ratio": 1.2},
        ]
        orb = {"date": "2026-09-07", "symbols": ["UP.NS"], "rows": [
            {"symbol": "UP.NS", "side": "LONG", "trigger": 141, "stop": 139,
             "target": 145, "qty": 35}]}
        result = unified_recommendations(scan, momentum, orb)["recommendations"]
        by_symbol = {row["symbol"]: row for row in result}
        self.assertEqual(by_symbol["UP"]["bias"], "LONG")
        self.assertEqual(by_symbol["UP"]["confidence"], "High")
        self.assertEqual(by_symbol["UP"]["plan"]["trigger"], 141)
        self.assertEqual(by_symbol["MIX"]["bias"], "WAIT")

    def test_global_regime_adjusts_confidence_without_changing_direction(self):
        scan = [{"symbol": "UP", "name": "Up", "direction": "Bullish"}]
        momentum = [{"symbol": "UP", "name": "Up", "direction": "Bullish", "score": 75,
                     "close": 140, "sma6": 137, "atr": 2, "volume_ratio": 1.2}]
        orb = {"date": "2026-09-07", "symbols": [], "rows": []}
        result = unified_recommendations(
            scan, momentum, orb, market_context={"regime": "Risk-off", "score": -42})
        row = result["recommendations"][0]
        self.assertEqual(row["bias"], "LONG")
        self.assertEqual(row["confidence"], "Low")
        self.assertEqual(row["global_adjustment"], -6)
        self.assertEqual(result["market_regime"], "Risk-off")


if __name__ == "__main__":
    unittest.main()
