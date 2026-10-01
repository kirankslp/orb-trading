"""Session-clustered confidence intervals (stats.py)."""

import math
import unittest

import numpy as np
import pandas as pd

import strategy_backtest as sb
from stats import session_total_ci


def iid_ci(pnl, z=1.96):
    """The per-trade interval the reports used to print, for comparison."""
    half = z * pnl.std(ddof=1) * math.sqrt(len(pnl))
    return pnl.sum() - half, pnl.sum() + half


class TestSessionCI(unittest.TestCase):
    def test_correlated_days_widen_the_interval(self):
        """Ten positions per day that all move with that day's market: the
        per-trade interval counts them as ten draws, the honest one as one."""
        rng = np.random.default_rng(0)
        day_moves = rng.normal(0, 100, 60)
        rows = [dict(date=d, pnl=m + rng.normal(0, 5)) for d, m in enumerate(day_moves)
                for _ in range(10)]
        tr = pd.DataFrame(rows)
        lo_c, hi_c = session_total_ci(tr)
        lo_i, hi_i = iid_ci(tr.pnl)
        self.assertGreater((hi_c - lo_c) / (hi_i - lo_i), 2.5,
                           "perfectly correlated days: ~sqrt(10) wider")

    def test_independent_trades_are_roughly_unchanged(self):
        """With no within-day correlation the two should broadly agree, so the
        clustered interval is not just conservative for its own sake."""
        rng = np.random.default_rng(1)
        tr = pd.DataFrame(dict(date=np.repeat(np.arange(200), 5),
                               pnl=rng.normal(0, 100, 1000)))
        lo_c, hi_c = session_total_ci(tr)
        lo_i, hi_i = iid_ci(tr.pnl)
        self.assertAlmostEqual((hi_c - lo_c) / (hi_i - lo_i), 1.0, delta=0.15)

    def test_centred_on_the_total(self):
        tr = pd.DataFrame(dict(date=[1, 1, 2, 3], pnl=[10.0, 5.0, -2.0, 7.0]))
        lo, hi = session_total_ci(tr)
        self.assertAlmostEqual((lo + hi) / 2, 20.0)

    def test_sessions_without_trades_count_as_zero(self):
        tr = pd.DataFrame(dict(date=[1, 2], pnl=[50.0, 50.0]))
        lo, hi = session_total_ci(tr)
        lo_z, hi_z = session_total_ci(tr, sessions=[1, 2, 3, 4])
        self.assertAlmostEqual((lo_z + hi_z) / 2, 100.0, msg="zeros leave the total alone")
        self.assertGreater(hi_z - lo_z, hi - lo, msg="...but add spread")

    def test_too_few_sessions(self):
        self.assertEqual(session_total_ci(pd.DataFrame(dict(date=[1, 1], pnl=[1.0, 2.0]))),
                         (None, None))
        self.assertEqual(session_total_ci(pd.DataFrame()), (None, None))

    def test_other_columns(self):
        tr = pd.DataFrame(dict(date=[1, 2, 3], pnl=[0.0] * 3, gross=[1.0, 2.0, 3.0]))
        lo, hi = session_total_ci(tr, "gross")
        self.assertAlmostEqual((lo + hi) / 2, 6.0)


class TestStrategySummaryUsesSessions(unittest.TestCase):
    def test_summary_interval_is_session_clustered(self):
        tr = pd.DataFrame(dict(date=[1] * 5 + [2] * 5 + [3] * 5,
                               pnl=[100.0] * 5 + [-100.0] * 5 + [50.0] * 5,
                               gross=[110.0] * 5 + [-90.0] * 5 + [60.0] * 5,
                               cost=[10.0] * 15, reason=["squareoff"] * 15))
        s = sb.summarise(tr, sessions=[1, 2, 3])
        self.assertEqual((s["lo"], s["hi"]), session_total_ci(tr, sessions=[1, 2, 3]))
        self.assertEqual((s["glo"], s["ghi"]),
                         session_total_ci(tr, "gross", sessions=[1, 2, 3]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
