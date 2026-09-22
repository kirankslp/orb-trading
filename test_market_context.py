import unittest
from datetime import datetime

import pandas as pd

from market_context import (
    classify_headline, derive_market_regime, fetch_cboe_vix,
    fetch_india_vix, fetch_market_news, fetch_world_markets,
)


class FakeResponse:
    def __init__(self, *, data=None, text="", content=b""):
        self._data = data
        self.text = text
        self.content = content or text.encode()

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


class FakeHttp:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response

    def post(self, *args, **kwargs):
        return self.response


class FakeMarketData:
    def daily(self, symbol, days):
        self.request = (symbol, days)
        return pd.DataFrame(
            {"Close": [13.0, 15.6]},
            index=[datetime(2026, 9, 3), datetime(2026, 9, 4)],
        )


class MarketContextTests(unittest.TestCase):
    def test_world_market_response_is_mapped_and_labelled(self):
        payload = {"data": [{"s": "SP:SPX", "d": ["SPX", "S&P 500", 6500, 1.25, 80, "delayed_streaming_600"]}]}
        rows = fetch_world_markets(FakeHttp(FakeResponse(data=payload)))
        self.assertEqual(rows[0]["name"], "S&P 500")
        self.assertEqual(rows[0]["direction"], "Up")
        self.assertEqual(rows[0]["change_pct"], 1.25)

    def test_cboe_and_india_vix_changes(self):
        csv_text = "DATE,OPEN,HIGH,LOW,CLOSE\n09/03/2026,14,15,13,14.00\n09/04/2026,15,17,14,16.80\n"
        cboe = fetch_cboe_vix(FakeHttp(FakeResponse(text=csv_text)))
        india = fetch_india_vix(FakeMarketData())
        self.assertEqual(cboe["state"], "Normal")
        self.assertEqual(cboe["change_pct"], 20.0)
        self.assertEqual(india["change_pct"], 20.0)

    def test_news_rss_is_classified(self):
        xml = b"""<rss><channel><item><title>Oil prices rally after ceasefire deal</title>
        <link>https://example.test/story</link><pubDate>Fri, 04 Sep 2026 12:00:00 GMT</pubDate>
        <source>Example</source></item></channel></rss>"""
        rows = fetch_market_news(FakeHttp(FakeResponse(content=xml)))
        self.assertEqual(rows[0]["category"], "Geopolitics")
        self.assertEqual(rows[0]["impact"], "Risk-positive")

    def test_risk_off_regime_from_falling_markets_and_rising_vix(self):
        markets = [{"change_pct": -1.5}, {"change_pct": -0.8}, {"change_pct": -1.1}]
        volatility = [{"name": "CBOE VIX", "level": 28, "change_pct": 12}]
        news = [{"impact": "Risk-negative"}, {"impact": "Risk-negative"}]
        result = derive_market_regime(markets, volatility, news)
        self.assertEqual(result["regime"], "Risk-off")
        self.assertLess(result["score"], -15)

    def test_headline_watch_when_no_directional_keyword(self):
        self.assertEqual(classify_headline("Investors await quarterly results")[1], "Watch")


if __name__ == "__main__":
    unittest.main()
