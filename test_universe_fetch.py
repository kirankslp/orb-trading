"""Universe loading and partial-fetch tolerance.

A 2565-name exchange dump always carries delisted and suspended scrips, so some
daily fetches fail every run. The rule is "never SILENTLY incomplete", not
"never incomplete": skips are reported, and a failure rate high enough to mean a
dead token rather than dead scrips still aborts.
"""

import io
import os
import unittest
from contextlib import redirect_stdout

import pandas as pd

import symbol_screener as sc
from kite_data import KiteDataError


class FakeMD:
    """Fails for any symbol in `dead`, returns a usable frame otherwise."""

    def __init__(self, dead=()):
        self.dead = set(dead)

    def daily(self, symbol, days):
        if symbol in self.dead:
            raise KiteDataError(f"no instrument token for {symbol}")
        idx = pd.bdate_range(end="2026-03-01", periods=days)
        return pd.DataFrame(
            {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
             "Volume": 1_000_000}, index=pd.DatetimeIndex(idx, name="Datetime"))


class TestFetchTolerance(unittest.TestCase):
    def setUp(self):
        self.universe = [f"S{i:04d}.NS" for i in range(100)]

    def test_clean_fetch_returns_everything(self):
        out = sc.fetch_daily(self.universe, days=40, market_data=FakeMD())
        self.assertEqual(len(out), 100)

    def test_tolerable_failures_are_reported_not_swallowed(self):
        dead = self.universe[:10]          # 10% < 15% tolerance
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = sc.fetch_daily(self.universe, days=40, market_data=FakeMD(dead))
        self.assertEqual(len(out), 90)
        printed = buf.getvalue()
        self.assertIn("skipped 10", printed, "a skip must be visible")
        self.assertIn("10.0%", printed)

    def test_excessive_failures_still_abort(self):
        dead = self.universe[:40]          # 40% > 15% tolerance
        with self.assertRaises(SystemExit) as ctx:
            sc.fetch_daily(self.universe, days=40, market_data=FakeMD(dead))
        msg = str(ctx.exception)
        self.assertIn("40 of 100", msg)
        self.assertIn("session token", msg,
                      "the likely cause should be named, not just the count")

    def test_boundary_just_under_tolerance(self):
        dead = self.universe[:15]          # exactly 15%, not OVER the tolerance
        with redirect_stdout(io.StringIO()):
            out = sc.fetch_daily(self.universe, days=40, market_data=FakeMD(dead))
        self.assertEqual(len(out), 85)

    def test_empty_frames_count_as_failures(self):
        class Empty(FakeMD):
            def daily(self, symbol, days):
                if symbol in self.dead:
                    return pd.DataFrame()
                return super().daily(symbol, days)
        with redirect_stdout(io.StringIO()):
            out = sc.fetch_daily(self.universe, days=40,
                                 market_data=Empty(self.universe[:5]))
        self.assertEqual(len(out), 95)

    def test_total_failure_aborts(self):
        with self.assertRaises(SystemExit):
            sc.fetch_daily(self.universe, days=40,
                           market_data=FakeMD(self.universe))


class TestUniverse(unittest.TestCase):
    def test_default_is_the_full_nse_list(self):
        u = sc.load_universe()
        self.assertGreater(len(u), 2000, "EQUITY_L.csv should be the default pool")
        self.assertTrue(all(s.endswith(".NS") for s in u))

    def test_no_duplicates_and_sorted(self):
        u = sc.load_universe()
        self.assertEqual(len(u), len(set(u)))
        self.assertEqual(u, sorted(u))

    def test_env_override_still_wins(self):
        saved = os.environ.get("ORB_UNIVERSE_FILE")
        try:
            os.environ["ORB_UNIVERSE_FILE"] = "EQUITY_L.csv"
            self.assertGreater(len(sc.load_universe()), 2000)
        finally:
            if saved is None:
                os.environ.pop("ORB_UNIVERSE_FILE", None)
            else:
                os.environ["ORB_UNIVERSE_FILE"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
