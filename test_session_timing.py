"""Session timing: when a position is squared off, which sessions a backtest may
include, and how long histories are fetched.

The square-off tests exist because the bug they cover survived every other
test: all three engines filled the forced exit at the CLOSE of the candle
stamped 15:15 (15:30 on 15m bars), while SQUAREOFF_TIME means "out at 15:15".
On days where Kite's data stopped at 15:15 the same position exited at the
15:15 price instead, so half the sessions in a real run were held 15 minutes
longer than the other half.
"""

import datetime
import os
import unittest

import pandas as pd

import kite_data as kd
import orb_backtest as ob
import paper_broker as pb
import strategies as st

DAY = datetime.date(2026, 8, 14)


def day_bars(n=25, last_bar_jump=5.0):
    """A continuous 15m session from 09:15: every open equals the previous
    close, so the price "at 15:15" is unambiguous. Bar 3 breaks above the
    opening range. The 15:15 candle (index 24) closes far from where it opens,
    so filling at its close instead of its open shows up as a different price.
    """
    t0 = datetime.datetime.combine(DAY, datetime.time(9, 15))
    closes = [100.0, 100.2, 100.1, 101.5] + [101.5 + 0.05 * (i % 3) for i in range(4, 25)]
    closes[24] = closes[23] + last_bar_jump
    rows, prev = [], 100.0
    for i in range(n):
        o, c = prev, closes[i]
        t = t0 + datetime.timedelta(minutes=15 * i)
        rows.append(dict(dt=t, date=DAY, time=t.strftime("%H:%M"), Open=o,
                         High=max(o, c) + 0.05, Low=min(o, c) - 0.05, Close=c,
                         Volume=1000.0))
        prev = c
    return pd.DataFrame(rows)


FULL = day_bars(25)        # ends with the 15:15 candle
CUT = day_bars(24)         # Kite's data stops at 15:15: last candle is 15:00
WIDE_ATR = 10.0            # 5% stop, 10% target: neither can trigger


class TestSquareOffORB(unittest.TestCase):
    def test_fills_at_the_1515_open_not_its_close(self):
        t = ob.trade_day(DAY, FULL, 3, "T", 100000, 2000, WIDE_ATR)
        self.assertEqual(t["reason"], "squareoff")
        self.assertEqual(t["exit_time"], "15:15")
        bar = FULL.iloc[24]
        self.assertAlmostEqual(t["exit"], round(bar.Open, 2))
        self.assertNotAlmostEqual(t["exit"], round(bar.Close, 2),
                                  msg="the 15:15 close is the 15:30 price")

    def test_full_and_truncated_days_exit_at_the_same_price(self):
        """The inconsistency the real run exposed: same position, same moment,
        different price depending only on whether Kite sent the last candle."""
        full = ob.trade_day(DAY, FULL, 3, "T", 100000, 2000, WIDE_ATR)
        cut = ob.trade_day(DAY, CUT, 3, "T", 100000, 2000, WIDE_ATR)
        self.assertEqual(cut["reason"], "eod")
        self.assertAlmostEqual(full["exit"], cut["exit"])
        self.assertAlmostEqual(full["pnl"], cut["pnl"])

    def test_engine_version_is_inside_the_freeze(self):
        before, values = pb.config_fingerprint()
        self.assertEqual(values["ENGINE_VERSION"], ob.ENGINE_VERSION)
        saved = ob.ENGINE_VERSION
        try:
            ob.ENGINE_VERSION = saved + 1
            self.assertNotEqual(before, pb.config_fingerprint()[0])
        finally:
            ob.ENGINE_VERSION = saved


