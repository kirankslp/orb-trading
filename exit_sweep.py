"""
Stop and target widths for the ORB, tested out of sample.

The trade diagnostics showed where orb45 loses: the 15% of trades that hit
their stop cost about Rs 49,500, more than everything the other 85% made. A
trade log cannot say whether a different stop would have done better, because
it does not know what a stopped trade did next. This re-runs the same picks,
days and costs through the frozen engine with other exit widths.

Variants, fixed before any run:

    stop    0.5x ATR (current), 0.75x, 1.0x, none
    target  1.0x ATR (current), none

"none" means no level at all: the position runs to the 15:15 square-off.
Finite stops keep the engine's 0.2%..5% clamp. With the current stop the
target keeps the engine's 2:1 link to the clamped stop, so the (0.5x, 1.0x)
row reproduces the strategy comparison exactly; check its net against that run.

Judged the same way as the tuning levers: sessions split in time, a variant
must beat the current exits in the first two thirds and clearly beat them in
the last third (95% interval on the per-session difference). It counts as a
fix only if it is also profitable in the last third. Seven variants on two
strategies is 14 tries, so a single pass can be luck: paper-test before any
config change. A variant without a stop also carries gap and circuit risk the
backtest cannot see, so a real version would keep a wide disaster stop.

Research only. Nothing here trades.

    python exit_sweep.py
"""

import argparse
import itertools

import numpy as np
import pandas as pd

import orb_backtest as ob
import stats
import strategy_backtest as sb
from trade_diagnostics import split_sessions

STOP_MULTS = (0.5, 0.75, 1.0, None)
TARGET_MULTS = (1.0, None)
CURRENT = (0.5, 1.0)
FAR = 0.9            # a level 90% away: never reached intraday, i.e. no level


def label(stop, target):
    s = "no stop" if stop is None else f"stop {stop:g}x"
    t = "no target" if target is None else f"target {target:g}x"
    return f"{s}, {t}"


def make_levels(stop, target):
    """A replacement for ob.levels_for with other ATR multiples."""
    lo, hi = ob.ATR_BOUNDS

    def levels(atr_pct=None):
        if not atr_pct or not np.isfinite(atr_pct):
            sl, tgt = ob.SL_PCT, ob.TARGET_PCT
        else:
            a = atr_pct / 100.0
            sl = min(max(a * stop, lo), hi) if stop is not None else FAR
            if target is None:
                tgt = FAR
            elif stop is not None:
                tgt = sl * (target / stop)        # the engine's link to the clamped stop
            else:
                tgt = max(a * target, lo)
            return sl, tgt
        return (sl if stop is not None else FAR), (tgt if target is not None else FAR)
    return levels


def with_levels(stop, target, fn):
    """Run fn with the engine's exits swapped, and always restore them."""
    original = ob.levels_for
    if (stop, target) != CURRENT:
        ob.levels_for = make_levels(stop, target)
    try:
        return fn()
    finally:
        ob.levels_for = original


def daily_net(tr, sessions):
    if tr is None or tr.empty:
        return pd.Series(0.0, index=sessions)
    return tr.groupby("date")["pnl"].sum().reindex(sessions, fill_value=0.0)


def evaluate(variants, sessions):
    """variants: {(stop, target): trades}. Rows with early/late results and
    the paired difference against the current exits."""
    early, late = split_sessions(sessions)
    cur = daily_net(variants[CURRENT], sessions)
    rows = []
    for key, tr in variants.items():
        d = daily_net(tr, sessions)
        diff = d - cur
        _, dlo, dhi = stats.mean_ci(diff[late])
        lo, hi = stats.session_total_ci(tr[tr["date"].isin(late)] if len(tr) else tr,
                                        "pnl", sessions=late)
        mix = (tr["reason"].value_counts(normalize=True) * 100) if len(tr) else pd.Series(dtype=float)
        rows.append(dict(
            key=key, label=label(*key), trades=len(tr),
            stop_pct=mix.get("stoploss", 0.0), target_pct=mix.get("target", 0.0),
            gross_tr=tr["gross"].mean() if len(tr) else np.nan,
            net=d.sum(), early_net=d[early].sum(), late_net=d[late].sum(),
            early_diff=diff[early].sum(), late_diff=diff[late].sum(),
            late_diff_lo=None if dlo is None else dlo * len(late),
            late_diff_hi=None if dhi is None else dhi * len(late),
            late_lo=lo, late_hi=hi))
    return rows


