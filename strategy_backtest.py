"""
Compare intraday strategies on identical picks, days and costs.

    python strategy_backtest.py
    python strategy_backtest.py --only orb45,vwap,bollinger

Strategies:
    orb45      the frozen ORB: 45-minute range on 15m bars (baseline)
    orb15      ORB on the first 15 minutes, on 5m bars (AlgoTest's variant),
               same ATR stops and targets
    vwap       VWAP mean reversion          (strategies.py)
    bollinger  Bollinger mean reversion     (strategies.py)
    ema        9/20 EMA crossover           (strategies.py)

Every strategy trades the SAME point-in-time screener picks on the SAME
sessions through the SAME cost model. Only the rule differs, so the daily P&L
of two strategies can be differenced session by session. That paired
difference has far less noise than either total, which is why the headline
here is "versus the baseline", not each strategy's own P&L.

Also reports how far price actually travelled after each ORB entry (maximum
favourable excursion), in daily-ATR units and in opening-range widths. The
frozen ORB hit its 1.0x ATR target on 2.8% of trades; this says what a
reachable target would have looked like, as a diagnosis rather than a
parameter search.

Writes strategy_trades.csv. Research only: no orders, no config changes.
"""

import argparse
import math

import numpy as np
import pandas as pd

import orb_backtest as ob
import strategies as st
from kite_data import KiteMarketData

ALL = ("orb45", "orb15", "vwap", "bollinger", "ema")
ORB15_INTERVAL = "5m"
ORB15_MINUTES = 15


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def load_interval(symbols, interval, market_data=None):
    """Like ob.load_many, for an interval other than the configured one."""
    md = market_data or KiteMarketData()
    out = {}
    for s in symbols:
        d = md.intraday(s, interval, ob._period_days())
        if not d.empty:
            out[s] = ob._normalize(d)
    return out


# --------------------------------------------------------------------------
# ORB on a different range length, through the frozen engine
# --------------------------------------------------------------------------

def orb_watchlist(intraday, picks, metrics, n_or):
    """ob.trade_day with an explicit range length. The engine is untouched;
    only how many candles make the opening range changes."""
    budget, trades = ob.slot_budget(), []
    for day in sorted(picks):
        for sym in picks[day]:
            df = intraday.get(sym)
            if df is None:
                continue
            g = df[df["date"] == day]
            if g.empty or float(g.iloc[0]["Open"]) > budget:
                continue
            m = metrics.get((day, sym)) or {}
            t = ob.trade_day(day, g, n_or, sym, budget,
                             m.get("turnover_cr"), m.get("atr_pct"))
            if t:
                trades.append(t)
    return pd.DataFrame(trades)


def excursions(trades, intraday, metrics, n_or):
    """Maximum favourable excursion after each ORB entry, to the square-off.

    Measured from the bar AFTER the entry bar, because the entry bar's own
    high or low may have printed before the fill. That understates reach
    slightly, which is the conservative direction for a claim that a target
    was reachable.
    """
    rows = []
    for t in trades.itertuples():
        df = intraday.get(t.symbol)
        if df is None:
            continue
        g = df[df["date"] == t.date].sort_values("dt").reset_index(drop=True)
        if len(g) <= n_or:
            continue
        or_w = (g.iloc[:n_or]["High"].max() - g.iloc[:n_or]["Low"].min()) / t.entry
        after = g[(g["time"] > t.entry_time) & (g["time"] < ob.SQUAREOFF_TIME)]
        if after.empty:
            mfe = 0.0
        elif t.side == "LONG":
            mfe = max(0.0, (after["High"].max() - t.entry) / t.entry)
        else:
            mfe = max(0.0, (t.entry - after["Low"].min()) / t.entry)
        atr = (metrics.get((t.date, t.symbol)) or {}).get("atr_pct")
        rows.append(dict(mfe_pct=mfe * 100, or_width_pct=or_w * 100,
                         mfe_atr=mfe / (atr / 100) if atr else np.nan,
                         mfe_or=mfe / or_w if or_w > 0 else np.nan))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------

def summarise(tr):
    n = len(tr)
    wins, losses = tr[tr.pnl > 0], tr[tr.pnl <= 0]
    net, gross, cost = tr.pnl.sum(), tr.gross.sum(), tr.cost.sum()
    sd = tr.pnl.std(ddof=1) if n > 1 else float("nan")
    half = 1.96 * sd * math.sqrt(n) if n > 1 else float("nan")
    aw = wins.pnl.mean() if len(wins) else 0.0
    al = -losses.pnl.mean() if len(losses) else 0.0
    return dict(
        trades=n, win=len(wins) / n * 100 if n else 0.0, gross=gross, cost=cost,
        net=net, per_trade=net / n if n else 0.0,
        cost_share=cost / abs(gross) * 100 if gross else float("inf"),
        rr=aw / al if al else float("nan"), lo=net - half, hi=net + half,
        exits=tr.reason.value_counts(normalize=True).mul(100).round(1).to_dict())


