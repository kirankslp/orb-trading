"""Futures re-pricing: charges, one-lot sizing, both sides, coverage,
break-even slippage, capital and an end-to-end run."""

import contextlib
import datetime as dt
import io
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import futures_reprice as fp
import orb_backtest as ob
from kite_data import KiteMarketData


def trade(date="2026-03-02", strategy="orb45", symbol="RELIANCE.NS", side="LONG",
          entry=1000.0, exit=1010.0, slip_pct=0.03, turnover_cr=2000.0):
    return dict(date=date, strategy=strategy, symbol=symbol, side=side, qty=9,
                entry=entry, exit=exit, slip_pct=slip_pct, turnover_cr=turnover_cr,
                gross=0.0, cost=0.0, pnl=0.0)


class TestCharges(unittest.TestCase):
    def test_hand_computed_round_trip(self):
        # brokerage 20+20, exchange 17.3, SEBI 1, STT 250, stamp 10, GST 18% of 58.3
        self.assertAlmostEqual(fp.futures_charges(500000, 500000, 0.0005), 328.794, places=3)

    def test_brokerage_is_capped_per_order(self):
        small = fp.futures_charges(10000, 10000, 0.0)
        big = fp.futures_charges(1e6, 1e6, 0.0)
        self.assertAlmostEqual(small - (20000 * (fp.FUT_EXCH_PCT + fp.FUT_SEBI_PCT) * 1.18
                                        + 10000 * fp.FUT_STAMP_BUY_PCT), 6 * 1.18, places=6)
        self.assertLess(big / 2e6, small / 2e4, "the cap makes a big trade cheaper per rupee")

    def test_stt_only_on_the_sell_leg_and_stamp_only_on_the_buy(self):
        base = fp.futures_charges(100000, 100000, 0.0005)
        self.assertAlmostEqual(fp.futures_charges(100000, 200000, 0.0005) - base,
                               100000 * (0.0005 + fp.FUT_EXCH_PCT + fp.FUT_SEBI_PCT
                                         + (fp.FUT_EXCH_PCT + fp.FUT_SEBI_PCT) * 0.18), places=6)


class TestReprice(unittest.TestCase):
    def test_long_and_short_at_one_lot(self):
        t = pd.DataFrame([trade(side="LONG", entry=1000, exit=1010),
                          trade(side="SHORT", entry=1000, exit=1010)])
        fr = fp.reprice(t, {"RELIANCE": 500})
        self.assertEqual(list(fr.gross), [5000.0, -5000.0])
        self.assertEqual(list(fr.notional), [500000.0, 500000.0])
        self.assertEqual(list(fr.turnover), [1005000.0, 1005000.0])

    def test_short_swaps_the_legs(self):
        t = pd.DataFrame([trade(side="SHORT", entry=1000, exit=900)])
        r = fp.reprice(t, {"RELIANCE": 100}).iloc[0]
        # bought back at 900 (90,000), sold at 1000 (1,00,000)
        self.assertAlmostEqual(r["fut STT 0.05%"], fp.futures_charges(90000, 100000, 0.0005))
        self.assertAlmostEqual(r["cash_charges"], ob.charges(90000, 100000))

    def test_slippage_uses_the_logs_tier(self):
        t = pd.DataFrame([trade(slip_pct=0.05, entry=100, exit=100)])
        r = fp.reprice(t, {"RELIANCE": 1000}).iloc[0]
        self.assertAlmostEqual(r["slip"], 200000 * 0.0005)

    def test_missing_tier_falls_back_to_the_model(self):
        t = pd.DataFrame([trade(slip_pct=float("nan"), turnover_cr=float("nan"), entry=100, exit=100)])
        r = fp.reprice(t, {"RELIANCE": 1000}).iloc[0]
        self.assertAlmostEqual(r["slip"], 200000 * ob.SLIPPAGE_PCT)

    def test_stocks_without_futures_are_dropped(self):
        t = pd.DataFrame([trade(symbol="RELIANCE.NS"), trade(symbol="SMALLCAP.NS")])
        self.assertEqual(list(fp.reprice(t, {"RELIANCE": 500}).symbol), ["RELIANCE.NS"])
        self.assertTrue(fp.reprice(t, {}).empty)


