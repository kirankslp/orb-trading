"""
Today's ORB trade plan: what to watch, at what levels, for how many shares.

    python daily_plan.py --premarket   # watchlist only, before the open
    python daily_plan.py               # full plan, run AFTER the range completes

The opening range is only known once OR_MINUTES of trading have elapsed, so the
full plan is empty before 10:00 IST on a 09:15 open with a 45-minute range.

This produces levels for a human to act on. It places no orders.
"""

import argparse
import datetime

import pandas as pd

import orb_backtest as ob
import symbol_screener as sc

IST = "Asia/Kolkata"


def today_ist():
    return pd.Timestamp.now(tz=IST).date()


def todays_watchlist(n=None, days=None):
    """Rank on closes through YESTERDAY. Slicing strictly before today also
    drops the partial daily bar Yahoo serves while the session is live.

    Returns (symbols, {symbol: turnover_cr}) so levels can be costed at each
    name's own slippage tier rather than a flat rate.
    """
    daily = sc.fetch_daily(sc.load_universe(), days=days or sc.LOOKBACK_DAYS + 60)
    budget = ob.slot_budget()
    ranked = sc.screen_asof(daily, today_ist(), max_price=budget)
    if ranked.empty:
        return [], {}
    syms = ranked.symbol.head(n or ob.MAX_POSITIONS).tolist()
    return syms, dict(zip(ranked.symbol, ranked.turnover_cr))


def opening_range(df, day, n_or):
    """(high, low, completed_at) for the day's first n_or candles, or None."""
    g = df[df["date"] == day].sort_values("dt")
    if len(g) < n_or:
        return None
    b = g.iloc[:n_or]
    return float(b["High"].max()), float(b["Low"].min()), g.iloc[n_or-1]["time"]


def levels(symbol, or_high, or_low, budget, turnover_cr=None):
    """Both sides of the breakout as placeable stop orders."""
    rows = []
    for side, trig in (("LONG", or_high), ("SHORT", or_low)):
        qty = int(budget // trig)
        if qty == 0:
            continue
        sign = 1 if side == "LONG" else -1
        sl  = trig * (1 - sign*ob.SL_PCT)
        tgt = trig * (1 + sign*ob.TARGET_PCT)
        # net of charges + slippage, same accounting the backtest uses, so these
        # are what actually lands in the account rather than the raw stop distance
        cost = (ob.charges(trig*qty, sl*qty)
                + (trig*qty + sl*qty) * ob.slippage_for(turnover_cr))
        risk, reward = abs(trig-sl)*qty + cost, abs(tgt-trig)*qty - cost
        rows.append(dict(symbol=symbol, side=side, trigger=round(trig,2),
                         stop=round(sl,2), target=round(tgt,2), qty=qty,
                         deploy=round(trig*qty), cost=round(cost,1),
                         risk=round(risk,1), reward=round(reward,1),
                         rr=round(reward/risk,2) if risk else None))
    return rows


def build(premarket=False):
    day, n_or, budget = today_ist(), ob.or_candles(), ob.slot_budget()
    syms, liq = todays_watchlist()
    if not syms:
        return day, [], [], "screener returned nothing affordable"

    if premarket:
        return day, syms, [], None

    intraday = ob.load_many(syms)
    rows, pending = [], []
    for s in syms:
        df = intraday.get(s)
        if df is None or df[df["date"] == day].empty:
            pending.append(f"{s}: no bars for {day} (holiday, halt, or feed lag)")
            continue
        r = opening_range(df, day, n_or)
        if r is None:
            pending.append(f"{s}: range still forming, need {n_or} candles")
            continue
        hi, lo, done_at = r
        rows.extend(levels(s, hi, lo, budget, liq.get(s)))
    return day, syms, rows, "; ".join(pending) if pending else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--premarket", action="store_true",
                    help="watchlist only, skip the opening-range levels")
    a = ap.parse_args()

    day, syms, rows, note = build(a.premarket)
    budget = ob.slot_budget()

    print("="*74)
    print(f"ORB PLAN {day}   budget Rs {ob.DAY_BUDGET:,.0f} x {ob.LEVERAGE:g} "
          f"over {ob.MAX_POSITIONS} slots = Rs {budget:,.0f}/position")
    print(f"range {ob.OR_MINUTES}m | stop {ob.SL_PCT*100:.2f}% | "
          f"target {ob.TARGET_PCT*100:.2f}% | square off {ob.SQUAREOFF_TIME}")
    print("="*74)
    print("Watchlist:", ", ".join(syms) if syms else "(empty)")

    if a.premarket:
        print("\nPre-market run. Levels need the first "
              f"{ob.OR_MINUTES} minutes; re-run after the range completes.")
    elif rows:
        d = pd.DataFrame(rows)
        print()
        print(d.to_string(index=False))
        print(f"\nIf every long triggers : Rs {d[d.side=='LONG'].deploy.sum():,.0f} "
              f"deployed, Rs {d[d.side=='LONG'].risk.sum():,.1f} at risk")
        print(f"Worst case both sides  : Rs {d.risk.sum():,.1f} "
              f"({d.risk.sum()/ob.DAY_BUDGET*100:.1f}% of budget) if every stop fills")
        d.to_csv("daily_plan.csv", index=False)
        print("\nPlan -> daily_plan.csv")
    else:
        print("\nNo levels produced.")

    if note:
        print(f"\nnote: {note}")
    print("\nLevels only. No orders placed. Verify against your broker feed "
          "before acting.")


if __name__ == "__main__":
    main()
