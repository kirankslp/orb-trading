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
INTERVAL       = "15m"        # 5m or 15m
PERIOD         = "60d"        # yfinance intraday history cap ~60d
OR_MINUTES     = 45           # opening range = first N minutes
SL_PCT         = 0.004        # stop loss 0.4% from entry
TARGET_PCT     = 0.008        # target 0.8% from entry (2:1)
SQUAREOFF_TIME = "15:15"      # force exit time

# ---- capital ----
DAY_BUDGET     = 10000        # TOTAL rupees working across all positions in a day
LEVERAGE       = 1.0          # 1.0 = own cash. Intraday MIS runs ~5x at most
                              # brokers; it scales size and losses alike
MAX_POSITIONS  = 2            # DAY_BUDGET splits across this many picks. More
                              # names = better spread, but a smaller per-slot
                              # budget strands cash on high-priced shares
# Whole shares only. Anything the per-slot budget cannot buy one of is skipped.

# ---- costs: NSE intraday equity, Zerodha-style ----
# These decide the outcome at a 10k budget. A flat 0.03% understates them ~3x.
BROKERAGE_PCT  = 0.0003       # 0.03% per executed order...
BROKERAGE_CAP  = 20           # ...or Rs 20, whichever is LOWER
STT_SELL_PCT   = 0.00025      # 0.025%, sell leg only
EXCH_TXN_PCT   = 0.0000297    # NSE, both legs
SEBI_PCT       = 0.000001     # both legs
STAMP_BUY_PCT  = 0.00003      # buy leg only
GST_PCT        = 0.18         # on brokerage + txn + sebi
SLIPPAGE_PCT   = 0.0005       # 0.05% per leg, stop/market fills

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


def slot_budget():
    """Rupees available to a single position."""
    return DAY_BUDGET * LEVERAGE / MAX_POSITIONS


def charges(buy_val, sell_val):
    """Round-trip statutory + broker charges in rupees, on actual turnover.

    Brokerage is per-order min(cap, pct), so at a small budget it stays
    percentage-based and the Rs 20 cap never binds.
    """
    brok  = (min(BROKERAGE_CAP, buy_val * BROKERAGE_PCT)
             + min(BROKERAGE_CAP, sell_val * BROKERAGE_PCT))
    txn   = (buy_val + sell_val) * EXCH_TXN_PCT
    sebi  = (buy_val + sell_val) * SEBI_PCT
    stt   = sell_val * STT_SELL_PCT
    stamp = buy_val * STAMP_BUY_PCT
    gst   = (brok + txn + sebi) * GST_PCT
    return brok + txn + sebi + stt + stamp + gst


def _pnl(day, side, entry, exit_px, t_in, t_out, reason, ambiguous=False,
         symbol=None, qty=1):
    gross = ((exit_px - entry) if side == "LONG" else (entry - exit_px)) * qty
    # a short sells first, so the legs swap but the turnover is the same shape
    buy_val, sell_val = ((entry*qty, exit_px*qty) if side == "LONG"
                         else (exit_px*qty, entry*qty))
    cost = charges(buy_val, sell_val) + (buy_val + sell_val) * SLIPPAGE_PCT
    net = gross - cost
    deployed = entry * qty
    return dict(date=day, symbol=symbol, side=side, qty=qty,
                entry=round(entry,2), exit=round(exit_px,2), deployed=round(deployed,0),
                entry_time=t_in, exit_time=t_out, reason=reason, ambiguous=ambiguous,
                gross_pct=round(gross/deployed*100,3),
                gross=round(gross,1), cost=round(cost,1), pnl=round(net,1))


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


