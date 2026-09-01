"""
Opening Range Breakout (ORB) intraday backtester.
One trade per day. Single-file, dependency-light.

    pip install yfinance pandas numpy
    python orb_backtest.py

Edit CONFIG to change symbol, range window, stops, targets.
"""

import yfinance as yf
import pandas as pd
import numpy as np

# ------------------------- CONFIG -------------------------
SYMBOL         = "^NSEI"      # Nifty. "^NSEBANK" Bank Nifty, "RELIANCE.NS" a stock
INTERVAL       = "15m"        # 5m or 15m
PERIOD         = "60d"        # yfinance intraday history cap ~60d
OR_MINUTES     = 45           # opening range = first N minutes
SL_PCT         = 0.004        # stop loss 0.4% from entry
TARGET_PCT     = 0.008        # target 0.8% from entry (2:1)
SQUAREOFF_TIME = "15:15"      # force exit time
CAPITAL        = 100000       # notional per trade
BROKERAGE_PCT  = 0.0003       # round-trip cost (brokerage+slippage+tax) 0.03%
# ----------------------------------------------------------


def load_data():
    df = yf.download(SYMBOL, period=PERIOD, interval=INTERVAL, progress=False)
    if df.empty:
        raise SystemExit("No data returned. Check symbol / internet access.")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    tcol = "Datetime" if "Datetime" in df.columns else "Date"
    df[tcol] = pd.to_datetime(df[tcol])
    try:
        df[tcol] = df[tcol].dt.tz_convert("Asia/Kolkata")
    except Exception:
        pass
    df["date"] = df[tcol].dt.date
    df["time"] = df[tcol].dt.strftime("%H:%M")
    df["dt"]   = df[tcol]
    return df


def _pnl(day, side, entry, exit_px, t_in, t_out, reason):
    gross_pct = ((exit_px - entry) if side == "LONG" else (entry - exit_px)) / entry
    net_pct = gross_pct - BROKERAGE_PCT
    return dict(date=day, side=side, entry=round(entry,2), exit=round(exit_px,2),
                entry_time=t_in, exit_time=t_out, reason=reason,
                gross_pct=round(gross_pct*100,3), net_pct=round(net_pct*100,3),
                pnl=round(net_pct*CAPITAL,1))


def backtest(df):
    trades = []
    or_candles = OR_MINUTES // int(INTERVAL.replace("m", ""))

    for day, g in df.groupby("date"):
        g = g.sort_values("dt").reset_index(drop=True)
        if len(g) < or_candles + 2:
            continue

        or_block = g.iloc[:or_candles]
        or_high, or_low = or_block["High"].max(), or_block["Low"].min()

        side = entry = sl = tgt = entry_time = None

        for i in range(or_candles, len(g)):
            h, l, c, t = g.iloc[i][["High","Low","Close","time"]]

            # --- not yet in a trade: hunt for the breakout ---
            if side is None:
                if h > or_high:
                    side, entry, entry_time = "LONG", or_high, t
                    sl, tgt = entry*(1-SL_PCT), entry*(1+TARGET_PCT)
                elif l < or_low:
                    side, entry, entry_time = "SHORT", or_low, t
                    sl, tgt = entry*(1+SL_PCT), entry*(1-TARGET_PCT)
                continue  # can't enter and exit same candle in this simple model

            # --- in a trade: check square-off, then SL, then target ---
            if t >= SQUAREOFF_TIME:
                trades.append(_pnl(day, side, entry, c, entry_time, t, "squareoff")); break
            if side == "LONG":
                if l <= sl:  trades.append(_pnl(day, side, entry, sl,  entry_time, t, "stoploss")); break
                if h >= tgt: trades.append(_pnl(day, side, entry, tgt, entry_time, t, "target"));   break
            else:
                if h >= sl:  trades.append(_pnl(day, side, entry, sl,  entry_time, t, "stoploss")); break
                if l <= tgt: trades.append(_pnl(day, side, entry, tgt, entry_time, t, "target"));   break
        else:
            # loop ended with a still-open position
            if side is not None:
                trades.append(_pnl(day, side, entry, g.iloc[-1]["Close"], entry_time, g.iloc[-1]["time"], "eod"))

    return pd.DataFrame(trades)


def report(tr):
    if tr.empty:
        print("No trades generated."); return
    wins, losers = tr[tr.net_pct>0], tr[tr.net_pct<=0]
    tr = tr.sort_values(["date","entry_time"]).copy()
    tr["cum_pnl"] = tr.pnl.cumsum()
    dd = (tr.cum_pnl - tr.cum_pnl.cummax()).min()
    print("="*60)
    print(f"Symbol {SYMBOL} | {INTERVAL} | days tested {tr.date.nunique()}")
    print("="*60)
    print(f"Total trades   : {len(tr)}")
    print(f"Win rate       : {len(wins)/len(tr)*100:.1f}%")
    print(f"Total net P&L  : Rs {tr.pnl.sum():,.0f}")
    print(f"Avg P&L/trade  : Rs {tr.pnl.mean():,.0f}")
    print(f"Avg winner     : {wins.net_pct.mean():.3f}%  |  Avg loser: {losers.net_pct.mean():.3f}%" if len(wins) and len(losers) else "")
    if len(wins) and len(losers):
        exp = (len(wins)/len(tr))*wins.net_pct.mean() + (len(losers)/len(tr))*losers.net_pct.mean()
        print(f"Expectancy     : {exp:.3f}% per trade")
    print(f"Max drawdown   : Rs {dd:,.0f}")
    print("="*60)
    print(tr.reason.value_counts().to_string())
    print("="*60)
    tr.to_csv("orb_trades.csv", index=False)
    print("Trade log -> orb_trades.csv")


if __name__ == "__main__":
    report(backtest(load_data()))
