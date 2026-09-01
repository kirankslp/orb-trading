"""
Grid-search stop and target against real data, reusing a single download.

    python sweep.py              # fetch once, cache, sweep
    python sweep.py --refetch    # force a fresh download
    python sweep.py --slots 1    # sweep at a different MAX_POSITIONS

Stop and target do not affect WHICH symbols get picked, so one fetch serves the
whole grid. MAX_POSITIONS does change the watchlist (it changes the per-slot
budget, and so what is affordable), which is why it is a flag rather than a
swept axis: changing it rebuilds the cache.

Reads the same CONFIG as orb_backtest, so DAY_BUDGET and the cost model here are
whatever that file says.
"""

import argparse
import datetime
import os
import pickle

import numpy as np
import pandas as pd

import orb_backtest as ob
import symbol_screener as sc

CACHE = "sweep_cache.pkl"

# (target, stop). Held at 2:1 so the sweep isolates SIZE from ratio; the last two
# widen the ratio as well, to see whether the stop or the target is the problem.
GRID = [(0.008, 0.004), (0.012, 0.006), (0.016, 0.008), (0.020, 0.010),
        (0.030, 0.015), (0.012, 0.008), (0.016, 0.010), (0.024, 0.012)]

# --wide: does gross keep climbing, or does it flatten as everything turns into
# a hold-to-squareoff? The answer decides whether widening is an edge or just a
# slower way of running the clock out.
WIDE = [(0.008, 0.004), (0.020, 0.010), (0.030, 0.015), (0.040, 0.020),
        (0.050, 0.025), (0.060, 0.030), (0.080, 0.040)]


def build_cache(slots):
    pool = sc.load_universe()
    daily = sc.fetch_daily(pool, days=sc.LOOKBACK_DAYS + 120)
    sessions = sorted({d for df in daily.values() for d in df.index.date})
    cutoff = sessions[-1] - datetime.timedelta(days=int(ob.PERIOD.rstrip("d")))
    sessions = [d for d in sessions if d > cutoff]

    budget = ob.DAY_BUDGET * ob.LEVERAGE / slots
    picks = {d: sc.watchlist_asof(daily, d, slots, max_price=budget) for d in sessions}
    picks = {d: p for d, p in picks.items() if p}
    if not picks:
        raise SystemExit("screener returned no picks")

    needed = sorted({s for p in picks.values() for s in p})
    ranked = sc.screen_asof(daily, max_price=budget)
    liquidity = dict(zip(ranked.symbol, ranked.turnover_cr)) if not ranked.empty else {}

    print(f"{len(sessions)} sessions | pool {len(pool)} | {len(needed)} symbols "
          f"| fetching intraday...")
    intraday = ob.load_many(needed)
    return dict(picks=picks, intraday=intraday, liquidity=liquidity, slots=slots,
                pool=len(pool), day_budget=ob.DAY_BUDGET,
                built=datetime.datetime.now())


def load_cache(slots, refetch):
    if not refetch and os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            c = pickle.load(f)
        stale = (c["slots"] != slots or c["day_budget"] != ob.DAY_BUDGET
                 or "liquidity" not in c)
        if not stale:
            age = datetime.datetime.now() - c["built"]
            print(f"using {CACHE} built {age.days}d {age.seconds//3600}h ago "
                  f"(--refetch to rebuild)")
            return c
        print("cache was built for a different budget/slots, rebuilding")
    c = build_cache(slots)
    with open(CACHE, "wb") as f:
        pickle.dump(c, f)
    return c


def run(cache, tgt, sl):
    ob.TARGET_PCT, ob.SL_PCT, ob.MAX_POSITIONS = tgt, sl, cache["slots"]
    tr, _ = ob.backtest_watchlist(cache["intraday"], cache["picks"],
                                  cache.get("liquidity"))
    if tr.empty:
        return None, None
    n = len(tr)
    daily = tr.groupby("date").pnl.sum().sort_index()
    reasons = tr.reason.value_counts()
    # SE of mean net per trade: how much of this is signal vs 40 sessions of luck
    se = tr.pnl.std(ddof=1) / np.sqrt(n)
    return dict(
        tgt=tgt*100, sl=sl*100, rr=tgt/sl, n=n,
        win=(tr.pnl > 0).mean()*100,
        gross=tr.gross.sum(), cost=tr.cost.sum(), net=tr.pnl.sum(),
        per_trade=tr.pnl.mean(), se=se,
        gross_pt=tr.gross.mean(),
        dd=(daily.cumsum() - daily.cumsum().cummax()).min(),
        sqoff=int(reasons.get("squareoff", 0)) + int(reasons.get("eod", 0)),
        tgt_hits=int(reasons.get("target", 0)),
        stops=int(reasons.get("stoploss", 0)),
    ), tr


