"""
Opening Range Breakout (ORB) intraday backtester.
One trade per symbol per day. Single-file, dependency-light.

    pip install yfinance pandas numpy
    python orb_backtest.py

Two modes, set by UNIVERSE_MODE:
  single   -> backtest SYMBOL alone
  screener -> for every session, rebuild the watchlist from symbol_screener
              using only closes up to the PREVIOUS day, then trade the top
              MAX_POSITIONS names that morning. Picks change daily.

Edit CONFIG to change symbol, range window, stops, targets.
"""

import datetime

import yfinance as yf
import pandas as pd
import numpy as np

# ------------------------- CONFIG -------------------------
UNIVERSE_MODE  = "screener"   # "screener" (daily picks) or "single"
SYMBOL         = "^NSEI"      # used by single mode. "^NSEBANK", "RELIANCE.NS"
MAX_POSITIONS  = 3            # screener mode: how many of the day's picks to trade
INTERVAL       = "15m"        # 5m or 15m
PERIOD         = "60d"        # yfinance intraday history cap ~60d
OR_MINUTES     = 45           # opening range = first N minutes
SL_PCT         = 0.004        # stop loss 0.4% from entry
TARGET_PCT     = 0.008        # target 0.8% from entry (2:1)
SQUAREOFF_TIME = "15:15"      # force exit time
CAPITAL        = 100000       # notional PER TRADE, so screener mode can deploy
                              # up to MAX_POSITIONS x CAPITAL on a given day
BROKERAGE_PCT  = 0.0003       # round-trip cost (brokerage+slippage+tax) 0.03%

# How to treat the breakout candle itself. OHLC hides the intrabar path, so the
# candle that triggers the entry may also contain the stop, the target, or both.
#   conservative : both levels live on the entry bar, the stop wins a tie
#   optimistic   : only the target can fill on the entry bar
#   skip         : no exits until the next bar (turns a reversal into a free hold)
ENTRY_BAR_POLICY = "conservative"
# ----------------------------------------------------------