def trade_day(day, g, n_or, symbol=None, budget=None):
    """Run one symbol through one session. Returns a trade dict, or None."""
    budget = slot_budget() if budget is None else budget
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

            qty = int(budget // entry)     # whole shares only, no fractions on NSE
            if qty == 0:
                return None                # one share costs more than the slot

            # The breakout candle can carry its own reversal. Price reached the
            # entry level inside this candle, so the rest of the candle is
            # tradeable and has to be resolved before moving on.
            if ENTRY_BAR_POLICY != "skip":
                hit = _resolve_exit(side, sl, tgt, h, l,
                                    allow_stop=ENTRY_BAR_POLICY == "conservative")
                if hit:
                    px, reason, amb = hit
                    return _pnl(day, side, entry, px, entry_time, t, reason, amb, symbol, qty)
            continue

        # --- in a trade: check square-off, then the levels ---
        if t >= SQUAREOFF_TIME:
            return _pnl(day, side, entry, c, entry_time, t, "squareoff", False, symbol, qty)
        hit = _resolve_exit(side, sl, tgt, h, l)
        if hit:
            px, reason, amb = hit
            return _pnl(day, side, entry, px, entry_time, t, reason, amb, symbol, qty)

    if side is not None:       # data ran out with the position still open
        last = g.iloc[-1]
        return _pnl(day, side, entry, last["Close"], entry_time, last["time"], "eod", False, symbol, qty)
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
    """Daily picks. intraday: {symbol: df}. picks: {date: [symbols]}.

    Returns (trades, unaffordable) where unaffordable lists the (date, symbol,
    price) slots the budget could not buy a single share of.
    """
    n_or, budget = or_candles(), slot_budget()
    trades, unaffordable = [], []
    for day in sorted(picks):
        for sym in picks[day]:
            df = intraday.get(sym)
            if df is None:
                continue
            g = df[df["date"] == day]
            if g.empty:
                continue
            px = float(g.iloc[0]["Open"])
            if px > budget:
                unaffordable.append((day, sym, round(px, 1)))
                continue
            t = trade_day(day, g, n_or, sym, budget)
            if t:
                trades.append(t)
    return pd.DataFrame(trades), unaffordable


def report(tr, title="", unaffordable=None):
    if tr.empty:
        print("No trades generated."); return
    tr = tr.sort_values(["date","entry_time"]).copy()
    wins, losers = tr[tr.pnl>0], tr[tr.pnl<=0]

    # Positions run concurrently in screener mode, so the equity curve has to be
    # built from daily totals. Cumsum over individual trades would imply an
    # intraday ordering across symbols that the data does not support.
    daily = tr.groupby("date").pnl.sum().sort_index()
    dd = (daily.cumsum() - daily.cumsum().cummax()).min()
    net, gross, cost = tr.pnl.sum(), tr.gross.sum(), tr.cost.sum()

    print("="*70)
    print(title or f"Symbol {SYMBOL}")
    print(f"{INTERVAL} | sessions {tr.date.nunique()} | entry-bar policy {ENTRY_BAR_POLICY}")
    print(f"Budget Rs {DAY_BUDGET:,.0f}/day x {LEVERAGE:g} leverage "
          f"over {MAX_POSITIONS} slots = Rs {slot_budget():,.0f}/position")
    print("="*70)
    print(f"Total trades   : {len(tr)}")
    print(f"Win rate       : {len(wins)/len(tr)*100:.1f}%")
    print(f"Gross P&L      : Rs {gross:,.0f}")
    print(f"Costs paid     : Rs {cost:,.0f}   ({cost/max(abs(gross),1e-9)*100:.0f}% of gross)")
    print(f"NET P&L        : Rs {net:,.0f}   ({net/DAY_BUDGET*100:+.1f}% of day budget)")
    print(f"Avg P&L/trade  : Rs {tr.pnl.mean():,.1f}")
    print(f"Avg P&L/day    : Rs {daily.mean():,.1f}")
    print(f"Cost per trade : Rs {tr.cost.mean():,.1f} on Rs {tr.deployed.mean():,.0f} "
          f"deployed ({tr.cost.sum()/tr.deployed.sum()*100:.3f}% round trip)")
    if len(wins) and len(losers):
        print(f"Avg winner     : Rs {wins.pnl.mean():,.1f}  |  Avg loser: Rs {losers.pnl.mean():,.1f}")
        exp = (len(wins)/len(tr))*wins.pnl.mean() + (len(losers)/len(tr))*losers.pnl.mean()
        print(f"Expectancy     : Rs {exp:,.1f} per trade")
    print(f"Max drawdown   : Rs {dd:,.0f}  (on the daily equity curve)")
    print(f"Capital used   : Rs {tr.deployed.mean():,.0f} avg/position of "
          f"Rs {slot_budget():,.0f} allowed ({tr.deployed.mean()/slot_budget()*100:.0f}% "
          f"working, rest stranded by whole-share sizing)")
    amb = int(tr.ambiguous.sum())
    print(f"Ambiguous bars : {amb} ({amb/len(tr)*100:.1f}% of trades exited on a "
          f"candle spanning both levels; outcome is an assumption, not data)")
    if unaffordable:
        syms = sorted({s for _, s, _ in unaffordable})
        print(f"Skipped        : {len(unaffordable)} slots priced above "
              f"Rs {slot_budget():,.0f}/share -> {', '.join(syms[:8])}"
              f"{' ...' if len(syms) > 8 else ''}")
    print("="*70)
    print(tr.reason.value_counts().to_string())

    if tr.symbol.nunique() > 1:
        by = tr.groupby("symbol").agg(trades=("pnl","size"), qty=("qty","mean"),
                                      net=("pnl","sum"), cost=("cost","sum"),
                                      win=("pnl", lambda s: (s>0).mean()*100))
        by[["qty","win"]] = by[["qty","win"]].round(1)
        print("="*70)
        print(by.sort_values("net", ascending=False).to_string())
        per_day = tr.groupby("date")
        print(f"\nPositions/day  : avg {per_day.size().mean():.1f}, max {per_day.size().max()}")
        print(f"Peak exposure  : Rs {per_day.deployed.sum().max():,.0f} of "
              f"Rs {DAY_BUDGET*LEVERAGE:,.0f} available")
    print("="*70)
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

    budget = slot_budget()
    picks = {d: sc.watchlist_asof(daily, d, MAX_POSITIONS, max_price=budget)
             for d in sessions}
    picks = {d: p for d, p in picks.items() if p}
    if not picks:
        raise SystemExit(
            f"Screener returned no picks. At Rs {budget:,.0f}/position nothing in "
            f"the universe is affordable, or the liquidity filters are too tight.")

    needed = sorted({s for p in picks.values() for s in p})
    print(f"{len(sessions)} sessions | {len(needed)} distinct symbols ever picked "
          f"| top {MAX_POSITIONS}/day at Rs {budget:,.0f}/position")
    print("Fetching intraday bars...")
    intraday = load_many(needed)
    missing = [s for s in needed if s not in intraday]
    if missing:
        print(f"no intraday data for: {', '.join(missing)}")
    trades, unaffordable = backtest_watchlist(intraday, picks)
    report(trades, unaffordable=unaffordable,
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
