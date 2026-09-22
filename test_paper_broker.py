"""Paper broker and report checks. Synthetic bars, no Kite, no network."""

import datetime
import json
import os
import shutil
import tempfile
import unittest

import pandas as pd

import daily_plan as dp
import orb_backtest as ob
import paper_broker as pb
import paper_report as pr
import symbol_screener as sc


def bars(day, prices, start="09:15", minutes=15):
    """Intraday frame in the shape load_many returns. prices: [(o,h,l,c), ...]"""
    t0 = datetime.datetime.combine(day, datetime.time.fromisoformat(start))
    rows = []
    for i, (o, h, l, c) in enumerate(prices):
        t = t0 + datetime.timedelta(minutes=minutes * i)
        rows.append(dict(dt=t, date=day, time=t.strftime("%H:%M"),
                         Open=o, High=h, Low=l, Close=c))
    return pd.DataFrame(rows)


DAY = datetime.date(2026, 3, 2)


class PaperDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (pb.PAPER_DIR, pb.PLANS_DIR, pb.LEDGER)
        pb.PAPER_DIR = self.tmp
        pb.PLANS_DIR = os.path.join(self.tmp, "plans")
        pb.LEDGER = os.path.join(self.tmp, "ledger.csv")

    def tearDown(self):
        pb.PAPER_DIR, pb.PLANS_DIR, pb.LEDGER = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestFingerprint(unittest.TestCase):
    def test_stable_and_sensitive(self):
        a, _ = pb.config_fingerprint()
        self.assertEqual(a, pb.config_fingerprint()[0], "must be deterministic")
        saved = ob.MAX_POSITIONS
        try:
            ob.MAX_POSITIONS = saved + 1
            self.assertNotEqual(a, pb.config_fingerprint()[0],
                                "a frozen param changing must move the hash")
        finally:
            ob.MAX_POSITIONS = saved

    def test_covers_cost_model(self):
        """Cost parameters must be inside the freeze: changing slippage changes
        every trade's P&L, so a silent edit has to be detectable."""
        before, _ = pb.config_fingerprint()
        saved = ob.SLIPPAGE_TIERS
        try:
            ob.SLIPPAGE_TIERS = [(1000, 0.01)]
            self.assertNotEqual(before, pb.config_fingerprint()[0])
        finally:
            ob.SLIPPAGE_TIERS = saved
        self.assertEqual(before, pb.config_fingerprint()[0], "must restore")


