"""
Relative volume in the opening range: is something happening in this stock today?

A breakout on an ordinary day is mostly noise. On a 179-session run the ORB's
median favourable move after entry was about 0.9% and a 1x ATR target was
reached on under 3% of trades: breakouts mostly do not follow through. A stock
trading several times its usual volume in the opening minutes is usually
reacting to something (results, an order win, a block deal), and that is the
kind of breakout with a reason to continue. Relative volume is a proxy for that
catalyst which needs no news feed and carries no timestamp risk.

    rvol(d, s) = volume of s in the opening range on d
                 / median of that same window over s's previous sessions

Point in time by construction: the numerator is complete the moment the range
closes, which is exactly when an ORB entry becomes possible, and the baseline
uses only sessions strictly before d.

The test was stated before any run: ORB trades in stocks at RVOL_MIN (2.0x) or
more earn more gross per trade than the rest. The threshold is fixed here, not
tuned; the other buckets in the report are descriptive.
"""

import math

import numpy as np
import pandas as pd

RVOL_MIN = 2.0          # pre-stated threshold for "in play"; do not tune on results
RVOL_LOOKBACK = 20      # sessions in the baseline
RVOL_MIN_HISTORY = 10   # fewer prior sessions than this and the baseline is too thin

BUCKETS = ((None, None, "unknown"), (0.0, 1.0, "< 1x"), (1.0, 2.0, "1-2x"),
           (2.0, 3.0, "2-3x"), (3.0, math.inf, ">= 3x"))


def opening_volume(df, n_or):
    """Volume in each session's first n_or bars. Sessions missing any of those
    bars are left out rather than compared on a shorter window."""
    df = df.sort_values("dt")
    first = df.groupby("date").head(n_or)
    counts = first.groupby("date").size()
    vol = first.groupby("date")["Volume"].sum()
    return vol[counts == n_or].astype(float)


def opening_rvol(intraday, n_or, lookback=RVOL_LOOKBACK, min_history=RVOL_MIN_HISTORY):
    """{(date, symbol): rvol} for every session with a usable baseline.

    The baseline is the MEDIAN of the previous `lookback` sessions' opening
    volume, so one earlier spike cannot inflate it, and shift(1) keeps today's
    own volume out of it.
    """
    out = {}
    for sym, df in intraday.items():
        if df is None or df.empty or "Volume" not in df.columns:
            continue
        vol = opening_volume(df, n_or)
        base = vol.shift(1).rolling(lookback, min_periods=min_history).median()
        ratio = vol / base
        for day, r in ratio.items():
            if np.isfinite(r) and base[day] > 0:
                out[(day, sym)] = float(r)
    return out


def annotate(trades, rvol_map):
    """Trades with an `rvol` column; NaN where no baseline existed."""
    if trades.empty:
        return trades.assign(rvol=pd.Series(dtype=float))
    keys = zip(trades["date"], trades["symbol"])
    return trades.assign(rvol=[rvol_map.get(k, np.nan) for k in keys])


def in_play(trades, threshold=RVOL_MIN):
    """Only the trades whose stock opened at threshold x usual volume or more.
    Unknown RVOL is excluded: an untested trade is not evidence either way."""
    if trades.empty or "rvol" not in trades.columns:
        return trades.iloc[0:0]
    return trades[trades["rvol"] >= threshold].copy()


def bucket_of(r):
    if r is None or not np.isfinite(r):
        return "unknown"
    for lo, hi, name in BUCKETS[1:]:
        if lo <= r < hi:
            return name
    return "unknown"


def bucket_table(trades, moves=None):
    """Per RVOL bucket: trades, win rate, gross and net per trade, exit mix and
    the median favourable move after entry (when excursions are supplied,
    keyed by date and symbol)."""
    if trades.empty:
        return pd.DataFrame()
    t = trades.copy()
    t["bucket"] = [bucket_of(r) for r in t["rvol"]]
    if moves is not None and not moves.empty:
        t = t.merge(moves[["date", "symbol", "mfe_pct"]], on=["date", "symbol"], how="left")
    rows = []
    for _, _, name in BUCKETS:
        b = t[t["bucket"] == name]
        if b.empty:
            continue
        rows.append(dict(
            bucket=name, trades=len(b), win=(b.pnl > 0).mean() * 100,
            gross_tr=b.gross.mean(), net_tr=b.pnl.mean(),
            target=(b.reason == "target").mean() * 100,
            stop=(b.reason == "stoploss").mean() * 100,
            move=b["mfe_pct"].median() if "mfe_pct" in b else np.nan))
    return pd.DataFrame(rows)
