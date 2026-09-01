"""
Daily symbol screener for intraday (ORB-style) trading.
Ranks a universe of stocks by liquidity, volatility (ATR%), and momentum
to produce a short daily watchlist.

    pip install yfinance pandas numpy
    python symbol_screener.py

Runs on daily data (fast, reliable on Yahoo). Edit UNIVERSE and the
weights/filters in CONFIG. Output: ranked table + watchlist.csv
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
MIN_PRICE       = 50       # skip penny stocks
MIN_AVG_TURNOVER= 50e7     # min avg daily turnover in Rs (50 cr) -> liquidity floor
TOP_N           = 10       # size of final watchlist
# ranking weights (must sum to ~1)
W_ATR           = 0.45     # reward movement
W_TURNOVER      = 0.25     # reward liquidity
W_MOMENTUM      = 0.30     # reward directional strength
# ----------------------------------------------------------


def atr_pct(df, period=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr / c.iloc[-1] * 100)   # ATR as % of price


def screen():
    data = yf.download(UNIVERSE, period=f"{LOOKBACK_DAYS+20}d",
                       interval="1d", group_by="ticker", progress=False)
    rows = []
    for sym in UNIVERSE:
        try:
            df = data[sym].dropna()
            if len(df) < 20 or df["Close"].iloc[-1] < MIN_PRICE:
                continue
            price     = df["Close"].iloc[-1]
            avg_vol   = df["Volume"].tail(LOOKBACK_DAYS).mean()
            turnover  = avg_vol * price
            if turnover < MIN_AVG_TURNOVER:
                continue
            atrp      = atr_pct(df)
            # momentum = % change over lookback, absolute (direction-agnostic strength)
            mom       = abs(df["Close"].iloc[-1] / df["Close"].iloc[-LOOKBACK_DAYS] - 1) * 100
            rows.append(dict(symbol=sym, price=round(price,1),
                             atr_pct=round(atrp,2),
                             turnover_cr=round(turnover/1e7,0),
                             momentum_pct=round(mom,2)))
        except Exception as e:
            continue

    d = pd.DataFrame(rows)
    if d.empty:
        print("Nothing passed the filters."); return d

    # normalize each metric 0-1, then weighted score
    for col in ["atr_pct","turnover_cr","momentum_pct"]:
        lo, hi = d[col].min(), d[col].max()
        d[col+"_n"] = 0.0 if hi==lo else (d[col]-lo)/(hi-lo)
    d["score"] = (W_ATR*d["atr_pct_n"] + W_TURNOVER*d["turnover_cr_n"]
                  + W_MOMENTUM*d["momentum_pct_n"])
    d = d.sort_values("score", ascending=False).reset_index(drop=True)
    return d


if __name__ == "__main__":
    d = screen()
    if not d.empty:
        show = d[["symbol","price","atr_pct","turnover_cr","momentum_pct","score"]].copy()
        show["score"] = show["score"].round(3)
        print("\n=== RANKED UNIVERSE ===")
        print(show.to_string(index=False))
        watch = d.head(TOP_N)
        watch[["symbol","price","atr_pct","turnover_cr","momentum_pct","score"]].to_csv("watchlist.csv", index=False)
        print(f"\nTop {TOP_N} watchlist -> watchlist.csv")
        print(", ".join(watch["symbol"].tolist()))
