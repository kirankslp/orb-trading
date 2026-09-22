"""Read-only global market context for the unified research dashboard.

The module deliberately keeps external context separate from the strategy
signals.  Its regime can adjust confidence, but never creates or vetoes a
trade by itself.
"""

from __future__ import annotations

import csv
import io
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests

from kite_data import IST


TRADINGVIEW_URL = "https://scanner.tradingview.com/global/scan"
CBOE_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"

WORLD_MARKETS = (
    {"ticker": "SP:SPX", "name": "S&P 500", "region": "United States"},
    {"ticker": "NASDAQ:NDX", "name": "Nasdaq 100", "region": "United States"},
    {"ticker": "DJ:DJI", "name": "Dow Jones", "region": "United States"},
    {"ticker": "XETR:DAX", "name": "DAX", "region": "Europe"},
    {"ticker": "TVC:UKX", "name": "FTSE 100", "region": "Europe"},
    {"ticker": "TVC:NI225", "name": "Nikkei 225", "region": "Asia"},
    {"ticker": "TVC:HSI", "name": "Hang Seng", "region": "Asia"},
    {"ticker": "TVC:KOSPI", "name": "KOSPI", "region": "Asia"},
)

NEGATIVE_WORDS = {
    "attack", "conflict", "crash", "crisis", "default", "escalation",
    "hike", "inflation", "recession", "sanction", "selloff", "tariff",
    "tension", "war",
}
POSITIVE_WORDS = {
    "ceasefire", "deal", "easing", "growth", "recovery", "rally", "stimulus",
}


def _clamp(value, low, high):
    return max(low, min(high, value))


def _direction(change):
    return "Up" if change > 0.05 else "Down" if change < -0.05 else "Flat"


def _vix_state(level):
    if level >= 30:
        return "Stress"
    if level >= 20:
        return "Elevated"
    if level < 15:
        return "Low"
    return "Normal"


def fetch_world_markets(http=requests):
    """Fetch the latest major-index move from TradingView's scanner."""
    payload = {
        "symbols": {
            "tickers": [market["ticker"] for market in WORLD_MARKETS],
            "query": {"types": []},
        },
        "columns": ["name", "description", "close", "change", "change_abs", "update_mode"],
    }
    response = http.post(TRADINGVIEW_URL, json=payload, timeout=20)
    response.raise_for_status()
    by_ticker = {row["s"]: row["d"] for row in response.json().get("data", [])}
    rows = []
    for market in WORLD_MARKETS:
        values = by_ticker.get(market["ticker"])
        if not values or values[2] is None or values[3] is None:
            continue
        change = float(values[3])
        mode = str(values[5] or "unknown").replace("_", " ")
        rows.append({
            **market,
            "symbol": values[0],
            "close": round(float(values[2]), 2),
            "change_pct": round(change, 2),
            "change_abs": round(float(values[4] or 0), 2),
            "direction": _direction(change),
            "data_mode": mode,
            "source": "TradingView",
        })
    if not rows:
        raise RuntimeError("TradingView returned no recognised global indices.")
    return rows


def fetch_cboe_vix(http=requests):
    """Fetch the two latest official Cboe VIX closes and calculate the move."""
    response = http.get(CBOE_VIX_URL, timeout=20)
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    if len(rows) < 2:
        raise RuntimeError("Cboe VIX history did not contain two observations.")
    previous, latest = rows[-2], rows[-1]
    level, previous_level = float(latest["CLOSE"]), float(previous["CLOSE"])
    change = level - previous_level
    return {
        "name": "CBOE VIX", "level": round(level, 2),
        "change": round(change, 2),
        "change_pct": round(change / previous_level * 100, 2) if previous_level else 0,
        "asof": latest["DATE"], "state": _vix_state(level), "source": "Cboe",
    }


def fetch_india_vix(market_data):
    """Fetch India VIX through the already-authenticated Kite session."""
    frame = market_data.daily("INDIA VIX", 3)
    if len(frame) < 2:
        raise RuntimeError("Kite returned fewer than two India VIX candles.")
    previous, latest = frame.iloc[-2], frame.iloc[-1]
    previous_level, level = float(previous["Close"]), float(latest["Close"])
    change = level - previous_level
    asof_value = frame.index[-1]
    asof = asof_value.date().isoformat() if hasattr(asof_value, "date") else str(asof_value)
    return {
        "name": "India VIX", "level": round(level, 2),
        "change": round(change, 2),
        "change_pct": round(change / previous_level * 100, 2) if previous_level else 0,
        "asof": asof, "state": _vix_state(level), "source": "Kite Connect",
    }


