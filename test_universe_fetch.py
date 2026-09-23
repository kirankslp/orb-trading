"""Universe loading and partial-fetch tolerance.

A 2565-name exchange dump always carries delisted and suspended scrips, so some
daily fetches fail every run. The rule is "never SILENTLY incomplete", not
"never incomplete": skips are reported, and a failure rate high enough to mean a
dead token rather than dead scrips still aborts.
"""

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

import pandas as pd

import symbol_screener as sc
from kite_data import KiteDataError, KiteInstrumentError


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


class TestTradeToTradeFilter(unittest.TestCase):
    """BE and BZ require delivery and cannot be squared off intraday, so an
    intraday strategy must never see them. This is correctness, not tidiness:
    including them puts untakeable trades in a backtest."""

    def _csv(self, rows):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "pool.csv")
        with open(path, "w") as fh:
            fh.write("SYMBOL,NAME OF COMPANY, SERIES\n")
            for sym, series in rows:
                fh.write(f"{sym},Some Co Ltd,{series}\n")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return path

    def test_t2t_series_are_dropped(self):
        path = self._csv([("GOODEQ", "EQ"), ("BADBE", "BE"), ("BADBZ", "BZ")])
        with redirect_stdout(io.StringIO()):
            out = sc.load_universe(path)
        self.assertEqual(out, ["GOODEQ.NS"])

    def test_drop_is_reported(self):
        path = self._csv([("GOODEQ", "EQ"), ("BADBE", "BE")])
        buf = io.StringIO()
        with redirect_stdout(buf):
            sc.load_universe(path)
        self.assertIn("dropped 1", buf.getvalue())
        self.assertIn("Trade-to-Trade", buf.getvalue())

    def test_whitespace_and_case_tolerated(self):
        path = self._csv([(" mixedeq ", " eq "), ("X", " Be ")])
        with redirect_stdout(io.StringIO()):
            out = sc.load_universe(path)
        self.assertEqual(out, ["MIXEDEQ.NS"])

    def test_csv_without_series_column_is_untouched(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "plain.csv")
        with open(path, "w") as fh:
            fh.write("SYMBOL\nAAA\nBBB\n")
        self.assertEqual(sc.load_universe(path), ["AAA.NS", "BBB.NS"])

    def test_shipped_equity_list_drops_the_known_t2t_name(self):
        """3IINFOLTD is series BE and is what aborted a real 2565-name run."""
        with redirect_stdout(io.StringIO()):
            u = sc.load_universe("EQUITY_L.csv")
        self.assertNotIn("3IINFOLTD.NS", u)
        self.assertEqual(len(u), 2302)


class TestInstrumentErrorTolerated(unittest.TestCase):
    """A symbol absent from Kite's instrument list raises KiteInstrumentError,
    a SIBLING of KiteDataError. Catching only KiteDataError let one such name
    abort the whole run."""

    class MissingInstrumentMD(FakeMD):
        def daily(self, symbol, days):
            if symbol in self.dead:
                raise KiteInstrumentError(
                    f"'{symbol}' is not a current NSE Kite instrument")
            return super().daily(symbol, days)

    def test_missing_instrument_is_skipped_not_fatal(self):
        universe = [f"S{i:04d}.NS" for i in range(100)]
        md = self.MissingInstrumentMD(universe[:5])
        with redirect_stdout(io.StringIO()):
            out = sc.fetch_daily(universe, days=40, market_data=md)
        self.assertEqual(len(out), 95)

    def test_both_error_types_count_toward_the_same_tolerance(self):
        universe = [f"S{i:04d}.NS" for i in range(100)]

        class Mixed(FakeMD):
            def daily(self, symbol, days):
                if symbol in ("S0000.NS", "S0001.NS"):
                    raise KiteInstrumentError("absent")
                if symbol in ("S0002.NS",):
                    raise KiteDataError("bad response")
                return FakeMD.daily(self, symbol, days)

        buf = io.StringIO()
        with redirect_stdout(buf):
            out = sc.fetch_daily(universe, days=40, market_data=Mixed())
        self.assertEqual(len(out), 97)
        self.assertIn("skipped 3", buf.getvalue())

    def test_too_many_missing_instruments_still_aborts(self):
        universe = [f"S{i:04d}.NS" for i in range(100)]
        md = self.MissingInstrumentMD(universe[:40])
        with self.assertRaises(SystemExit):
            sc.fetch_daily(universe, days=40, market_data=md)
