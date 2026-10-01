"""Order-book arithmetic and the spread logger. Fake quotes, no Kite."""

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

import pandas as pd

import kite_data as kd
import spread_probe as sp

BOOK = {
    "buy":  [{"price": 99.9, "quantity": 50}, {"price": 99.8, "quantity": 100}],
    "sell": [{"price": 100.1, "quantity": 50}, {"price": 100.3, "quantity": 100}],
}


class TestWalk(unittest.TestCase):
    def test_fills_inside_the_first_level(self):
        self.assertEqual(sp.walk(BOOK["sell"], 20), (100.1, 20))

    def test_crosses_levels_and_averages(self):
        px, filled = sp.walk(BOOK["sell"], 100)          # 50 @100.1 + 50 @100.3
        self.assertEqual(filled, 100)
        self.assertAlmostEqual(px, 100.2)

    def test_reports_a_partial_fill_when_the_book_runs_out(self):
        px, filled = sp.walk(BOOK["sell"], 1000)
        self.assertEqual(filled, 150)
        self.assertAlmostEqual(px, (50 * 100.1 + 100 * 100.3) / 150)

    def test_skips_empty_levels(self):
        self.assertEqual(sp.walk([{"price": 0, "quantity": 0},
                                  {"price": 101, "quantity": 10}], 5), (101.0, 5))


class TestBookMetrics(unittest.TestCase):
    def test_small_order_pays_half_the_spread(self):
        m = sp.book_metrics(BOOK, 2000)                  # 19 shares, inside level 1
        self.assertAlmostEqual(m["mid"], 100.0)
        self.assertAlmostEqual(m["spread_pct"], 0.2)
        self.assertAlmostEqual(m["half_spread_pct"], 0.1)
        self.assertAlmostEqual(m["buy_impact_pct"], 0.1)
        self.assertAlmostEqual(m["sell_impact_pct"], 0.1)
        self.assertTrue(m["depth_ok"])

    def test_large_order_pays_more_than_half_the_spread(self):
        """The whole point of walking the book: a slot bigger than the top
        level costs more than the quoted spread suggests."""
        m = sp.book_metrics(BOOK, 10000)                 # 100 shares
        self.assertAlmostEqual(m["buy_impact_pct"], 0.2)
        self.assertGreater(m["buy_impact_pct"], m["half_spread_pct"])

    def test_one_sided_or_crossed_book_is_skipped(self):
        self.assertIsNone(sp.book_metrics({"buy": BOOK["buy"], "sell": []}, 1000))
        self.assertIsNone(sp.book_metrics({"buy": [{"price": 101, "quantity": 1}],
                                           "sell": [{"price": 100, "quantity": 1}]}, 1000))

    def test_thin_book_is_flagged(self):
        self.assertFalse(sp.book_metrics(BOOK, 1_000_000)["depth_ok"])


class TestBuckets(unittest.TestCase):
    def test_breakout_hour_is_its_own_bucket(self):
        self.assertEqual(sp.bucket("09:20"), "open")
        self.assertEqual(sp.bucket("10:05"), "breakout hour")
        self.assertEqual(sp.bucket("12:00"), "midday")
        self.assertEqual(sp.bucket("15:10"), "close")
        self.assertEqual(sp.bucket("16:00"), "outside")

    def test_market_hours(self):
        ist = "Asia/Kolkata"
        self.assertTrue(sp.market_open(pd.Timestamp("2026-10-01 10:00", tz=ist)))
        self.assertFalse(sp.market_open(pd.Timestamp("2026-10-01 08:00", tz=ist)))
        self.assertFalse(sp.market_open(pd.Timestamp("2026-10-03 10:00", tz=ist)),
                         "Saturday")


class FakeQuoteClient:
    def __init__(self):
        self.calls = []

    def quote(self, keys):
        self.calls.append(list(keys))
        return {k: {"last_price": 100.0, "depth": BOOK} for k in keys
                if k != "NSE:EMPTY"}


class TestOrderBooks(unittest.TestCase):
    def test_batched_and_keyed_by_plain_symbol(self):
        client = FakeQuoteClient()
        md = kd.KiteMarketData(client=client, request_pause=0)
        books = md.order_books(["AAA.NS", "BBB.NS", "EMPTY.NS", "AAA.NS"], batch=2)
        self.assertEqual(client.calls, [["NSE:AAA", "NSE:BBB"], ["NSE:EMPTY"]],
                         "deduped, then batched")
        self.assertEqual(set(books), {"AAA", "BBB"})
        self.assertEqual(books["AAA"]["sell"][0]["price"], 100.1)


class TestSnapshotAndReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (sp.SPREAD_DIR, sp.SPREADS_CSV)
        sp.SPREAD_DIR = self.tmp
        sp.SPREADS_CSV = os.path.join(self.tmp, "spreads.csv")

    def tearDown(self):
        sp.SPREAD_DIR, sp.SPREADS_CSV = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_snapshot_appends_and_report_compares_to_the_model(self):
        md = kd.KiteMarketData(client=FakeQuoteClient(), request_pause=0)
        ts = pd.Timestamp("2026-10-01 10:05", tz="Asia/Kolkata")
        turnover = {"AAA.NS": 2000, "BBB.NS": 500}
        for _ in range(2):
            rows = sp.snapshot(md, ["AAA.NS", "BBB.NS", "EMPTY.NS"], turnover,
                               notional=2000, ts=ts)
        self.assertEqual(len(rows), 2, "a symbol with no book is skipped, not faked")
        df = sp.read_spreads()
        self.assertEqual(len(df), 4, "append-only across snapshots")
        self.assertEqual(set(df.bucket), {"breakout hour"})
        self.assertEqual(sorted(df.modelled_slip_pct.unique()), [0.03, 0.05])

        out = io.StringIO()
        with redirect_stdout(out):
            sp.report(df, gap=5.1)
        text = out.getvalue()
        self.assertIn("measured 0.1000%", text)
        self.assertIn("DEARER", text, "0.10% measured vs 0.03-0.05% modelled")
        self.assertIn("still Rs", text)
        self.assertIn("lower bound", text)

    def test_watchlist_is_cached_for_the_day(self):
        calls = []

        def fake_watchlist(market_data=None):
            calls.append(1)
            return ["AAA.NS"], {"AAA.NS": {"turnover_cr": 900}}

        saved = sp.dp.todays_watchlist
        sp.dp.todays_watchlist = fake_watchlist
        try:
            day = pd.Timestamp("2026-10-01").date()
            a = sp.todays_symbols(day=day)
            b = sp.todays_symbols(day=day)
        finally:
            sp.dp.todays_watchlist = saved
        self.assertEqual(a, b)
        self.assertEqual(a, (["AAA.NS"], {"AAA.NS": 900}))
        self.assertEqual(len(calls), 1, "the 15-minute ranking runs once a day")

    def test_empty_report_says_so(self):
        out = io.StringIO()
        with redirect_stdout(out):
            sp.report(pd.DataFrame())
        self.assertIn("No snapshots yet", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