def verdict(row):
    if row["key"] == CURRENT:
        return "current exits"
    if not row["early_diff"] > 0:
        return "no: not better in the earlier part"
    if row["late_diff_lo"] is None or not row["late_diff_lo"] > 0:
        return "no: no clear gain in the later third"
    if row["late_lo"] is not None and row["late_lo"] > 0:
        return "BETTER and PROFITABLE later: paper-test it"
    return "better than current, still not profitable"


def _n(x, w=9):
    return f"{'-':>{w}}" if x is None or not np.isfinite(x) else f"{x:>{w},.0f}"


def print_table(name, rows, sessions):
    early, late = split_sessions(sessions)
    print(f"\n{name}   earlier: {early[0]} to {early[-1]} ({len(early)})   "
          f"later: {late[0]} to {late[-1]} ({len(late)})")
    print(f"  {'exits':<26}{'trades':>7}{'stop%':>7}{'tgt%':>6}{'gross/tr':>9}{'net':>9}"
          f"{'early net':>10}{'late net':>9}{'vs current, early':>18}{'vs current, late (95% CI)':>34}   verdict")
    for r in rows:
        ci = (f"{_n(r['late_diff'], 8)} ({_n(r['late_diff_lo'], 7)} .. {_n(r['late_diff_hi'], 7)})"
              if r["key"] != CURRENT else f"{'-':>8}")
        print(f"  {r['label']:<26}{r['trades']:>7}{r['stop_pct']:>6.0f}%{r['target_pct']:>5.0f}%"
              f"{_n(r['gross_tr'], 9)}{_n(r['net'])}{_n(r['early_net'], 10)}{_n(r['late_net'])}"
              f"{_n(r['early_diff'], 18) if r['key'] != CURRENT else '-':>18}{ci:>34}   {verdict(r)}")


def run(market_data=None):
    pool, sessions, picks, metrics = ob.screener_picks(market_data)
    needed = sorted({s for p in picks.values() for s in p})
    sessions = sorted(str(s) for s in sessions) if sessions else []
    print(f"{len(sessions)} sessions | {len(needed)} distinct symbols picked")
    print(f"Fetching {ob.INTERVAL} and {sb.ORB15_INTERVAL} bars...")
    bars = ob.load_many(needed, market_data=market_data)
    bars5 = sb.load_interval(needed, sb.ORB15_INTERVAL, market_data)
    n15 = sb.ORB15_MINUTES // int(sb.ORB15_INTERVAL.rstrip("m"))

    strategies = {
        "orb45": lambda: ob.backtest_watchlist(bars, picks, metrics)[0],
        "orb15": lambda: sb.orb_watchlist(bars5, picks, metrics, n15),
    }
    print("=" * 100)
    print("EXIT SWEEP   same picks, days and costs; only the stop and target widths change")
    print("=" * 100)
    for name, fn in strategies.items():
        variants = {}
        for stop, target in itertools.product(STOP_MULTS, TARGET_MULTS):
            tr = with_levels(stop, target, fn)
            if len(tr):
                tr = tr.assign(date=tr["date"].astype(str))
            variants[(stop, target)] = tr
        days = sessions or sorted({d for tr in variants.values() for d in tr.get("date", [])})
        print_table(name, evaluate(variants, days), days)
    print("\nThe current row must match the strategy comparison's net; if not, stop and report it.")
    print("A variant without a stop has no protection from gaps or circuits in real trading.")
    print("Anything that passes is a candidate for paper trading, not a config change.")
    print("\nBacktest only. No orders placed, no config changed.")


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args(argv)
    run()


if __name__ == "__main__":
    main()