class TestSquareOffPaperBroker(unittest.TestCase):
    def _row(self, g):
        or_high = g.iloc[:3].High.max()
        or_low = g.iloc[:3].Low.min()
        sl, tgt = ob.levels_for(WIDE_ATR)
        return dict(symbol="T", or_high=or_high, or_low=or_low, sl_pct=sl,
                    tgt_pct=tgt, qty_long=50, qty_short=50)

    def test_matches_the_backtest_engine(self):
        """Paper and backtest must agree on the exit, or the paired comparison
        between forward and backtest results measures this bug, not the data."""
        for g in (FULL, CUT):
            paper = pb._replay(DAY, g, self._row(g), 3, 2000)
            back = ob.trade_day(DAY, g, 3, "T", 100000, 2000, WIDE_ATR)
            self.assertAlmostEqual(paper["exit"], back["exit"])
            self.assertEqual(paper["reason"], back["reason"])

    def test_fills_at_the_1515_open(self):
        t = pb._replay(DAY, FULL, self._row(FULL), 3, 2000)
        self.assertEqual(t["reason"], "squareoff")
        self.assertAlmostEqual(t["exit"], round(FULL.iloc[24].Open, 2))


class _AlwaysLong(st.Strategy):
    """Enters at the first opportunity and never exits on its own, so the only
    thing under test is the engine's square-off."""
    name = "always"

    def entry(self, prev, row, atr_frac):
        return "LONG"


class TestSquareOffStrategies(unittest.TestCase):
    def test_fills_at_the_1515_open_and_matches_a_truncated_day(self):
        s = _AlwaysLong()
        full = st.trade_day(s, DAY, s.prepare(FULL), "T", 100000, 2000, WIDE_ATR)
        cut = st.trade_day(s, DAY, s.prepare(CUT), "T", 100000, 2000, WIDE_ATR)
        self.assertEqual(full["reason"], "squareoff")
        self.assertAlmostEqual(full["exit"], round(FULL.iloc[24].Open, 2))
        self.assertAlmostEqual(full["exit"], cut["exit"])


class TestClosedSessions(unittest.TestCase):
    def test_today_and_later_are_dropped(self):
        today = datetime.date(2026, 10, 1)
        days = [datetime.date(2026, 9, 29), datetime.date(2026, 9, 30), today,
                datetime.date(2026, 10, 2)]
        self.assertEqual(ob.closed_sessions(days, today), days[:2])

    def test_defaults_to_the_ist_date(self):
        today = pd.Timestamp.now(tz="Asia/Kolkata").date()
        self.assertEqual(ob.closed_sessions([today]), [])


