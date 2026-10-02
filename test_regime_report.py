"""Regime report: point-in-time VIX, classification edges, zero-trade sessions,
the pre-stated test and an end-to-end run on a synthetic log."""

import contextlib
import datetime as dt
import io
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import regime_report as rr
import stats

DAYS = [dt.date(2026, 3, 2) + dt.timedelta(days=i) for i in range(12)]
DAYS = [d for d in DAYS if d.weekday() < 5]          # 2026-03-02 is a Monday: 10 sessions


def candles(days, closes, opens=None, highs=None, lows=None):
    opens = opens or closes
    return pd.DataFrame(dict(
        Datetime=pd.to_datetime([str(d) for d in days]).tz_localize("Asia/Kolkata"),
        Open=opens, High=highs or [max(o, c) for o, c in zip(opens, closes)],
        Low=lows or [min(o, c) for o, c in zip(opens, closes)], Close=closes,
        Volume=[0] * len(days)))


class FakeMD:
    def __init__(self, nifty, vix):
        self.frames = {rr.NIFTY: nifty, rr.VIX: vix}
        self.calls = []

    def candles(self, symbol, interval, start, end):
        self.calls.append((symbol, interval, start, end))
        return self.frames[symbol]


def flat_nifty(days):
    return candles(days, [100.0] * len(days))


class TestMarketDays(unittest.TestCase):
    def test_vix_is_the_previous_close_not_todays(self):
        vix = candles(DAYS, [10.0 + i for i in range(len(DAYS))])
        mkt = rr.market_days(FakeMD(flat_nifty(DAYS), vix), str(DAYS[0]), str(DAYS[-1]))
        self.assertTrue(math.isnan(mkt.loc[str(DAYS[0]), "vix_prev"]),
                        "no earlier VIX in the data, so unknown")
        self.assertEqual(mkt.loc[str(DAYS[3]), "vix_prev"], 12.0)

    def test_todays_vix_cannot_move_todays_value(self):
        base = candles(DAYS, [10.0] * len(DAYS))
        spiked = candles(DAYS, [10.0] * (len(DAYS) - 1) + [99.0])
        a = rr.market_days(FakeMD(flat_nifty(DAYS), base), str(DAYS[0]), str(DAYS[-1]))
        b = rr.market_days(FakeMD(flat_nifty(DAYS), spiked), str(DAYS[0]), str(DAYS[-1]))
        self.assertEqual(a.loc[str(DAYS[-1]), "vix_prev"], b.loc[str(DAYS[-1]), "vix_prev"])

    def test_missing_vix_day_falls_back_to_the_last_one_before(self):
        vix = candles(DAYS[:2] + DAYS[3:], [11.0, 13.0] + [20.0] * (len(DAYS) - 3))
        mkt = rr.market_days(FakeMD(flat_nifty(DAYS), vix), str(DAYS[0]), str(DAYS[-1]))
        self.assertEqual(mkt.loc[str(DAYS[3]), "vix_prev"], 13.0)

    def test_fetch_starts_before_the_first_session(self):
        md = FakeMD(flat_nifty(DAYS), candles(DAYS, [10.0] * len(DAYS)))
        rr.market_days(md, str(DAYS[0]), str(DAYS[-1]))
        self.assertTrue(all(c[2] < DAYS[0] and c[1] == "day" for c in md.calls))


class TestClassify(unittest.TestCase):
    def mkt(self, opens, closes, highs, lows, vix):
        idx = [str(d) for d in DAYS[:len(opens)]]
        return rr.classify(pd.DataFrame(dict(open=opens, high=highs, low=lows, close=closes,
                                             vix_prev=vix), index=idx))

    def test_day_type_edges(self):
        m = self.mkt([100] * 4, [100.5, 99.5, 100.49, 99.51], [101] * 4, [99] * 4, [10] * 4)
        self.assertEqual(list(m.day_type), ["up", "down", "sideways", "sideways"])

    def test_range_and_vix_buckets(self):
        m = self.mkt([100] * 4, [100] * 4, [100.3, 100.6, 101.2, 100.0],
                     [100.0, 100.0, 100.0, 100.0], [11.99, 12.0, 15.0, float("nan")])
        self.assertEqual(list(m.range_bucket), ["< 0.6%", "0.6-1.2%", ">= 1.2%", "< 0.6%"])
        self.assertEqual(list(m.vix_bucket), ["< 12", "12-15", "15-20", "unknown"])


