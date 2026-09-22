"""Offline checks for the Kite market-data adapter (no credentials required)."""

import unittest

import pandas as pd

from kite_data import KiteMarketData, interval_for_kite, kite_symbol
import symbol_screener as sc


class FakeKite:
    def __init__(self, candles=None):
        self.calls = []
        self.candles = candles if candles is not None else [
            {"date": "2026-09-01T09:15:00+0530", "open": 100, "high": 102,
             "low": 99, "close": 101, "volume": 1000},
            {"date": "2026-09-02T09:15:00+0530", "open": 101, "high": 103,
             "low": 100, "close": 102, "volume": 1100},
        ]

    def instruments(self, exchange):
        self.calls.append(("instruments", exchange))
        return [{"exchange": "NSE", "tradingsymbol": "RELIANCE", "instrument_token": 738561,
                 "tick_size": 0.05}]

    def ltp(self, keys):
        self.calls.append(("ltp", keys))
        return {"NSE:RELIANCE": {"instrument_token": 738561, "last_price": 1412.35}}

    def historical_data(self, token, start, end, interval, **kwargs):
        self.calls.append(("historical", token, interval, kwargs))
        return self.candles


class KiteDataTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeKite()
        self.data = KiteMarketData(client=self.client, request_pause=0)

    def test_existing_ns_symbols_resolve_to_nse_tokens(self):
        self.assertEqual(kite_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(kite_symbol("^NSEI"), "NIFTY 50")
        self.assertEqual(self.data.instrument_token("RELIANCE.NS"), 738561)
        self.assertEqual(self.client.calls, [("instruments", "NSE")])

    def test_candles_are_ist_with_project_column_names(self):
        frame = self.data.intraday("RELIANCE.NS", "15m", 5)
        self.assertEqual(list(frame.columns), ["Datetime", "Open", "High", "Low", "Close", "Volume"])
        self.assertEqual(str(frame.Datetime.dt.tz), "Asia/Kolkata")
        self.assertEqual(self.client.calls[-1][2], "15minute")

    def test_daily_output_has_datetime_index_for_screener(self):
        daily = self.data.daily("RELIANCE.NS", 1)
        self.assertIsInstance(daily.index, pd.DatetimeIndex)
        self.assertEqual(str(daily.index.tz), "Asia/Kolkata")
        self.assertEqual(self.client.calls[-1][2], "day")

    def test_screener_uses_adapter_and_rejects_empty_symbol(self):
        daily = sc.fetch_daily(["RELIANCE.NS"], days=2, market_data=self.data)
        self.assertIn("RELIANCE.NS", daily)
        empty = KiteMarketData(client=FakeKite(candles=[]), request_pause=0)
        with self.assertRaisesRegex(SystemExit, "partial Kite universe"):
            sc.fetch_daily(["RELIANCE.NS"], days=2, market_data=empty)

    def test_kite_interval_mapping(self):
        self.assertEqual(interval_for_kite("5m"), "5minute")
        self.assertEqual(interval_for_kite("1d"), "day")

    def test_batched_ltp_and_instrument_tick_size(self):
        prices = self.data.latest_prices(["RELIANCE.NS", "RELIANCE"])
        self.assertEqual(prices, {"RELIANCE": 1412.35})
        self.assertEqual(self.data.instrument_info("RELIANCE")["tick_size"], 0.05)


if __name__ == "__main__":
    unittest.main()