def paired(tr_a, tr_b, sessions):
    """Session-by-session difference in net P&L, a minus b.

    A session where a strategy took no trade counts as zero, not as missing:
    standing aside is a result too.
    """
    da = tr_a.groupby("date").pnl.sum().reindex(sessions, fill_value=0.0)
    db = tr_b.groupby("date").pnl.sum().reindex(sessions, fill_value=0.0)
    d = da - db
    n = len(d)
    half = 1.96 * d.std(ddof=1) * math.sqrt(n) if n > 1 else float("nan")
    return d.sum(), d.sum() - half, d.sum() + half, (d > 0).mean() * 100


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def print_report(results, sessions, reach):
    print("=" * 94)
    print(f"STRATEGY COMPARISON   {len(sessions)} sessions | Rs {ob.DAY_BUDGET:,} over "
          f"{ob.MAX_POSITIONS} slots | identical picks, days and costs")
    print("=" * 94)
    hdr = (f"{'strategy':<10} {'trades':>6} {'win%':>6} {'gross':>9} {'costs':>9} "
           f"{'net':>9} {'net/tr':>7} {'cost/gr':>8} {'RR':>5}  95% CI on net")
    print(hdr)
    print("-" * len(hdr))
    for name, tr in results.items():
        if tr.empty:
            print(f"{name:<10} {'no trades':>6}")
            continue
        s = summarise(tr)
        cs = f"{s['cost_share']:.0f}%" if np.isfinite(s["cost_share"]) else "n/a"
        print(f"{name:<10} {s['trades']:>6} {s['win']:>5.1f}% {s['gross']:>9,.0f} "
              f"{s['cost']:>9,.0f} {s['net']:>9,.0f} {s['per_trade']:>7.1f} {cs:>8} "
              f"{s['rr']:>5.2f}  {s['lo']:>8,.0f} .. {s['hi']:,.0f}"
              + ("   ~0" if s["lo"] <= 0 <= s["hi"] else ""))
    print("  ~0 marks a net P&L statistically indistinguishable from zero.")

    print("\nExit mix (% of trades)")
    for name, tr in results.items():
        if not tr.empty:
            ex = summarise(tr)["exits"]
            print(f"  {name:<10} " + "  ".join(f"{k} {v:.0f}%" for k, v in sorted(ex.items())))

    base = results.get("orb45")
    if base is not None and not base.empty:
        print("\nPaired against orb45, session by session (positive = better than orb45)")
        for name, tr in results.items():
            if name == "orb45" or tr.empty:
                continue
            tot, lo, hi, share = paired(tr, base, sessions)
            verdict = ("not distinguishable from orb45" if lo <= 0 <= hi else
                       "BETTER than orb45" if lo > 0 else "WORSE than orb45")
            print(f"  {name:<10} Rs{tot:>9,.0f}   95% CI {lo:>8,.0f} .. {hi:>8,.0f}   "
                  f"better on {share:.0f}% of sessions   {verdict}")

    for name, ex in reach.items():
        if ex.empty:
            continue
        print(f"\nTarget reachability for {name}: share of trades whose price travelled "
              f"at least this far after entry  (n={len(ex)})")
        atr = ex["mfe_atr"].dropna()
        orw = ex["mfe_or"].dropna()
        print("  in daily ATR  : " + "  ".join(
            f">={m:g}x {(atr >= m).mean() * 100:>4.1f}%" for m in (0.25, 0.5, 0.75, 1.0)))
        print("  in OR widths  : " + "  ".join(
            f">={m:g}x {(orw >= m).mean() * 100:>4.1f}%" for m in (0.5, 1.0, 1.5, 2.0)))
        print(f"  median OR width {ex['or_width_pct'].median():.2f}% of price, "
              f"median excursion {ex['mfe_pct'].median():.2f}%")
    if reach:
        print("  The frozen ORB targets 1.0x daily ATR. Read this as which unit of "
              "target the market\n  actually reaches, not as a multiple to pick: "
              "choosing the best row here is fitting\n  these sessions.")


# --------------------------------------------------------------------------

def run(only=ALL, market_data=None):
    pool, sessions, picks, metrics = ob.screener_picks(market_data)
    needed = sorted({s for p in picks.values() for s in p})
    print(f"{len(sessions)} sessions | pool {len(pool)} | {len(needed)} distinct "
          f"symbols picked | strategies: {', '.join(only)}")

    results, reach = {}, {}
    if any(s in only for s in ("orb45", "vwap", "bollinger", "ema")):
        print(f"Fetching {ob.INTERVAL} bars...")
        bars = ob.load_many(needed, market_data=market_data)
        if "orb45" in only:
            tr, _ = ob.backtest_watchlist(bars, picks, metrics)
            results["orb45"] = tr
            if not tr.empty:
                reach["orb45"] = excursions(tr, bars, metrics, ob.or_candles())
        for name in ("vwap", "bollinger", "ema"):
            if name in only:
                results[name] = st.backtest_watchlist(st.STRATEGIES[name], bars,
                                                      picks, metrics)

    if "orb15" in only:
        print(f"Fetching {ORB15_INTERVAL} bars for the 15-minute ORB...")
        bars5 = load_interval(needed, ORB15_INTERVAL, market_data)
        n_or = ORB15_MINUTES // int(ORB15_INTERVAL.rstrip("m"))
        tr = orb_watchlist(bars5, picks, metrics, n_or)
        results["orb15"] = tr
        if not tr.empty:
            reach["orb15"] = excursions(tr, bars5, metrics, n_or)

    ordered = {k: results[k] for k in ALL if k in results}
    print_report(ordered, sessions, reach)

    frames = [tr.assign(strategy=k) for k, tr in ordered.items() if not tr.empty]
    if frames:
        pd.concat(frames, ignore_index=True).to_csv("strategy_trades.csv", index=False)
        print("\nTrade log -> strategy_trades.csv")
    print("\nBacktest only. No orders placed, no config changed.")
    return ordered, reach


def main():
    ap = argparse.ArgumentParser(description="Compare intraday strategies.")
    ap.add_argument("--only", help=f"comma list from {','.join(ALL)}")
    a = ap.parse_args()
    only = tuple(x.strip() for x in a.only.split(",")) if a.only else ALL
    bad = [x for x in only if x not in ALL]
    if bad:
        raise SystemExit(f"unknown strategy: {', '.join(bad)}. Choose from {', '.join(ALL)}")
    run(only)


if __name__ == "__main__":
    main()
