"""
Measure what a fill would really cost, from Kite's live order book.

    python spread_probe.py snap              # one snapshot of today's picks
    python spread_probe.py loop --every 15   # snapshot every 15 min until close
    python spread_probe.py report            # measured vs modelled slippage

Why this exists
---------------
On a 179-session backtest every strategy except Bollinger made Rs12-14 a trade
before costs and paid Rs17.8 in costs, a gap of Rs4-5. The largest single line
in those costs is slippage, and it is an assumption: 0.03%-0.05% per leg by
liquidity tier, never measured. The gap is about the size of that assumption,
so whether any of this is tradeable comes down to one unmeasured number.

This measures it without trading. For each of the day's picks it reads the
five-level book and prices an order of the actual slot size against it:
walk the asks to fill a market buy, walk the bids to fill a market sell, and
compare the average fill to the mid. That is what a market or stop-market
order pays to cross the book, which is what the backtest's slippage models.

It is a LOWER bound for this strategy. Breakout entries are stop orders that
trigger while price is moving fast, exactly when books thin and spreads widen;
a snapshot on a schedule mostly catches calmer moments. If the measured cost
already exceeds the model, the strategy is in worse shape than the backtest
says. If it is well under, that is necessary but not sufficient.

No orders are placed. Snapshots append to spreads/spreads.csv.
"""

import argparse
import json
import os
import time

import pandas as pd

import daily_plan as dp
import orb_backtest as ob
from kite_data import kite_symbol

SPREAD_DIR = os.environ.get("SPREAD_DIR", "spreads")
SPREADS_CSV = os.path.join(SPREAD_DIR, "spreads.csv")
OPEN, CLOSE = "09:15", "15:30"
# Time-of-day buckets: the morning bucket is when ORB breakouts actually fire.
BUCKETS = (("09:15", "10:00", "open"), ("10:00", "11:00", "breakout hour"),
           ("11:00", "14:30", "midday"), ("14:30", "15:30", "close"))


# --------------------------------------------------------------------------
# pure book arithmetic
# --------------------------------------------------------------------------

def walk(levels, qty):
    """Average fill price for `qty` shares against book levels, best first.

    Returns (avg_price, filled_qty). filled_qty < qty means five levels of
    depth were not enough, which is itself worth knowing.
    """
    remaining, cost, filled = qty, 0.0, 0
    for lvl in levels:
        price, avail = float(lvl.get("price") or 0), int(lvl.get("quantity") or 0)
        if price <= 0 or avail <= 0:
            continue
        take = min(remaining, avail)
        cost += take * price
        filled += take
        remaining -= take
        if remaining == 0:
            break
    return (cost / filled if filled else None), filled


