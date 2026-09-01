"""
Daily symbol screener for intraday (ORB-style) trading.
Ranks a universe of stocks by liquidity, volatility (ATR%), and momentum
to produce a short daily watchlist.

    pip install yfinance pandas numpy
    python symbol_screener.py

Runs on daily data (fast, reliable on Yahoo). Point UNIVERSE_FILE at a symbol
list to widen the pool; edit the weights/filters in CONFIG. Output: ranked table + watchlist.csv

Also importable. orb_backtest calls watchlist_asof() once per historical
session to rebuild the watchlist as it would have looked that morning.
"""

import os

import yfinance as yf
import pandas as pd
import numpy as np

# ------------------------- CONFIG -------------------------
# The candidate POOL is just what gets fetched. Liquidity decides the tradeable
# universe, per day, from bars available before that morning. Point a file at
# UNIVERSE_FILE (one symbol per line, or a CSV with a SYMBOL column) to widen the
# pool beyond the large caps below; NSE publishes the full equity list as
# EQUITY_L.csv, and any Nifty 500 constituent CSV works too. Symbols without a
# suffix get ".NS" appended.
UNIVERSE_FILE = None       # e.g. "nifty500.csv" or "EQUITY_L.csv"

# Fallback pool when no file is given. NSE F&O / large-cap names.
DEFAULT_UNIVERSE = [
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
CHUNK           = 100      # symbols per yfinance download call
# ranking weights (must sum to ~1)
W_ATR           = 0.45     # reward movement
W_TURNOVER      = 0.25     # reward liquidity
W_MOMENTUM      = 0.30     # reward directional strength
# ----------------------------------------------------------

# momentum reaches back LOOKBACK_DAYS bars and ATR needs its own warmup, so a
# symbol with fewer bars than this cannot be scored (it used to raise inside the
# try/except and get dropped without a word).
MIN_BARS = max(ATR_PERIOD + 1, LOOKBACK_DAYS + 1)


def load_universe(path=None):
    """Candidate pool: one symbol per line, or a CSV with a SYMBOL column."""
    path = path or UNIVERSE_FILE
    if not path:
        return list(DEFAULT_UNIVERSE)
    if not os.path.exists(path):
        raise SystemExit(f"UNIVERSE_FILE {path!r} not found")
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path)
        col = next((c for c in df.columns if c.strip().upper() == "SYMBOL"), df.columns[0])
        syms = df[col].astype(str)
    else:
        with open(path) as f:
            syms = pd.Series([ln.strip() for ln in f if ln.strip()
                              and not ln.startswith("#")])
    syms = (syms.str.strip().str.upper()
                .map(lambda s: s if "." in s else s + ".NS"))
    return sorted(set(syms) - {""})


def fetch_daily(universe=None, days=None):
    """Download daily bars once. Returns {symbol: DataFrame indexed by date}.

    Chunked, because a pool of a few hundred symbols in one call is where
    yfinance starts dropping tickers silently.
    """
    universe = universe or load_universe()
    days = days or LOOKBACK_DAYS + 20
    out, failed = {}, 0
    for i in range(0, len(universe), CHUNK):
        batch = universe[i:i+CHUNK]
        data = yf.download(batch, period=f"{days}d", interval="1d",
                           group_by="ticker", progress=False)
        if data.empty:
            failed += len(batch)
            continue
        for sym in batch:
            try:
                df = data[sym].dropna() if len(batch) > 1 else data.dropna()
            except KeyError:
                continue
            if not df.empty:
                out[sym] = df
    if not out:
        raise SystemExit("No daily data returned. Check symbols / internet access.")
    if len(universe) > CHUNK:
        print(f"pool {len(universe)} symbols -> {len(out)} with usable history")
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
