"""Momentum backtest: costs, tax, the signal's point-in-time rules, the
simulation's mechanics and an end-to-end run on synthetic data."""

import contextlib
import io
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import momentum_backtest as mb

DATES = pd.bdate_range("2023-01-02", periods=600)


def panel(growth, price=100.0, volume=1e7, dates=DATES):
    """Wide Open/Close/turnover frames from per-symbol daily growth rates."""
    t = np.arange(len(dates))
    close = pd.DataFrame({s: price * (1 + g) ** t for s, g in growth.items()}, index=dates)
    opens = close.shift(1).fillna(close.iloc[0])
    turnover = close * volume
    return opens, close, turnover


class TestCosts(unittest.TestCase):
    def test_hand_computed(self):
        self.assertAlmostEqual(mb.buy_cost(100000), 118.6226, places=3)
        self.assertAlmostEqual(mb.sell_cost(100000), 103.6226 + mb.DP_CHARGE, places=3)

    def test_dp_is_per_sale_not_per_rupee(self):
        self.assertAlmostEqual(mb.sell_cost(0), mb.DP_CHARGE)


class TestTax(unittest.TestCase):
    def test_rules(self):
        self.assertAlmostEqual(mb.tax_for(100000, 0), 20000)
        self.assertAlmostEqual(mb.tax_for(-50000, 300000), 0.125 * 125000)
        self.assertEqual(mb.tax_for(-10000, -5000), 0)
        self.assertEqual(mb.tax_for(0, 100000), 0, "inside the Rs 1.25 lakh exemption")

    def test_fiscal_year(self):
        self.assertEqual(mb.fiscal_year(pd.Timestamp("2026-03-31")), 2025)
        self.assertEqual(mb.fiscal_year(pd.Timestamp("2026-04-01")), 2026)


class TestSignal(unittest.TestCase):
    def setUp(self):
        self.o, self.c, self.t = panel({"UP": 0.003, "MID": 0.001, "FLAT": 0.0, "DOWN": -0.001})
        self.day = DATES[400]

    def test_ranks_by_twelve_minus_one(self):
        self.assertEqual(mb.pick(self.c, self.t, self.day, n=2), ["UP", "MID"])

    def test_future_prices_cannot_change_the_pick(self):
        c = self.c.copy()
        c.loc[c.index > self.day, "DOWN"] *= 50
        self.assertEqual(mb.pick(c, self.t, self.day, n=2), ["UP", "MID"])

    def test_the_latest_month_is_skipped(self):
        c = self.c.copy()
        pos = DATES.get_loc(self.day)
        # FLAT rockets only inside the skipped month: it must not rank first.
        c.iloc[pos - mb.SKIP + 1:pos + 1, c.columns.get_loc("FLAT")] *= 3
        self.assertEqual(mb.pick(c, self.t, self.day, n=1), ["UP"])

    def test_eligibility_rules(self):
        o, c, t = panel({"OK": 0.001, "CHEAP": 0.001, "THIN": 0.001})
        c["CHEAP"] = c["CHEAP"] * 0.2          # below Rs 50
        t["THIN"] = t["THIN"] / 1000           # far under the turnover floor
        self.assertEqual(mb.eligible(c, t, DATES[400]), ["OK"])
        self.assertEqual(mb.eligible(c, t, DATES[100]), [], "under a year of history")

    def test_eligibility_cannot_see_later_turnover(self):
        o, c, t = panel({"OK": 0.001, "LATE": 0.001})
        day = DATES[400]
        t.loc[t.index <= day, "LATE"] = 0.0     # illiquid up to the day...
        t.loc[t.index > day, "LATE"] = 1e12     # ...hugely liquid only afterwards
        self.assertEqual(mb.eligible(c, t, day), ["OK"])

    def test_eligibility_cannot_see_later_prices(self):
        o, c, t = panel({"OK": 0.001, "LATE": 0.001})
        day = DATES[400]
        c.loc[c.index > day, "LATE"] = np.nan   # history ends: still judged on the past
        c.loc[c.index <= DATES[200], "OK"] = np.nan   # under 90% of a year before the day
        self.assertEqual(mb.eligible(c, t, day), ["LATE"])

    def test_excluded_symbols_never_rank(self):
        self.assertNotIn("UP", mb.pick(self.c, self.t, self.day, excluded=["UP"], n=2))

    def test_suspect_split_is_flagged(self):
        c = self.c.copy()
        c.loc[c.index >= DATES[300], "MID"] /= 2.5
        self.assertEqual(mb.suspect_symbols(c), ["MID"])


class TestMonthEnds(unittest.TestCase):
    def test_last_session_of_each_month(self):
        ends = mb.month_ends(DATES, DATES[0])
        self.assertEqual(ends[0], pd.Timestamp("2023-01-31"))
        self.assertTrue(all(e.month != (e + pd.offsets.BDay(1)).month for e in ends[:-1]))