def trades_frame(rows):
    return pd.DataFrame(rows, columns=["date", "strategy", "pnl", "gross", "cost"])


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.mkt = rr.classify(rr.market_days(
            FakeMD(flat_nifty(DAYS), candles(DAYS, [10, 20] * (len(DAYS) // 2))),
            str(DAYS[0]), str(DAYS[-1])))

    def test_no_trade_session_is_a_zero_not_missing(self):
        t = trades_frame([(str(DAYS[1]), "orb45", 50.0, 60.0, 10.0),
                          (str(DAYS[2]), "ema", -5.0, 0.0, 5.0)])
        s = rr.session_frame(t, "orb45", rr.sessions_of(t), self.mkt)
        self.assertEqual(len(s), 2)
        self.assertEqual(s.loc[str(DAYS[2]), "pnl"], 0.0)
        self.assertEqual(s.loc[str(DAYS[2]), "trades"], 0)

    def test_session_without_market_data_is_unknown(self):
        later = str(DAYS[-1] + dt.timedelta(days=7))
        t = trades_frame([(later, "orb45", 1.0, 2.0, 1.0)])
        s = rr.session_frame(t, "orb45", rr.sessions_of(t), self.mkt)
        self.assertEqual(s.loc[later, "vix_bucket"], "unknown")
        self.assertEqual(s.loc[later, "day_type"], "unknown")

    def test_group_table_counts_and_rates(self):
        t = trades_frame([(str(DAYS[1]), "orb45", 30.0, 40.0, 10.0),
                          (str(DAYS[1]), "orb45", -10.0, 0.0, 10.0),
                          (str(DAYS[2]), "orb45", 5.0, 15.0, 10.0)])
        s = rr.session_frame(t, "orb45", rr.sessions_of(t), self.mkt)
        tab = rr.group_table(s, "vix_bucket", [b[2] for b in rr.VIX_BUCKETS]).set_index("group")
        # DAYS[1] opens after VIX 10 (< 12); DAYS[2] after VIX 20 (>= 20).
        self.assertEqual(tab.loc["< 12", "trades"], 2)
        self.assertAlmostEqual(tab.loc["< 12", "gross_tr"], 20.0)
        self.assertAlmostEqual(tab.loc["< 12", "net"], 20.0)
        self.assertEqual(tab.loc[">= 20", "sessions"], 1)
        self.assertIsNone(tab.loc[">= 20", "lo"], "one session has no spread")


class TestVixTest(unittest.TestCase):
    def sess(self, vix, pnl):
        idx = [f"2026-03-{i + 1:02d}" for i in range(len(vix))]
        return pd.DataFrame(dict(vix_prev=vix, pnl=pnl, gross=pnl, trades=[1] * len(vix)), index=idx)

    def test_clear_difference_is_reported(self):
        s = self.sess([20, 21, 22, 23, 10, 11, 12, 13], [100, 110, 90, 105, 0, 5, -5, 2])
        t = rr.vix_test(s)
        self.assertEqual((t["high_n"], t["low_n"]), (4, 4))
        self.assertAlmostEqual(t["diff"], 101.25 - 0.5)
        self.assertEqual(rr.verdict(t["lo"], t["hi"]), "BETTER after high VIX")

    def test_split_is_inclusive_and_unknown_vix_is_left_out(self):
        s = self.sess([15.0, 14.99, float("nan")], [1, 2, 3])
        t = rr.vix_test(s)
        self.assertEqual((t["high_n"], t["low_n"]), (1, 1))
        self.assertEqual(rr.verdict(t["lo"], t["hi"]), "too few sessions to tell")

    def test_noise_is_not_distinguishable(self):
        s = self.sess([20, 21, 22, 10, 11, 12], [50, -40, 10, 30, -35, 5])
        t = rr.vix_test(s)
        self.assertEqual(rr.verdict(t["lo"], t["hi"]), "not distinguishable")

    def test_stretches(self):
        self.assertEqual(rr.stretches([True, True, False, True, False, False, True]), 3)
        self.assertEqual(rr.stretches([]), 0)


class TestStats(unittest.TestCase):
    def test_mean_ci(self):
        m, lo, hi = stats.mean_ci([1.0, 2.0, 3.0])
        self.assertEqual(m, 2.0)
        self.assertAlmostEqual(hi - m, 1.96 * 1.0 / math.sqrt(3))
        self.assertEqual(stats.mean_ci([5.0]), (5.0, None, None))
        self.assertEqual(stats.mean_ci([]), (None, None, None))

    def test_diff_ci_is_welch(self):
        d, lo, hi = stats.diff_ci([1.0, 3.0], [0.0, 0.0, 0.0])
        self.assertEqual(d, 2.0)
        self.assertAlmostEqual(hi - d, 1.96 * math.sqrt(2.0 / 2 + 0.0 / 3))


class TestEndToEnd(unittest.TestCase):
    def test_report_runs_on_a_synthetic_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            for i, d in enumerate(DAYS):
                for s in ("orb45", "ema"):
                    rows.append(dict(date=str(d), strategy=s, symbol="X", pnl=float(i - 3),
                                     gross=float(i), cost=3.0))
            pd.DataFrame(rows).to_csv(Path(tmp) / "strategy_trades.csv", index=False)
            md = FakeMD(candles(DAYS, [100 + i for i in range(len(DAYS))],
                                opens=[100 + i - 0.8 for i in range(len(DAYS))]),
                        candles(DAYS, [14.0, 16.0] * (len(DAYS) // 2)))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rr.run(None, "orb45", market_data=md, root=Path(tmp))
            text = out.getvalue()
        for part in ("MARKET REGIME REPORT", "1. Pre-stated test", "2. By previous-day",
                     "3. By Nifty day type", "4. By Nifty day range", "5. By month",
                     "orb45", "ema", "No orders placed"):
            self.assertIn(part, text)
        self.assertNotIn("nan", text.lower())

    def test_unknown_log_id_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            pd.DataFrame([dict(date="2026-03-02", pnl=1.0)]).to_csv(
                Path(tmp) / "orb_trades.csv", index=False)
            with self.assertRaises(SystemExit):
                rr.pick_log("nope", Path(tmp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