def paired(base, tr):
    """Gross difference per trade against the baseline config.

    Entries are identical across the grid (picks, opening ranges and breakout
    detection do not depend on stop or target), so this is a paired measurement.
    Its standard error is much smaller than the SE of either config's level,
    which is what makes a real improvement visible in ~40 sessions.
    """
    k = ["date", "symbol"]
    a, b = base.set_index(k).gross, tr.set_index(k).gross
    common = a.index.intersection(b.index)
    if len(common) < 2:
        return np.nan, np.nan
    d = b.loc[common] - a.loc[common]
    return d.mean(), d.std(ddof=1) / np.sqrt(len(d))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--slots", type=int, default=ob.MAX_POSITIONS)
    ap.add_argument("--wide", action="store_true",
                    help="push the target out until the strategy is just a hold")
    a = ap.parse_args()

    cache = load_cache(a.slots, a.refetch)
    grid = WIDE if a.wide else GRID

    rows, base = [], None
    for t, s in grid:
        r, tr = run(cache, t, s)
        if not r:
            continue
        if base is None:
            base = tr
        r["dgross"], r["dse"] = paired(base, tr)
        rows.append(r)
    d = pd.DataFrame(rows)

    liq = cache.get("liquidity") or {}
    tiers = sorted({ob.slippage_for(v) for v in liq.values()}) if liq else [ob.SLIPPAGE_PCT]
    print(f"\nDAY_BUDGET Rs {ob.DAY_BUDGET:,.0f} over {a.slots} slot(s) | pool "
          f"{cache.get('pool', '?')} | slippage tiers in use: "
          f"{', '.join(f'{t*100:.2f}%' for t in tiers)}/leg")
    print(f"vs-base columns compare against {grid[0][0]*100:.1f}%/"
          f"{grid[0][1]*100:.2f}% on identical entries (paired)")
    print("="*118)
    show = d.copy()
    show["net/trade"] = show.per_trade.round(1).astype(str) + " +-" + show.se.round(1).astype(str)
    show["gross/trade"] = show.gross_pt.round(1)
    # paired t on the gross improvement: |mean| / SE above ~2 is hard to dismiss
    show["vs base"] = show.dgross.round(1).astype(str) + " +-" + show.dse.round(1).astype(str)
    show["t"] = (show.dgross / show.dse).round(1)
    show["exits t/s/sq"] = (show.tgt_hits.astype(str) + "/" + show.stops.astype(str)
                            + "/" + show.sqoff.astype(str))
    show = show[["tgt","sl","rr","n","win","gross","cost","net","gross/trade",
                 "net/trade","vs base","t","dd","exits t/s/sq"]]
    print(show.to_string(index=False, float_format=lambda v: f"{v:,.1f}"))
    print("="*118)

    best = d.loc[d.net.idxmax()]
    print(f"\nBest net: {best.tgt:.1f}%/{best.sl:.2f}% -> Rs {best.net:,.0f} "
          f"over {best.n:.0f} trades")
    print(f"  gross/trade Rs {best.gross_pt:.1f}, cost/trade "
          f"Rs {best.cost/best.n:.1f}, net/trade Rs {best.per_trade:.1f} "
          f"+- {best.se:.1f} (1 SE)")

    # The honest test: is gross per trade big enough to pay for the round trip?
    cost_pt = best.cost / best.n
    hi = best.gross_pt + 1.96*best.se
    verdict = ("costs exceed the optimistic end of the gross edge; no parameter "
               "in this grid rescues it"
               if hi < cost_pt else
               "gross edge may cover costs at the optimistic end; worth more data")
    print(f"\n  gross/trade 95% upper bound Rs {hi:.1f} vs cost/trade "
          f"Rs {cost_pt:.1f}\n  -> {verdict}")

    # Probability the true gross edge clears costs, treating the estimate as
    # normal. Blunt, but more useful than eyeballing an interval.
    from math import erf, sqrt
    z = (cost_pt - best.gross_pt) / best.se if best.se else float("inf")
    p = 0.5 * (1 - erf(z / sqrt(2)))
    print(f"  P(true gross/trade > cost/trade) ~ {p*100:.0f}%")

    print(f"\nThis was the best of {len(d)} configs, so its estimate is biased "
          "upward by\nthe selection itself. The 'vs base' column is the honest "
          "one: it is paired,\nso it measures whether widening actually changed "
          "anything on the same trades.")
    print("~40 sessions is a small sample. A single positive cell is noise "
          "unless it clears about 2 SE.")


if __name__ == "__main__":
    main()
