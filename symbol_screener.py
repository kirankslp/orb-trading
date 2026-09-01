"""
Daily symbol screener for intraday (ORB-style) trading.
Ranks a universe of stocks by liquidity, volatility (ATR%), and momentum
to produce a short daily watchlist.

    pip install yfinance pandas numpy
    python symbol_screener.py

Runs on daily data (fast, reliable on Yahoo). Edit UNIVERSE and the
weights/filters in CONFIG. Output: ranked table + watchlist.csv

Also importable. orb_backtest calls watchlist_asof() once per historical
session to rebuild the watchlist as it would have looked that morning.
"""

import yfinance as yf
import pandas as pd
import numpy as np

# ------------------------- CONFIG -------------------------
# Start with a liquid universe. These are NSE F&O / large-cap names.
# Add ".NS" suffix for NSE. Replace with your own list any time.
UNIVERSE = [
    "RELIANCE.NS","TCS.NS","HDFCBANK.NS","ICICIBANK.NS","INFY.NS","SBIN.NS",
    "BHARTIARTL.NS","ITC.NS","LT.NS","AXISBANK.NS","KOTAKBANK.NS","HINDUNILVR.NS",
    "BAJFINANCE.NS","MARUTI.NS","TATAMOTORS.NS","SUNPHARMA.NS","TITAN.NS","WIPRO.NS",
    "ADANIENT.NS","TATASTEEL.NS","JSWSTEEL.NS","HCLTECH.NS","ONGC.NS","NTPC.NS",
    "POWERGRID.NS","COALINDIA.NS","M&M.NS","TECHM.NS","ULTRACEMCO.NS","HINDALCO.NS",
]

LOOKBACK_DAYS   = 30       # window for ATR / avg volume / momentum
ATR_PERIOD      = 14
MIN_PRICE       = 50       # skip penny stocks
MIN_AVG_TURNOVER= 50e7     # min avg daily turnover in Rs (50 cr) -> liquidity floor
TOP_N           = 10       # size of final watchlist
# ranking weights (must sum to ~1)
W_ATR           = 0.45     # reward movement
W_TURNOVER      = 0.25     # reward liquidity
W_MOMENTUM      = 0.30     # reward directional strength
# ----------------------------------------------------------

# momentum reaches back LOOKBACK_DAYS bars and ATR needs its own warmup, so a
# symbol with fewer bars than this cannot be scored (it used to raise inside the
# try/except and get dropped without a word).
MIN_BARS = max(ATR_PERIOD + 1, LOOKBACK_DAYS + 1)


def fetch_daily(universe=None, days=None):
    """Download daily bars once. Returns {symbol: DataFrame indexed by date}."""
    universe = universe or UNIVERSE
    days = days or LOOKBACK_DAYS + 20
    data = yf.download(universe, period=f"{days}d", interval="1d",
                       group_by="ticker", progress=False)
    if data.empty:
        raise SystemExit("No daily data returned. Check symbols / internet access.")
    out = {}
    for sym in universe:
        try:
            df = data[sym].dropna()
        except KeyError:
            continue
        if not df.empty:
            out[sym] = df
    return out


def atr_pct(df, period=ATR_PERIOD):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr / c.iloc[-1] * 100)   # ATR as % of price


def metrics(df, max_price=None):
    """Screening metrics from the tail of df, or None if it fails a filter."""
    if len(df) < MIN_BARS:
        return None
    price = float(df["Close"].iloc[-1])
    if price < MIN_PRICE:
        return None
    if max_price is not None and price > max_price:
        return None      # a slot that cannot buy one share is a wasted slot
    turnover = float(df["Volume"].tail(LOOKBACK_DAYS).mean()) * price
    if turnover < MIN_AVG_TURNOVER:
        return None
    atrp = atr_pct(df)
    # momentum = % change over lookback, absolute (direction-agnostic strength)
    mom = abs(price / float(df["Close"].iloc[-LOOKBACK_DAYS]) - 1) * 100
    if not (np.isfinite(atrp) and np.isfinite(mom)):
        return None
    return dict(price=round(price,1), atr_pct=round(atrp,2),
                turnover_cr=round(turnover/1e7,0), momentum_pct=round(mom,2))


def rank(rows):
    """Min-max normalize each metric across survivors, then weighted score."""
    d = pd.DataFrame(rows)
    if d.empty:
        return d
    for col in ["atr_pct","turnover_cr","momentum_pct"]:
        lo, hi = d[col].min(), d[col].max()
        d[col+"_n"] = 0.0 if hi==lo else (d[col]-lo)/(hi-lo)
    d["score"] = (W_ATR*d["atr_pct_n"] + W_TURNOVER*d["turnover_cr_n"]
                  + W_MOMENTUM*d["momentum_pct_n"])
    return d.sort_values("score", ascending=False).reset_index(drop=True)


def screen_asof(daily, asof=None, max_price=None):
    """Rank the universe using only bars STRICTLY BEFORE `asof`.

    asof=None scores on everything available, which is what you want for
    tomorrow's watchlist. Passing a date is what keeps a backtest honest: on the
    morning of D the newest close you can possibly have seen is D-1's.

    max_price drops names a single position could not buy a share of.
    """
    rows = []
    for sym, df in daily.items():
        if asof is not None:
            df = df[df.index.date < asof]
        m = metrics(df, max_price)
        if m:
            rows.append(dict(symbol=sym, **m))
    return rank(rows)


def watchlist_asof(daily, asof, top_n=None, max_price=None):
    """Symbols to trade on the session `asof`, ranked best first."""
    d = screen_asof(daily, asof, max_price)
    return [] if d.empty else d["symbol"].head(top_n or TOP_N).tolist()


def screen():
    return screen_asof(fetch_daily())


COLS = ["symbol","price","atr_pct","turnover_cr","momentum_pct","score"]

if __name__ == "__main__":
    d = screen()
    if d.empty:
        raise SystemExit("Nothing passed the filters.")
    show = d[COLS].copy()
    show["score"] = show["score"].round(3)
    print("\n=== RANKED UNIVERSE ===")
    print(show.to_string(index=False))
    watch = d.head(TOP_N)
    watch[COLS].to_csv("watchlist.csv", index=False)
    print(f"\nTop {TOP_N} watchlist -> watchlist.csv")
    print(", ".join(watch["symbol"].tolist()))