class TestReplay(unittest.TestCase):
    """The core guarantee: resolution uses PLAN levels, never fresh ones."""

    def setUp(self):
        self.n_or = 3          # 45m range on 15m candles
        self.row = dict(symbol="T", or_high=100.0, or_low=98.0,
                        sl_pct=0.01, tgt_pct=0.02,
                        qty_long=500, qty_short=500)

    def test_long_target(self):
        g = bars(DAY, [(99, 100, 98, 99)] * 3 + [(100, 102.5, 100, 102.4)])
        t = pb._replay(DAY, g, self.row, self.n_or, 2000)
        self.assertEqual(t["side"], "LONG")
        self.assertEqual(t["reason"], "target")
        self.assertGreater(t["pnl"], 0)

    def test_short_stop(self):
        g = bars(DAY, [(99, 100, 98, 99)] * 3 + [(98, 99.5, 97.5, 99.4)])
        t = pb._replay(DAY, g, self.row, self.n_or, 2000)
        self.assertEqual(t["side"], "SHORT")
        self.assertEqual(t["reason"], "stoploss")
        self.assertLess(t["pnl"], 0)

    def test_no_breakout_is_no_trade(self):
        g = bars(DAY, [(99, 100, 98, 99)] * 6)
        self.assertIsNone(pb._replay(DAY, g, self.row, self.n_or, 2000))

    def test_uses_plan_levels_not_recomputed_range(self):
        """The bars carry a WIDER range than the plan recorded. If resolution
        recomputed the range it would not trigger; using the plan it must."""
        g = bars(DAY, [(99, 120, 80, 99)] * 3 + [(100, 102.5, 100, 102.4)])
        t = pb._replay(DAY, g, self.row, self.n_or, 2000)
        self.assertIsNotNone(t, "must trade off the recorded 100.0, not a fresh range")
        self.assertEqual(t["side"], "LONG")

    def test_entry_drift_recorded(self):
        """A gap through the level fills worse than the published trigger."""
        g = bars(DAY, [(99, 100, 98, 99)] * 3 + [(101, 103, 101, 102.9)])
        t = pb._replay(DAY, g, self.row, self.n_or, 2000)
        self.assertEqual(t["planned_trigger"], 100.0)
        self.assertAlmostEqual(t["entry"], 101.0)
        self.assertAlmostEqual(t["entry_drift"], 1.0, places=4)
        self.assertGreater(t["entry_drift_pct"], 0)

    def test_squareoff_closes_open_position(self):
        late = [(99, 100, 98, 99)] * 3 + [(100, 100.5, 99.9, 100.2)]
        g = bars(DAY, late, start="14:15")
        t = pb._replay(DAY, g, self.row, self.n_or, 2000)
        self.assertIsNotNone(t)
        self.assertIn(t["reason"], ("squareoff", "eod"))

    def test_costs_match_the_backtest(self):
        """Ledger rows must use the same cost model as orb_backtest, or the
        paired comparison against the backtest is meaningless."""
        g = bars(DAY, [(99, 100, 98, 99)] * 3 + [(100, 102.5, 100, 102.4)])
        t = pb._replay(DAY, g, self.row, self.n_or, 2000)
        ref = ob._pnl(DAY, "LONG", t["entry"], t["exit"], t["entry_time"],
                      t["exit_time"], t["reason"], t["ambiguous"], "T",
                      t["qty"], 2000, 0.01, 0.02)
        self.assertAlmostEqual(t["cost"], ref["cost"], places=6)
        self.assertAlmostEqual(t["pnl"], ref["pnl"], places=6)


class TestPlanImmutability(PaperDirCase):
    def _write(self, day=DAY):
        os.makedirs(pb.PLANS_DIR, exist_ok=True)
        with open(pb.plan_path(day), "w") as fh:
            json.dump({"date": str(day), "rows": [], "slot_budget": 5000,
                       "config_fingerprint": "abc"}, fh)

    def test_second_plan_refused(self):
        self._write()
        with self.assertRaises(pb.PlanExists):
            pb.write_plan(DAY)

    def test_missing_plan_raises(self):
        with self.assertRaises(pb.PlanMissing):
            pb.read_plan(DAY)

    def test_resolve_twice_refused(self):
        self._write()
        os.makedirs(pb.PAPER_DIR, exist_ok=True)
        pd.DataFrame([dict(plan_date=str(DAY), pnl=1.0)]).to_csv(pb.LEDGER, index=False)
        with self.assertRaises(pb.PlanExists):
            pb.resolve(DAY)


class TestLedger(PaperDirCase):
    def test_append_only(self):
        pb.append_ledger(pd.DataFrame([dict(plan_date="2026-03-02", pnl=10.0)]))
        pb.append_ledger(pd.DataFrame([dict(plan_date="2026-03-03", pnl=-4.0)]))
        led = pb.read_ledger()
        self.assertEqual(len(led), 2)
        self.assertEqual(led.pnl.sum(), 6.0)

    def test_empty_ledger_is_empty_frame(self):
        self.assertTrue(pb.read_ledger().empty)