def book_metrics(book, notional):
    """Spread and slot-sized impact for one symbol's book, or None if one side
    of the book is empty (pre-open, halt, circuit)."""
    bids, asks = book.get("buy") or [], book.get("sell") or []
    bid = next((float(l["price"]) for l in bids if float(l.get("price") or 0) > 0), None)
    ask = next((float(l["price"]) for l in asks if float(l.get("price") or 0) > 0), None)
    if bid is None or ask is None or ask < bid:
        return None
    mid = (bid + ask) / 2
    qty = max(1, int(notional // mid))
    buy_px, buy_fill = walk(asks, qty)
    sell_px, sell_fill = walk(bids, qty)
    return dict(
        bid=bid, ask=ask, mid=mid, qty=qty,
        spread_pct=(ask - bid) / mid * 100,
        half_spread_pct=(ask - bid) / 2 / mid * 100,
        # Cost of crossing the book for the full slot, per leg, vs the mid.
        buy_impact_pct=(buy_px - mid) / mid * 100 if buy_px else None,
        sell_impact_pct=(mid - sell_px) / mid * 100 if sell_px else None,
        depth_ok=buy_fill >= qty and sell_fill >= qty,
    )


def bucket(hhmm):
    for lo, hi, name in BUCKETS:
        if lo <= hhmm < hi:
            return name
    return "outside"


# --------------------------------------------------------------------------
# watchlist: built once a day, because ranking needs the full daily fetch
# --------------------------------------------------------------------------

def _watchlist_path(day):
    return os.path.join(SPREAD_DIR, f"watchlist_{day}.json")


def todays_symbols(market_data=None, day=None):
    """Today's picks plus each one's turnover, cached for the day.

    Ranking the universe means a daily-bar fetch for every symbol, about 15
    minutes. It only changes once a day, so a loop of snapshots must not redo
    it every time.
    """
    day = day or dp.today_ist()
    path = _watchlist_path(day)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
        return cached["symbols"], cached["turnover_cr"]
    syms, liq = dp.todays_watchlist(market_data=market_data)
    turnover = {s: (liq.get(s) or {}).get("turnover_cr") for s in syms}
    os.makedirs(SPREAD_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"date": str(day), "symbols": syms, "turnover_cr": turnover}, fh, indent=2)
    return syms, turnover


# --------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------

def now_ist():
    return pd.Timestamp.now(tz=dp.IST)


def market_open(ts=None):
    ts = ts or now_ist()
    return ts.weekday() < 5 and OPEN <= ts.strftime("%H:%M") <= CLOSE


def snapshot(market_data, symbols, turnover, notional=None, ts=None):
    """One row per symbol with a two-sided book. Appends to SPREADS_CSV."""
    notional = notional or ob.slot_budget()
    ts = ts or now_ist()
    books = market_data.order_books(symbols)
    rows = []
    for s in symbols:
        m = book_metrics(books.get(kite_symbol(s)) or {}, notional)
        if m is None:
            continue
        tc = turnover.get(s)
        rows.append(dict(
            ts=ts.isoformat(timespec="seconds"), date=str(ts.date()),
            time=ts.strftime("%H:%M"), bucket=bucket(ts.strftime("%H:%M")),
            symbol=s, turnover_cr=tc, notional=notional,
            modelled_slip_pct=ob.slippage_for(tc) * 100, **m))
    if rows:
        os.makedirs(SPREAD_DIR, exist_ok=True)
        frame = pd.DataFrame(rows)
        frame.to_csv(SPREADS_CSV, mode="a", header=not os.path.exists(SPREADS_CSV),
                     index=False)
    return rows


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def report(df, gap=None):
    if df.empty:
        print("No snapshots yet. Run `python spread_probe.py snap` during market hours.")
        return
    df = df.copy()
    df["leg_cost_pct"] = df[["buy_impact_pct", "sell_impact_pct"]].mean(axis=1)
    notional = float(df["notional"].median())

    print("=" * 78)
    print(f"MEASURED vs MODELLED SLIPPAGE   {df['date'].nunique()} days, "
          f"{len(df)} snapshots, {df['symbol'].nunique()} symbols, "
          f"Rs{notional:,.0f} orders")
    print("=" * 78)
    meas, model = df["leg_cost_pct"].median(), df["modelled_slip_pct"].median()
    print(f"\nPer leg, median:  measured {meas:.4f}%   modelled {model:.4f}%   "
          f"(half-spread alone {df['half_spread_pct'].median():.4f}%)")
    print(f"Per leg, 90th percentile measured: {df['leg_cost_pct'].quantile(0.9):.4f}%")
    saving = 2 * (model - meas) / 100 * notional
    print(f"Round trip on Rs{notional:,.0f}: measured is "
          f"Rs{abs(saving):.2f} {'CHEAPER' if saving > 0 else 'DEARER'} than the model")
    if gap is not None:
        print(f"Backtest gap to break-even: Rs{gap:.2f}/trade -> "
              + ("measured slippage would close it" if saving >= gap else
                 f"still Rs{gap - saving:.2f}/trade short even at measured slippage"))
    # Read back from CSV, so compare as text: bool("False") is True.
    thin = (~df["depth_ok"].astype(str).str.lower().eq("true")).mean() * 100
    if thin:
        print(f"Five-level book too thin to fill the slot in {thin:.1f}% of snapshots")

    print("\nBy time of day (median per-leg cost)")
    for _, _, name in BUCKETS:
        sub = df[df["bucket"] == name]
        if len(sub):
            print(f"  {name:<14} {sub['leg_cost_pct'].median():.4f}%   n={len(sub)}")

    print("\nBy symbol (median per-leg cost vs modelled tier)")
    per = (df.groupby("symbol")
             .agg(n=("leg_cost_pct", "size"), measured=("leg_cost_pct", "median"),
                  modelled=("modelled_slip_pct", "median"))
             .sort_values("measured", ascending=False))
    for sym, r in per.head(15).iterrows():
        flag = "  ABOVE MODEL" if r.measured > r.modelled else ""
        print(f"  {sym:<16} {r.measured:.4f}%  vs {r.modelled:.4f}%   n={int(r.n)}{flag}")

    print("\nA lower bound: breakout entries are stop orders that fire while price is")
    print("moving fast, when books thin and spreads widen. Scheduled snapshots mostly")
    print("catch calmer moments. Read 'cheaper than the model' as necessary, not")
    print("sufficient. No orders placed.")


def read_spreads():
    return pd.read_csv(SPREADS_CSV) if os.path.exists(SPREADS_CSV) else pd.DataFrame()


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Measure fill costs from the live order book.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("snap", help="one snapshot now")
    s1.add_argument("--force", action="store_true", help="snapshot even outside market hours")
    s2 = sub.add_parser("loop", help="snapshot repeatedly until the close")
    s2.add_argument("--every", type=int, default=15, help="minutes between snapshots")
    s3 = sub.add_parser("report", help="measured vs modelled slippage")
    s3.add_argument("--gap", type=float, help="backtest gap to break-even, Rs/trade")
    a = ap.parse_args()

    if a.cmd == "report":
        report(read_spreads(), a.gap)
        return

    from kite_data import KiteMarketData
    md = KiteMarketData()
    if a.cmd == "snap" and not (a.force or market_open()):
        raise SystemExit("Market is closed: an off-hours book is empty or stale. "
                         "Use --force to snapshot anyway.")
    print("Building today's watchlist (cached after the first run of the day)...")
    syms, turnover = todays_symbols(md)
    if not syms:
        raise SystemExit("Screener returned no picks today.")
    print(f"Watching {len(syms)}: {', '.join(syms)}")

    if a.cmd == "snap":
        rows = snapshot(md, syms, turnover)
        print(f"{len(rows)} books logged -> {SPREADS_CSV}")
        return

    while True:
        ts = now_ist()
        if ts.strftime("%H:%M") > CLOSE or ts.weekday() >= 5:
            print("Market closed. Done for today.")
            break
        if market_open(ts):
            rows = snapshot(md, syms, turnover, ts=ts)
            med = (pd.DataFrame(rows)[["buy_impact_pct", "sell_impact_pct"]].mean(axis=1).median()
                   if rows else float("nan"))
            print(f"{ts.strftime('%H:%M')}  {len(rows)} books  median per-leg cost "
                  f"{med:.4f}%" if rows else f"{ts.strftime('%H:%M')}  no two-sided books")
        else:
            print(f"{ts.strftime('%H:%M')}  waiting for the open")
        time.sleep(max(60, a.every * 60))


if __name__ == "__main__":
    main()
