"""VWAP, Bollinger and EMA strategies, and the comparison runner.

Synthetic bars only, no Kite. The tests that matter most are the causality
ones: a decision taken at a bar's close must not change when that bar's
successors, including the bar it fills on, are altered.
"""

import datetime
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

import numpy as np
import pandas as pd

import orb_backtest as ob
import strategies as st
import strategy_backtest as sb
import symbol_screener as sc

DAY = datetime.date(2026, 3, 2)


def mk(day, rows, start="09:15", minutes=15, vol=100):
    """rows: [(o, h, l, c)] or [(o, h, l, c, v)] -> frame shaped like ob._normalize."""
    t0 = datetime.datetime.combine(day, datetime.time.fromisoformat(start))
    out = []
    for i, r in enumerate(rows):
        o, h, l, c = r[:4]
        v = r[4] if len(r) > 4 else vol
        t = t0 + datetime.timedelta(minutes=minutes * i)
        out.append(dict(dt=t, date=day, time=t.strftime("%H:%M"),
                        Open=float(o), High=float(h), Low=float(l), Close=float(c),
                        Volume=float(v)))
    return pd.DataFrame(out)


# VWAP scenario: bar 1 closes 2.6% above VWAP with a 2% ATR (threshold 1%),
# so SHORT is decided at bar 1's close and fills at bar 2's open (104).
VWAP_ROWS = [
    (100, 100.5, 99.5, 100),
    (100, 104, 100, 104),
    (104, 104.2, 103, 103.2),
    (103, 103.1, 101.9, 102),
    (102, 102.5, 101.5, 102),
]


def run_vwap(rows, start="09:15", atr=2.0):
    hist = st.VWAPReversion().prepare(mk(DAY, rows, start))
    return st.trade_day(st.VWAPReversion(), DAY, hist, "T", 100000, 2000, atr)


class TestIndicators(unittest.TestCase):
    def test_vwap_matches_hand_calculation(self):
        g = st.add_vwap(mk(DAY, VWAP_ROWS[:2]))
        tp0 = (100.5 + 99.5 + 100) / 3
        tp1 = (104 + 100 + 104) / 3
        self.assertAlmostEqual(g.vwap.iloc[0], tp0)
        self.assertAlmostEqual(g.vwap.iloc[1], (tp0 + tp1) / 2)

    def test_vwap_is_volume_weighted(self):
        g = st.add_vwap(mk(DAY, [(100, 100, 100, 100, 900), (110, 110, 110, 110, 100)]))
        self.assertAlmostEqual(g.vwap.iloc[1], 101.0)

    def test_vwap_resets_each_session(self):
        d2 = DAY + datetime.timedelta(days=1)
        both = pd.concat([mk(DAY, [(100, 100, 100, 100)] * 3),
                          mk(d2, [(200, 200, 200, 200)] * 3)], ignore_index=True)
        g = st.add_vwap(both)
        self.assertAlmostEqual(g[g.date == d2].vwap.iloc[0], 200.0,
                               msg="yesterday's volume must not leak into today")

    def test_zero_volume_does_not_divide_by_zero(self):
        g = st.add_vwap(mk(DAY, [(100, 101, 99, 100, 0), (100, 101, 99, 100, 0)]))
        self.assertTrue(np.isfinite(g.vwap).all())

    def test_bollinger_matches_pandas(self):
        closes = [100 + (i % 5) for i in range(30)]
        g = st.add_bollinger(mk(DAY, [(c, c, c, c) for c in closes]))
        s = pd.Series(closes, dtype=float)
        mid = s.rolling(20).mean()
        sd = s.rolling(20).std(ddof=0)
        np.testing.assert_allclose(g.bb_mid.values[19:], mid.values[19:])
        np.testing.assert_allclose(g.bb_up.values[19:], (mid + 2 * sd).values[19:])
        self.assertTrue(g.bb_mid.iloc[:19].isna().all(), "no band before 20 bars")

    def test_ema_warmup_is_masked(self):
        g = st.add_ema(mk(DAY, [(100, 100, 100, 100)] * 30))
        self.assertTrue(g.ema_slow.iloc[:st.EMA_SLOW].isna().all())
        self.assertTrue(g.ema_slow.iloc[st.EMA_SLOW:].notna().all())

    def test_indicators_are_causal(self):
        """Changing the last bar must leave every earlier indicator value alone."""
        rows = [(100 + i % 7, 101 + i % 7, 99 + i % 7, 100 + i % 5) for i in range(40)]
        a = mk(DAY, rows)
        b = mk(DAY, rows[:-1] + [(500, 600, 400, 550)])
        for add, cols in ((st.add_vwap, ["vwap"]),
                          (st.add_bollinger, ["bb_mid", "bb_up", "bb_lo"]),
                          (st.add_ema, ["ema_fast", "ema_slow"])):
            ga, gb = add(a), add(b)
            for c in cols:
                np.testing.assert_allclose(ga[c].values[:-1], gb[c].values[:-1],
                                           err_msg=f"{c} looked ahead")