def _normalize(df):
    """yfinance frame -> flat columns plus date/time/dt helpers, in IST."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    tcol = "Datetime" if "Datetime" in df.columns else "Date"
    df[tcol] = pd.to_datetime(df[tcol])
    if df[tcol].dt.tz is None:
        # Guessing here would silently shift every bar and misalign SQUAREOFF_TIME,
        # so say so rather than swallowing it.
        print(f"warning: {tcol} came back tz-naive, assuming UTC. Verify that "
              f"{SQUAREOFF_TIME} lines up with the exchange session.")
        df[tcol] = df[tcol].dt.tz_localize("UTC")
    df[tcol] = df[tcol].dt.tz_convert("Asia/Kolkata")
    df["date"] = df[tcol].dt.date
    df["time"] = df[tcol].dt.strftime("%H:%M")
    df["dt"]   = df[tcol]
    return df


def load_data(symbol=None):
    df = yf.download(symbol or SYMBOL, period=PERIOD, interval=INTERVAL, progress=False)
    if df.empty:
        raise SystemExit("No data returned. Check symbol / internet access.")
    return _normalize(df)


def load_many(symbols):
    """One multi-ticker intraday download, split per symbol."""
    symbols = list(symbols)
    if len(symbols) == 1:
        return {symbols[0]: load_data(symbols[0])}
    raw = yf.download(symbols, period=PERIOD, interval=INTERVAL,
                      group_by="ticker", progress=False)
    out = {}
    for s in symbols:
        try:
            d = raw[s].dropna(how="all")
        except KeyError:
            continue
        if not d.empty:
            out[s] = _normalize(d)
    return out


def _pnl(day, side, entry, exit_px, t_in, t_out, reason, ambiguous=False, symbol=None):
    gross_pct = ((exit_px - entry) if side == "LONG" else (entry - exit_px)) / entry
    net_pct = gross_pct - BROKERAGE_PCT
    return dict(date=day, symbol=symbol, side=side,
                entry=round(entry,2), exit=round(exit_px,2),
                entry_time=t_in, exit_time=t_out, reason=reason, ambiguous=ambiguous,
                gross_pct=round(gross_pct*100,3), net_pct=round(net_pct*100,3),
                pnl=round(net_pct*CAPITAL,1))


def _resolve_exit(side, sl, tgt, h, l, allow_stop=True):
    """Resolve one candle against the open position's levels.

    Returns (exit_px, reason, ambiguous) or None if the candle touched neither
    level. A candle that spans both is unresolvable from OHLC alone, so the stop
    is assumed to have filled first and the trade is tagged ambiguous.
    """
    if side == "LONG":
        hit_sl, hit_tgt = l <= sl, h >= tgt
    else:
        hit_sl, hit_tgt = h >= sl, l <= tgt
    ambiguous = hit_sl and hit_tgt
    if hit_sl and allow_stop:
        return sl, "stoploss", ambiguous
    if hit_tgt:
        return tgt, "target", ambiguous
    return None


def or_candles():
    return OR_MINUTES // int(INTERVAL.replace("m", ""))


def trade_day(day, g, n_or, symbol=None):
    """Run one symbol through one session. Returns a trade dict, or None."""
    g = g.sort_values("dt").reset_index(drop=True)
    if len(g) < n_or + 2:
        return None

    or_block = g.iloc[:n_or]
    or_high, or_low = or_block["High"].max(), or_block["Low"].min()

    side = entry = sl = tgt = entry_time = None

    for i in range(n_or, len(g)):
        row = g.iloc[i]
        o, h, l, c, t = row["Open"], row["High"], row["Low"], row["Close"], row["time"]

        # --- not yet in a trade: hunt for the breakout ---
        if side is None:
            if t >= SQUAREOFF_TIME:
                return None                 # too late to open anything today
            if h > or_high:
                side, entry_time = "LONG", t
                entry = max(or_high, o)     # a gap through the level fills worse
                sl, tgt = entry*(1-SL_PCT), entry*(1+TARGET_PCT)
            elif l < or_low:
                side, entry_time = "SHORT", t
                entry = min(or_low, o)
                sl, tgt = entry*(1+SL_PCT), entry*(1-TARGET_PCT)
            else:
                continue

            # The breakout candle can carry its own reversal. Price reached the
            # entry level inside this candle, so the rest of the candle is
            # tradeable and has to be resolved before moving on.
            if ENTRY_BAR_POLICY != "skip":
                hit = _resolve_exit(side, sl, tgt, h, l,
                                    allow_stop=ENTRY_BAR_POLICY == "conservative")
                if hit:
                    px, reason, amb = hit
                    return _pnl(day, side, entry, px, entry_time, t, reason, amb, symbol)
            continue

        # --- in a trade: check square-off, then the levels ---
        if t >= SQUAREOFF_TIME:
            return _pnl(day, side, entry, c, entry_time, t, "squareoff", False, symbol)
        hit = _resolve_exit(side, sl, tgt, h, l)
        if hit:
            px, reason, amb = hit
            return _pnl(day, side, entry, px, entry_time, t, reason, amb, symbol)

    if side is not None:       # data ran out with the position still open
        last = g.iloc[-1]
        return _pnl(day, side, entry, last["Close"], entry_time, last["time"], "eod", False, symbol)
    return None


def backtest(df, symbol=None):
    """Single symbol across every session in df."""
    n_or = or_candles()
    trades = []
    for day, g in df.groupby("date"):
        t = trade_day(day, g, n_or, symbol or SYMBOL)
        if t:
            trades.append(t)
    return pd.DataFrame(trades)


def backtest_watchlist(intraday, picks):
    """Daily picks. intraday: {symbol: df}. picks: {date: [symbols]}."""
    n_or = or_candles()
    trades = []
    for day in sorted(picks):
        for sym in picks[day]:
            df = intraday.get(sym)
            if df is None:
                continue
            g = df[df["date"] == day]
            if g.empty:
                continue
            t = trade_day(day, g, n_or, sym)
            if t:
                trades.append(t)
    return pd.DataFrame(trades)


def report(tr, title=""):
    if tr.empty:
        print("No trades generated."); return
    tr = tr.sort_values(["date","entry_time"]).copy()
    wins, losers = tr[tr.net_pct>0], tr[tr.net_pct<=0]

    # Positions run concurrently in screener mode, so the equity curve has to be
    # built from daily totals. Cumsum over individual trades would imply an
    # intraday ordering across symbols that the data does not support.
    daily = tr.groupby("date").pnl.sum().sort_index()
    dd = (daily.cumsum() - daily.cumsum().cummax()).min()

    print("="*68)
    print(title or f"Symbol {SYMBOL}")
    print(f"{INTERVAL} | sessions {tr.date.nunique()} | entry-bar policy {ENTRY_BAR_POLICY}")
    print("="*68)
    print(f"Total trades   : {len(tr)}")
    print(f"Win rate       : {len(wins)/len(tr)*100:.1f}%")
    print(f"Total net P&L  : Rs {tr.pnl.sum():,.0f}")
    print(f"Avg P&L/trade  : Rs {tr.pnl.mean():,.0f}")
    print(f"Avg P&L/day    : Rs {daily.mean():,.0f}")
    if len(wins) and len(losers):
        print(f"Avg winner     : {wins.net_pct.mean():.3f}%  |  Avg loser: {losers.net_pct.mean():.3f}%")
        exp = (len(wins)/len(tr))*wins.net_pct.mean() + (len(losers)/len(tr))*losers.net_pct.mean()
        print(f"Expectancy     : {exp:.3f}% per trade")
    print(f"Max drawdown   : Rs {dd:,.0f}  (on the daily equity curve)")
    amb = int(tr.ambiguous.sum())
    print(f"Ambiguous bars : {amb} ({amb/len(tr)*100:.1f}% of trades exited on a "
          f"candle spanning both levels; outcome is an assumption, not data)")
    print("="*68)
    print(tr.reason.value_counts().to_string())

    if tr.symbol.nunique() > 1:
        by = tr.groupby("symbol").agg(trades=("pnl","size"), net=("pnl","sum"),
                                      win=("net_pct", lambda s: (s>0).mean()*100))
        by["win"] = by["win"].round(1)
        print("="*68)
        print(by.sort_values("net", ascending=False).to_string())
        per_day = tr.groupby("date").size()
        print(f"\nPositions/day  : avg {per_day.mean():.1f}, max {per_day.max()}")
        print(f"Peak exposure  : Rs {per_day.max()*CAPITAL:,.0f}")
    print("="*68)
    tr.to_csv("orb_trades.csv", index=False)
    print("Trade log -> orb_trades.csv")


def run_screener_mode():
    import symbol_screener as sc

    # Daily history is cheap and unlimited, so pull enough to score the FIRST
    # session of the intraday window with a full lookback behind it.
    daily = sc.fetch_daily(sc.UNIVERSE, days=sc.LOOKBACK_DAYS + 120)
    sessions = sorted({d for df in daily.values() for d in df.index.date})
    cutoff = sessions[-1] - datetime.timedelta(days=int(PERIOD.rstrip("d")))
    sessions = [d for d in sessions if d > cutoff]   # only what intraday can cover

    picks = {d: sc.watchlist_asof(daily, d, MAX_POSITIONS) for d in sessions}
    picks = {d: p for d, p in picks.items() if p}
    if not picks:
        raise SystemExit("Screener returned no picks. Loosen the filters in symbol_screener.")

    needed = sorted({s for p in picks.values() for s in p})
    print(f"{len(sessions)} sessions | {len(needed)} distinct symbols ever picked "
          f"| top {MAX_POSITIONS}/day")
    print("Fetching intraday bars...")
    intraday = load_many(needed)
    missing = [s for s in needed if s not in intraday]
    if missing:
        print(f"no intraday data for: {', '.join(missing)}")
    report(backtest_watchlist(intraday, picks),
           title=f"ORB on daily screener picks (top {MAX_POSITIONS} of {len(sc.UNIVERSE)})")


if __name__ == "__main__":
    if ENTRY_BAR_POLICY not in ("conservative", "optimistic", "skip"):
        raise SystemExit(f"ENTRY_BAR_POLICY must be conservative/optimistic/skip, got {ENTRY_BAR_POLICY!r}")
    if UNIVERSE_MODE == "screener":
        run_screener_mode()
    elif UNIVERSE_MODE == "single":
        report(backtest(load_data()))
    else:
        raise SystemExit(f"UNIVERSE_MODE must be screener/single, got {UNIVERSE_MODE!r}")