class TestPeriodWindow(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("ORB_PERIOD_DAYS", None)

    def tearDown(self):
        os.environ.pop("ORB_PERIOD_DAYS", None)
        if self._saved is not None:
            os.environ["ORB_PERIOD_DAYS"] = self._saved

    def test_default_is_the_configured_period(self):
        self.assertEqual(ob._period_days(), int(ob.PERIOD.rstrip("d")))

    def test_env_override_is_read_at_call_time(self):
        os.environ["ORB_PERIOD_DAYS"] = "365"
        self.assertEqual(ob._period_days(), 365)

    def test_explicit_argument_still_wins(self):
        os.environ["ORB_PERIOD_DAYS"] = "365"
        self.assertEqual(ob._period_days("30d"), 30)

    def test_daily_history_covers_the_window_and_default_is_unchanged(self):
        """screener_picks must fetch enough daily bars to rank the FIRST session
        of a long window, without changing what the default run fetches."""
        asked = []

        def fake_fetch(pool, days, market_data=None):
            asked.append(days)
            raise SystemExit("stop after recording")

        saved = ob.fetch_daily_via
        ob.fetch_daily_via = fake_fetch
        try:
            for env in (None, "365"):
                if env:
                    os.environ["ORB_PERIOD_DAYS"] = env
                with self.assertRaises(SystemExit):
                    ob.screener_picks()
        finally:
            ob.fetch_daily_via = saved
        import symbol_screener as sc
        self.assertEqual(asked[0], sc.LOOKBACK_DAYS + 120, "default run unchanged")
        self.assertGreater(asked[1] - sc.LOOKBACK_DAYS, 365 * 5 / 7,
                           "a year of sessions needs a year of trading days plus lookback")


class TestRequestWindows(unittest.TestCase):
    END = datetime.datetime(2026, 10, 1, 9, 35)

    def test_short_span_passes_through_untouched(self):
        start = self.END - datetime.timedelta(days=60)
        self.assertEqual(kd.request_windows(start, self.END, 180), [(start, self.END)])

    def test_long_span_is_covered_without_gaps_or_overlap(self):
        start = self.END - datetime.timedelta(days=365)
        w = kd.request_windows(start, self.END, 180)
        self.assertEqual(len(w), 3)
        self.assertEqual(w[0][0], start)
        self.assertEqual(w[-1][1], self.END)
        for (a, b), (c, _) in zip(w, w[1:]):
            self.assertEqual(c - b, datetime.timedelta(minutes=1), "touch, never overlap")
        for a, b in w:
            self.assertLessEqual(b - a, datetime.timedelta(days=180))

    def test_dates_stay_dates(self):
        d = datetime.date(2026, 10, 1)
        w = kd.request_windows(d - datetime.timedelta(days=10), d, 2000)
        self.assertEqual(w, [(d - datetime.timedelta(days=10), d)])
        self.assertIs(type(w[0][0]), datetime.date)

    def test_long_date_span_steps_by_whole_days(self):
        d = datetime.date(2026, 10, 1)
        w = kd.request_windows(d - datetime.timedelta(days=9), d, 4)
        self.assertEqual(len(w), 3)
        for (a, b), (c, _) in zip(w, w[1:]):
            self.assertEqual(c - b, datetime.timedelta(days=1))
            self.assertLessEqual((b - a).days, 3)


class WindowedKite:
    """Returns one 09:15 candle per day inside each requested window, and
    records every request so the test can see how a span was split."""

    def __init__(self, duplicate_boundary=False):
        self.calls = []
        self.duplicate_boundary = duplicate_boundary

    def instruments(self, exchange):
        return [{"exchange": "NSE", "tradingsymbol": "RELIANCE",
                 "instrument_token": 1, "tick_size": 0.05}]

    def historical_data(self, token, start, end, interval, **kwargs):
        self.calls.append((start, end, interval))
        out, d = [], pd.Timestamp(start).normalize()
        while d <= pd.Timestamp(end):
            ts = d + pd.Timedelta(hours=9, minutes=15)
            if pd.Timestamp(start) <= ts <= pd.Timestamp(end):
                out.append({"date": ts.tz_localize("Asia/Kolkata").isoformat(),
                            "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
            d += pd.Timedelta(days=1)
        if self.duplicate_boundary and out:
            out.append(dict(out[0]))
        return out


class TestChunkedCandles(unittest.TestCase):
    def test_year_of_15m_is_fetched_in_capped_windows_and_stitched(self):
        client = WindowedKite()
        md = kd.KiteMarketData(client=client, request_pause=0)
        end = datetime.datetime(2026, 10, 1, 9, 35)
        start = end - datetime.timedelta(days=365)
        frame = md.candles("RELIANCE.NS", "15m", start, end)
        self.assertEqual(len(client.calls), 3)
        for s, e, interval in client.calls:
            self.assertEqual(interval, "15minute")
            self.assertLessEqual(e - s, datetime.timedelta(days=180))
        self.assertEqual(len(frame), 365, "one candle per day, none lost or doubled")
        self.assertTrue(frame.Datetime.is_monotonic_increasing)

    def test_short_request_is_a_single_call(self):
        client = WindowedKite()
        md = kd.KiteMarketData(client=client, request_pause=0)
        md.intraday("RELIANCE.NS", "15m", 60)
        self.assertEqual(len(client.calls), 1)

    def test_duplicate_candles_are_dropped(self):
        client = WindowedKite(duplicate_boundary=True)
        md = kd.KiteMarketData(client=client, request_pause=0)
        end = datetime.datetime(2026, 10, 1, 9, 35)
        frame = md.candles("RELIANCE.NS", "15m", end - datetime.timedelta(days=20), end)
        self.assertFalse(frame.Datetime.duplicated().any())

    def test_5m_uses_the_tighter_cap(self):
        client = WindowedKite()
        md = kd.KiteMarketData(client=client, request_pause=0)
        md.intraday("RELIANCE.NS", "5m", 365)
        self.assertEqual(len(client.calls), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