class TestVWAPTrade(unittest.TestCase):
    def test_short_fade_exits_at_vwap(self):
        t = run_vwap(VWAP_ROWS)
        self.assertEqual(t["side"], "SHORT")
        self.assertEqual(t["entry"], 104.0, "fills at the NEXT bar's open")
        self.assertEqual(t["entry_time"], "09:45")
        self.assertEqual(t["reason"], "target")
        self.assertLess(t["exit"], t["entry"])
        self.assertGreater(t["gross"], 0)

    def test_decision_ignores_the_fill_bar_and_after(self):
        """The core no-lookahead guarantee: rewrite every bar from the fill
        onward (keeping only the fill bar's open) and the entry must not move."""
        base = run_vwap(VWAP_ROWS)
        altered = VWAP_ROWS[:2] + [(104, 150, 60, 70), (70, 300, 10, 200), (1, 2, 1, 1)]
        t = run_vwap(altered)
        self.assertEqual((t["side"], t["entry"], t["entry_time"]),
                         (base["side"], base["entry"], base["entry_time"]))

    def test_no_trade_below_the_stretch(self):
        calm = [(100, 100.3, 99.7, 100)] * 6
        self.assertIsNone(run_vwap(calm))

    def test_stop_gapped_through_fills_at_the_open(self):
        rows = VWAP_ROWS[:3] + [(110, 111, 109, 110)] + VWAP_ROWS[4:]
        t = run_vwap(rows)
        self.assertEqual(t["reason"], "stoploss")
        self.assertEqual(t["exit"], 110.0, "a gap through the stop is worse than the stop")

    def test_bar_spanning_stop_and_target_is_a_stop(self):
        rows = VWAP_ROWS[:3] + [(103.5, 106, 100, 103)] + VWAP_ROWS[4:]
        t = run_vwap(rows)
        self.assertEqual(t["reason"], "stoploss")
        self.assertTrue(t["ambiguous"])

    def test_no_entries_after_cutoff(self):
        self.assertIsNone(run_vwap(VWAP_ROWS, start="14:15"))

    def test_open_position_is_squared_off(self):
        rows = VWAP_ROWS[:3] + [(104, 104.3, 103.8, 104)] * 2
        t = run_vwap(rows, start="14:00")
        self.assertIsNotNone(t)
        self.assertIn(t["reason"], ("squareoff", "eod"))

    def test_costs_are_the_orb_cost_model(self):
        t = run_vwap(VWAP_ROWS)
        # t["exit"] is rounded to 2dp for display; the fill was the exact VWAP
        # carried from bar 2's close, so recompute from that, not the rounding.
        exact_exit = st.add_vwap(mk(DAY, VWAP_ROWS)).vwap.iloc[2]
        self.assertAlmostEqual(t["exit"], round(exact_exit, 2))
        ref = ob._pnl(DAY, t["side"], t["entry"], exact_exit, t["entry_time"],
                      t["exit_time"], t["reason"], t["ambiguous"], "T", t["qty"],
                      2000, t["sl_pct"] / 100, None)
        self.assertAlmostEqual(t["cost"], ref["cost"], places=6)
        self.assertAlmostEqual(t["pnl"], ref["pnl"], places=6)