def classify_headline(title):
    """Assign a transparent, keyword-only category and directional impact."""
    lower = title.lower()
    words = {word.strip(".,:;!?()[]{}'\"") for word in lower.split()}
    negative = len(words & NEGATIVE_WORDS)
    positive = len(words & POSITIVE_WORDS)
    impact = "Risk-negative" if negative > positive else "Risk-positive" if positive > negative else "Watch"
    if any(word in lower for word in ("fed", "central bank", "rate ", "inflation")):
        category = "Monetary policy"
    elif any(word in lower for word in ("war", "attack", "sanction", "tariff", "conflict", "ceasefire")):
        category = "Geopolitics"
    elif any(word in lower for word in ("oil", "opec", "gas", "energy")):
        category = "Energy"
    elif any(word in lower for word in ("market", "stocks", "shares", "index", "rally", "selloff")):
        category = "Markets"
    else:
        category = "Macro"
    return category, impact


def fetch_market_news(http=requests, limit=12):
    """Read a personal-use Google News RSS search for market-moving topics."""
    query = (
        'global markets OR inflation OR "central bank" OR oil OR tariffs OR '
        'sanctions OR recession OR war when:1d'
    )
    url = f"{GOOGLE_NEWS_URL}?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    response = http.get(url, timeout=20)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    rows, seen = [], set()
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        source_node = item.find("source")
        source = (source_node.text or "Google News").strip() if source_node is not None else "Google News"
        published = (item.findtext("pubDate") or "").strip()
        try:
            published = parsedate_to_datetime(published).astimezone(ZoneInfo(IST)).isoformat(timespec="minutes")
        except (TypeError, ValueError):
            pass
        category, impact = classify_headline(title)
        rows.append({
            "title": title, "url": link, "source": source, "published": published,
            "category": category, "impact": impact,
        })
        if len(rows) >= limit:
            break
    if not rows:
        raise RuntimeError("The news feed returned no matching headlines.")
    return rows


def derive_market_regime(markets, volatility, news):
    """Build a bounded risk score; context informs confidence, not direction."""
    moves = [float(row["change_pct"]) for row in markets]
    market_component = _clamp((statistics.mean(moves) if moves else 0) * 12, -35, 35)
    vix_component = 0.0
    for item in volatility:
        weight = 1.15 if item["name"] == "CBOE VIX" else 0.75
        vix_component -= _clamp(float(item["change_pct"]) * weight, -15, 15)
        if float(item["level"]) >= 30:
            vix_component -= 8
        elif float(item["level"]) >= 20:
            vix_component -= 3
    headline_balance = sum(
        1 if item["impact"] == "Risk-positive" else -1 if item["impact"] == "Risk-negative" else 0
        for item in news
    )
    news_component = _clamp(headline_balance * 2.5, -15, 15)
    score = round(_clamp(market_component + vix_component + news_component, -100, 100), 1)
    regime = "Risk-on" if score >= 15 else "Risk-off" if score <= -15 else "Mixed"
    if moves:
        advancing = sum(move > 0.05 for move in moves)
        breadth = f"{advancing} of {len(moves)} tracked indices are higher"
    else:
        breadth = "global index breadth is unavailable"
    summary = f"{breadth}; volatility and headline tone produce a {regime.lower()} backdrop."
    return {"regime": regime, "score": score, "summary": summary}


def build_market_context(market_data):
    """Refresh all context sources while retaining partial results on failure."""
    markets, volatility, news, errors = [], [], [], []
    for label, fetcher in (
        ("Global indices", lambda: fetch_world_markets()),
        ("CBOE VIX", lambda: fetch_cboe_vix()),
        ("India VIX", lambda: fetch_india_vix(market_data)),
        ("World news", lambda: fetch_market_news()),
    ):
        try:
            value = fetcher()
            if label == "Global indices":
                markets = value
            elif label == "World news":
                news = value
            else:
                volatility.append(value)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    regime = derive_market_regime(markets, volatility, news)
    return {
        "generated_at": datetime.now(ZoneInfo(IST)).isoformat(timespec="seconds"),
        "markets": markets, "volatility": volatility, "news": news,
        "errors": errors, **regime,
    }