class TestSummaries(unittest.TestCase):
    def frame(self):
        t = pd.DataFrame([trade(date="2026-03-02", entry=1000, exit=1004),
                          trade(date="2026-03-02", side="SHORT", entry=500, exit=498),
                          trade(date="2026-03-03", entry=1000, exit=995)])
        return fp.reprice(t, {"RELIANCE": 500})

    def test_breakeven_slippage_zeroes_the_net(self):
        fr = self.frame()
        col = "fut STT 0.02%"
        be = fp.breakeven_slip(fr, col)
        net = fr["gross"].sum() - fr[col].sum() - fr["turnover"].sum() * be
        self.assertAlmostEqual(net, 0.0, places=6)

    def test_scenarios_cover_cash_and_both_stt_rates(self):
        rows = fp.scenario_rows(self.frame(), ["2026-03-02", "2026-03-03"])
        self.assertEqual([r["scenario"] for r in rows],
                         ["cash at lot size", "fut STT 0.02%", "fut STT 0.05%"])
        self.assertGreater(rows[2]["cost_tr"], rows[1]["cost_tr"])

    def test_capital_sums_a_days_positions(self):
        med, peak = fp.capital_needed(self.frame(), margin_pct=0.2)
        self.assertAlmostEqual(peak, (500000 + 250000) * 0.2)
        self.assertAlmostEqual(med, ((500000 + 250000) * 0.2 + 500000 * 0.2) / 2)

    def test_worst_run(self):
        worst, dd = fp.worst_run(pd.Series([100.0, -300.0, 50.0, -100.0, 400.0]))
        self.assertEqual(worst, -300.0)
        self.assertEqual(dd, -350.0)
        self.assertEqual(fp.worst_run(pd.Series([10.0, 20.0])), (0.0, 0.0))


class FakeNFO:
    def __init__(self, rows):
        self.rows = rows

    def instruments(self, exchange):
        assert exchange == "NFO"
        return self.rows


class TestLots(unittest.TestCase):
    def test_nearest_future_only(self):
        d = dt.date
        rows = [dict(name="RELIANCE", instrument_type="FUT", lot_size=500, expiry=d(2026, 11, 26)),
                dict(name="RELIANCE", instrument_type="FUT", lot_size=250, expiry=d(2026, 10, 29)),
                dict(name="RELIANCE", instrument_type="CE", lot_size=999, expiry=d(2026, 10, 1)),
                dict(name="TCS", instrument_type="FUT", lot_size=175, expiry=d(2026, 10, 29)),
                dict(name="", instrument_type="FUT", lot_size=1, expiry=d(2026, 10, 29))]
        md = KiteMarketData(client=FakeNFO(rows), request_pause=0)
        self.assertEqual(md.futures_lots(), {"RELIANCE": 250, "TCS": 175})


class FakeMD:
    def futures_lots(self):
        return {"RELIANCE": 500, "TCS": 175}


class TestEndToEnd(unittest.TestCase):
    def test_report_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            for i in range(12):
                d = f"2026-03-{i + 2:02d}"
                rows.append(trade(date=d, symbol="RELIANCE.NS", entry=1000, exit=1000 + (i % 5) - 2))
                rows.append(trade(date=d, symbol="TCS.NS", side="SHORT", entry=3000, exit=3000 - (i % 3)))
                rows.append(trade(date=d, strategy="ema", symbol="NOFUT.NS", entry=50, exit=51))
            pd.DataFrame(rows).to_csv(Path(tmp) / "strategy_trades.csv", index=False)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                fp.run(None, "orb45", market_data=FakeMD(), root=Path(tmp))
            text = out.getvalue()
        for part in ("FUTURES RE-PRICING", "orb45: 24 of 24 trades", "ema: 0 of 12 trades",
                     "cash at lot size", "fut STT 0.02%", "fut STT 0.05%",
                     "margin at an assumed 20%", "No orders placed"):
            self.assertIn(part, text)
        self.assertNotIn("nan", text.lower())

    def test_log_without_prices_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            pd.DataFrame([dict(date="2026-03-02", pnl=1.0)]).to_csv(
                Path(tmp) / "strategy_trades.csv", index=False)
            with self.assertRaises(SystemExit):
                fp.run(None, "orb45", market_data=FakeMD(), root=Path(tmp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