class TestBollingerTrade(unittest.TestCase):
    def test_close_above_upper_band_is_faded_to_the_middle(self):
        prior = DAY - datetime.timedelta(days=1)
        warm = mk(prior, [(100 + (i % 2) * 0.2,) * 4 for i in range(25)])
        today = mk(DAY, [(100, 100.2, 99.9, 100.1), (100.1, 103, 100.1, 103),
                         (103, 103.1, 102, 102.2), (102.2, 102.3, 100, 100.2),
                         (100.2, 100.4, 100, 100.2)])
        s = st.BollingerReversion()
        hist = s.prepare(pd.concat([warm, today], ignore_index=True))
        t = st.trade_day(s, DAY, hist, "B", 100000, 2000, 3.0)
        self.assertEqual(t["side"], "SHORT")
        self.assertEqual(t["entry"], 103.0)
        self.assertEqual(t["reason"], "target")


class TestEMATrade(unittest.TestCase):
    def test_enters_on_cross_and_exits_on_the_opposite_cross(self):
        prior = DAY - datetime.timedelta(days=1)
        down = [110 - i * 0.4 for i in range(25)]
        warm = mk(prior, [(c, c + 0.1, c - 0.1, c) for c in down])
        path = [100, 100.5, 102, 104, 106, 107, 105, 102, 99, 97, 96, 96, 96]
        today = mk(DAY, [(c, c + 0.1, c - 0.1, c) for c in path])
        s = st.EMACross()
        hist = s.prepare(pd.concat([warm, today], ignore_index=True))
        t = st.trade_day(s, DAY, hist, "E", 100000, 2000, 10.0)   # wide stop
        self.assertEqual(t["side"], "LONG")
        self.assertEqual(t["reason"], "signal")

        g = hist[hist.date == DAY].reset_index(drop=True)
        diff = g.ema_fast - g.ema_slow
        up = next(i for i in range(1, len(g)) if diff[i - 1] <= 0 < diff[i])
        dn = next(i for i in range(up + 1, len(g)) if diff[i - 1] >= 0 > diff[i])
        self.assertEqual(t["entry"], g.Open[up + 1], "fills the bar after the cross")
        self.assertEqual(t["exit"], g.Open[dn + 1], "exits the bar after the opposite cross")


class TestWatchlist(unittest.TestCase):
    def test_tags_strategy_and_uses_point_in_time_metrics(self):
        intraday = {"T": mk(DAY, VWAP_ROWS)}
        picks = {DAY: ["T"]}
        tr = st.backtest_watchlist(st.STRATEGIES["vwap"], intraday, picks,
                                   {(DAY, "T"): dict(atr_pct=2.0, turnover_cr=2000)})
        self.assertEqual(len(tr), 1)
        self.assertEqual(tr.iloc[0]["strategy"], "vwap")
        # without the ATR the stretch threshold has no unit, so no trade
        none = st.backtest_watchlist(st.STRATEGIES["vwap"], intraday, picks, {})
        self.assertTrue(none.empty)


class TestRunnerPieces(unittest.TestCase):
    def test_paired_difference(self):
        sessions = [DAY, DAY + datetime.timedelta(days=1)]
        a = pd.DataFrame(dict(date=[DAY, DAY], pnl=[10.0, 5.0]))
        b = pd.DataFrame(dict(date=[sessions[1]], pnl=[4.0]))
        tot, lo, hi, share = sb.paired(a, b, sessions)
        self.assertAlmostEqual(tot, 11.0)       # (15 - 0) + (0 - 4)
        self.assertLess(lo, tot)
        self.assertGreater(hi, tot)
        self.assertEqual(share, 50.0)

    def test_orb_override_matches_the_frozen_engine(self):
        """orb_watchlist at the configured range length must reproduce
        ob.backtest_watchlist exactly: only n_or is allowed to differ."""
        rows = [(99, 100, 98, 99)] * 3 + [(100, 102.5, 100, 102.4)] + \
               [(102.4, 102.6, 102, 102.3)] * 20
        intraday = {"T": mk(DAY, rows)}
        picks, metrics = {DAY: ["T"]}, {(DAY, "T"): dict(atr_pct=2.0, turnover_cr=2000)}
        a = sb.orb_watchlist(intraday, picks, metrics, ob.or_candles())
        b, _ = ob.backtest_watchlist(intraday, picks, metrics)
        pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))

    def test_excursion_skips_the_entry_bar(self):
        """The entry bar's extreme may predate the fill, so it must not count."""
        rows = [(99, 100, 98, 99)] * 3 + [(100, 150, 100, 101), (101, 102, 100.5, 101.5)]
        intraday = {"T": mk(DAY, rows)}
        trade = pd.DataFrame([dict(symbol="T", date=DAY, side="LONG", entry=100.0,
                                   entry_time="10:00")])
        ex = sb.excursions(trade, intraday, {(DAY, "T"): dict(atr_pct=2.0)}, 3)
        self.assertAlmostEqual(ex.mfe_pct.iloc[0], 2.0, msg="150 on the entry bar ignored")
        self.assertAlmostEqual(ex.mfe_atr.iloc[0], 1.0)
        self.assertAlmostEqual(ex.or_width_pct.iloc[0], 2.0)