class TestReportStats(unittest.TestCase):
    def test_wilson_bounds(self):
        lo, hi = pr._wilson(5, 10)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(hi, 1.0)

    def test_wilson_handles_zero_n(self):
        self.assertEqual(pr._wilson(0, 0), (0.0, 0.0))

    def test_pnl_ci_needs_two_trades(self):
        self.assertEqual(pr._pnl_ci(pd.Series([5.0])), (None, None))
        lo, hi = pr._pnl_ci(pd.Series([5.0, -3.0, 2.0]))
        self.assertLess(lo, hi)

    def test_breakeven_from_realised(self):
        led = pd.DataFrame(dict(pnl=[100.0, 100.0, -50.0, -50.0]))
        # avg win 100, avg loss 50 -> break-even 50/150
        self.assertAlmostEqual(pr.breakeven_win_rate(led), 1 / 3, places=6)

    def test_breakeven_needs_both_sides(self):
        self.assertIsNone(pr.breakeven_win_rate(pd.DataFrame(dict(pnl=[1.0, 2.0]))))

    def test_summary_flags_straddling_zero(self):
        led = pd.DataFrame(dict(pnl=[500.0, -480.0, 300.0, -310.0],
                                gross=[1.0] * 4, cost=[1.0] * 4,
                                plan_date=["a", "a", "b", "b"]))
        s = pr.summarise(led)
        self.assertLessEqual(s["pnl_lo"], 0)
        self.assertGreaterEqual(s["pnl_hi"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FakeMarketData:
    """Minimal stand-in for KiteMarketData: daily() for the screener, intraday()
    for the replay. Two symbols, one that breaks out and one that does not."""

    SYMS = ["AAA", "BBB"]

    def daily(self, symbol, trading_days):
        if symbol not in self.SYMS:
            return pd.DataFrame().set_index(pd.DatetimeIndex([], name="Datetime"))
        end = pd.Timestamp(DAY)
        idx = pd.bdate_range(end=end - pd.Timedelta(days=1), periods=trading_days)
        base = 100.0 if symbol == "AAA" else 50.0
        f = pd.DataFrame(index=idx)
        f.index.name = "Datetime"
        # Gentle drift with a 2% daily range, and turnover well over the floor.
        f["Open"] = base
        f["High"] = base * 1.01
        f["Low"] = base * 0.99
        f["Close"] = base
        f["Volume"] = 60_000_000 if symbol == "AAA" else 150_000_000
        return f

    def intraday(self, symbol, interval, calendar_days):
        # AAA breaks its range to the upside and reaches target; BBB stays inside.
        if symbol == "AAA":
            px = [(99, 100, 98, 99)] * 3 + [(100, 103, 100, 102.8)] * 3
        else:
            px = [(49, 50, 48, 49)] * 6
        b = bars(DAY, px)
        # Real Kite candles come back tz-aware. Naive ones are read as UTC and
        # shift +5:30, which pushes the session past SQUAREOFF_TIME and silently
        # produces no trades.
        b["Datetime"] = pd.to_datetime(b["dt"]).dt.tz_localize(dp.IST)
        return b[["Datetime", "Open", "High", "Low", "Close"]].assign(Volume=1000)


class TestEndToEnd(PaperDirCase):
    """plan -> resolve -> report over a fake feed, exercising the real wiring."""

    def setUp(self):
        super().setUp()
        self._universe = sc.load_universe
        self._today = dp.today_ist
        sc.load_universe = lambda *a, **k: list(FakeMarketData.SYMS)
        dp.today_ist = lambda: DAY

    def tearDown(self):
        sc.load_universe = self._universe
        dp.today_ist = self._today
        super().tearDown()

    def test_plan_then_resolve_then_report(self):
        md = FakeMarketData()
        plan = pb.write_plan(DAY, market_data=md)

        self.assertTrue(os.path.exists(pb.plan_path(DAY)))
        self.assertEqual(plan["day_budget"], ob.DAY_BUDGET)
        self.assertTrue(plan["rows"], "expected levels for at least one symbol")
        for r in plan["rows"]:
            self.assertIn("or_high", r)
            self.assertIn("or_low", r)
            self.assertGreater(r["sl_pct"], 0)
            self.assertGreater(r["tgt_pct"], r["sl_pct"])

        # committing twice must be refused
        with self.assertRaises(pb.PlanExists):
            pb.write_plan(DAY, market_data=md)

        tr, plan2, fp = pb.resolve(DAY, market_data=md)
        self.assertEqual(fp, plan["config_fingerprint"],
                         "config must not drift between plan and resolve")
        self.assertFalse(tr.empty, "AAA should have triggered")
        self.assertIn("entry_drift_pct", tr.columns)
        self.assertIn("plan_fingerprint", tr.columns)

        led = pb.read_ledger()
        self.assertEqual(len(led), len(tr))
        # resolving the same day again must be refused: the ledger is append-only
        with self.assertRaises(pb.PlanExists):
            pb.resolve(DAY, market_data=md)

        pr.report(pb.read_ledger())      # must not raise on a real ledger