class TestSimulate(unittest.TestCase):
    def run_sim(self, growth, n=2, capital=100000.0):
        o, c, t = panel(growth)
        rebal = mb.month_ends(DATES, DATES[260])
        return mb.simulate(o, c, t, rebal, n=n, capital=capital), o, c, rebal

    def test_buys_at_the_next_sessions_open_in_whole_shares(self):
        res, o, c, rebal = self.run_sim({"UP": 0.003, "MID": 0.001, "DOWN": -0.001})
        first = res["trades"].iloc[0]
        exec_day = DATES[DATES.get_loc(rebal[0]) + 1]
        self.assertEqual(pd.Timestamp(first["date"]), exec_day)
        self.assertAlmostEqual(first["price"], round(o.at[exec_day, first["symbol"]], 2))
        self.assertEqual(first["qty"], int(first["qty"]))

    def test_continuing_holdings_are_left_alone(self):
        res, *_ = self.run_sim({"UP": 0.003, "MID": 0.001, "DOWN": -0.001})
        tr = res["trades"]
        # UP and MID lead every month: bought once, never sold or topped up.
        self.assertEqual(len(tr), 2)
        self.assertEqual(set(tr.action), {"BUY"})

    def test_cash_never_goes_negative_and_equity_is_tracked(self):
        res, *_ = self.run_sim({"A": 0.002, "B": 0.0015, "C": 0.001}, n=2, capital=5000.0)
        self.assertGreaterEqual(res["cash"], 0)
        self.assertTrue(res["equity"].notna().all())

    def test_rotation_sells_and_books_gains(self):
        # LATE overtakes EARLY's momentum halfway: EARLY is sold, LATE bought.
        t = np.arange(len(DATES))
        early = 100 * np.where(t < 400, 1.003 ** t, 1.003 ** 400 * 0.999 ** (t - 400))
        late = 100 * np.where(t < 400, 1.0 ** t, 1.006 ** (t - 400))
        c = pd.DataFrame({"EARLY": early, "LATE": late, "SLOW": 100 * 1.0005 ** t}, index=DATES)
        o, tt = c.shift(1).fillna(c.iloc[0]), c * 1e7
        rebal = mb.month_ends(DATES, DATES[260])
        res = mb.simulate(o, c, tt, rebal, n=1)
        sells = res["trades"][res["trades"].action == "SELL"]
        self.assertIn("EARLY", set(sells.symbol))
        self.assertTrue(sum(res["tax"].values()) >= 0)
        self.assertIn("LATE", set(res["held"]))

    def test_equal_weight_benchmark(self):
        o, c, t = panel({"A": 0.001, "B": 0.0})
        rebal = mb.month_ends(DATES, DATES[260])
        ew = mb.equal_weight_monthly(c, t, rebal)
        d0, d1 = rebal[0], rebal[1]
        expect = ((c.loc[d1, "A"] / c.loc[d0, "A"] - 1) + 0.0) / 2
        self.assertAlmostEqual(ew[d1], expect)


class TestSummary(unittest.TestCase):
    def test_cagr_and_drawdown(self):
        s = mb.summary(pd.Series([0.01] * 12))
        self.assertAlmostEqual(s["cagr"], 1.01 ** 12 - 1)
        self.assertEqual(s["mdd"], 0.0)
        s = mb.summary(pd.Series([0.10, -0.50, 0.20]))
        self.assertAlmostEqual(s["mdd"], -0.5)


class FakeMD:
    def __init__(self, frames):
        self.frames = frames

    def candles(self, symbol, interval, start, end):
        f = self.frames[symbol]
        return f[(f.Datetime.dt.date >= start) & (f.Datetime.dt.date <= end)].reset_index(drop=True)


def candle_frame(closes, dates):
    return pd.DataFrame(dict(Datetime=pd.to_datetime(dates).tz_localize("Asia/Kolkata"),
                             Open=closes, High=closes, Low=closes, Close=closes,
                             Volume=[1e7] * len(closes)))


class TestEndToEnd(unittest.TestCase):
    def test_report_runs_and_caches(self):
        end = pd.Timestamp.today().normalize() - pd.Timedelta(days=1)
        dates = pd.bdate_range(end=end, periods=520)
        rng = np.random.default_rng(1)
        frames = {}
        for i in range(8):
            r = rng.normal(0.0004 * (i - 3), 0.01, len(dates))
            frames[f"S{i}.NS"] = candle_frame(list(100 * np.cumprod(1 + r)), dates)
        frames["NIFTY 50"] = candle_frame(list(10000 * np.cumprod(1 + rng.normal(0.0003, 0.008, len(dates)))), dates)
        md = FakeMD(frames)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = io.StringIO()
            with mock.patch.object(mb, "TRADES_FILE", tmp / "t.csv"), \
                    mock.patch.object(mb, "MONTHLY_FILE", tmp / "m.csv"), \
                    mock.patch.object(mb, "TOP_N", 3), contextlib.redirect_stdout(out):
                mb.run(years=1, market_data=md, cache_dir=tmp / "cache",
                       universe=[f"S{i}.NS" for i in range(8)])
            text = out.getvalue()
            self.assertEqual(len(list((tmp / "cache").glob("*.csv"))), 8)
            self.assertTrue((tmp / "m.csv").is_file())
        for part in ("MOMENTUM BACKTEST", "SURVIVORSHIP", "Pre-stated test", "vs equal-weight universe",
                     "vs Nifty 50", "Calendar years", "Trading and costs", "No orders placed"):
            self.assertIn(part, text)

    def test_cache_is_reused(self):
        dates = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=30)
        f = candle_frame([100.0] * 30, dates)

        class Counting(FakeMD):
            calls = 0

            def candles(self, *a):
                Counting.calls += 1
                return super().candles(*a)

        md = Counting({"X.NS": f})
        with tempfile.TemporaryDirectory() as tmp:
            start, end = dates[0].date(), dates[-1].date()
            mb._cached("X.NS", start, end, tmp, md, False)
            mb._cached("X.NS", start, end, tmp, md, False)
            self.assertEqual(Counting.calls, 1)
            mb._cached("X.NS", start, end, tmp, md, True)
            self.assertEqual(Counting.calls, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