class FakeMD:
    """Daily bars for the screener, intraday at any interval for the runner."""

    SYMS = ["AAA.NS", "BBB.NS"]

    def __init__(self):
        self.end = pd.Timestamp(DAY)
        self.days = pd.bdate_range(end=self.end, periods=160)

    def daily(self, symbol, n):
        rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
        base = 200.0 if symbol == "AAA.NS" else 400.0
        px = base * np.cumprod(1 + rng.normal(0, 0.01, len(self.days)))
        f = pd.DataFrame(index=pd.DatetimeIndex(self.days, name="Datetime"))
        f["Open"], f["Close"] = px, px * (1 + rng.normal(0, 0.005, len(px)))
        f["High"] = np.maximum(f.Open, f.Close) * 1.01
        f["Low"] = np.minimum(f.Open, f.Close) * 0.99
        f["Volume"] = 8e9 / px                    # ~800 cr turnover, over the floor
        return f.tail(n)

    def intraday(self, symbol, interval, calendar_days):
        step = int(interval.rstrip("m"))
        rng = np.random.default_rng((abs(hash(symbol)) + step) % 2**32)
        base = 200.0 if symbol == "AAA.NS" else 400.0
        frames = []
        for d in self.days[-45:]:
            n = (375 // step)
            px = base * np.cumprod(1 + rng.normal(0, 0.004, n))
            rows = [(p, p * 1.003, p * 0.997, p * (1 + rng.normal(0, 0.002)),
                     rng.integers(1000, 5000)) for p in px]
            frames.append(mk(d.date(), rows, minutes=step))
        g = pd.concat(frames, ignore_index=True)
        g["Datetime"] = pd.to_datetime(g["dt"]).dt.tz_localize("Asia/Kolkata")
        return g[["Datetime", "Open", "High", "Low", "Close", "Volume"]]


class TestEndToEnd(unittest.TestCase):
    def test_every_strategy_runs_on_the_same_picks(self):
        tmp = tempfile.mkdtemp()
        cwd, saved_universe = os.getcwd(), sc.load_universe
        sc.load_universe = lambda *a, **k: list(FakeMD.SYMS)
        try:
            os.chdir(tmp)
            with redirect_stdout(io.StringIO()) as out:
                results, reach = sb.run(sb.ALL, market_data=FakeMD())
            text = out.getvalue()
            self.assertEqual(list(results), list(sb.ALL))
            self.assertTrue(any(not tr.empty for tr in results.values()))
            self.assertIn("STRATEGY COMPARISON", text)
            self.assertIn("Paired against orb45", text)
            self.assertIn("orb45", reach)
            self.assertTrue(os.path.exists("strategy_trades.csv"))
            log = pd.read_csv("strategy_trades.csv")
            self.assertTrue(set(log.strategy) <= set(sb.ALL))
            # identical picks: no strategy may trade a symbol the screener
            # did not hand it
            self.assertTrue(set(log.symbol) <= set(FakeMD.SYMS))
        finally:
            os.chdir(cwd)
            sc.load_universe = saved_universe
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
