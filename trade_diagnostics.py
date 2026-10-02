"""
Where do the intraday strategies make and lose money, and does any simple
tuning survive out of sample?

Reads an existing trade log (no Kite, no token) and reports:

  A. Per strategy: exit mix, and how many trades moved less than their own
     costs (the "nothing happened" trades that only pay friction).
  B. For one strategy in detail: results by exit reason, side, entry time,
     time held, stop distance, liquidity tier, position size and weekday.
     Description only.
  C. Tuning levers, tested out of sample. The levers are fixed below, before
     any run. Sessions are split in time: the first two thirds are where a
     lever would have been chosen, the last third is where it is judged.

Why C is built this way: on a strategy that loses money per trade, ANY filter
that trades less loses less, so "the total improved" proves nothing. A lever
counts only if the trades it keeps beat the trades it drops in BOTH halves.
Better means ahead in the earlier two thirds AND ahead in the later third with
a 95% interval above zero (one draw per session, so same-day trades are not
treated as independent). On random data about a quarter of levers look better
in both halves by chance; the interval is what screens most of them out. It
counts as fixing the strategy only if the kept trades are also profitable in
the later third with a 95% interval above zero. Even then the next step is paper
trading, because seven levers times five strategies is 35 tries, and about
two of them will look good by chance.

Research only. Nothing here trades.

    python trade_diagnostics.py                   latest comparison, orb45 in detail
    python trade_diagnostics.py --strategy ema
    python trade_diagnostics.py --log archive:strategy_trades_<stamp>.csv
"""

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

import calendar_data
import stats

ROOT = Path(__file__).resolve().parent
TRAIN_SHARE = 2 / 3

# Levers, fixed before any run. Each keeps the trades for which it is True.
LEVERS = (
    ("entries before 11:00", lambda t: t["entry_time"] < "11:00"),
    ("entries before 12:00", lambda t: t["entry_time"] < "12:00"),
    ("long only", lambda t: t["side"] == "LONG"),
    ("short only", lambda t: t["side"] == "SHORT"),
    ("most liquid tier only", lambda t: t["slip_pct"] <= 0.03),
    ("stop at least 1% away", lambda t: t["sl_pct"] >= 1.0),
    ("slot at least 90% used", lambda t: t["deployed"] >= 9000),
)


# --------------------------------------------------------------------------
# Preparation
# --------------------------------------------------------------------------

def _minutes(hhmm):
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return np.nan


def prepare(trades):
    """Adds held_min and the 'moved less than its costs' flag."""
    t = trades.copy()
    for col, default in (("side", ""), ("reason", ""), ("entry_time", ""), ("exit_time", "")):
        if col not in t.columns:
            t[col] = default
    for col in ("slip_pct", "sl_pct", "deployed"):
        if col not in t.columns:
            t[col] = np.nan
    t["entry_time"] = t["entry_time"].astype(str)
    t["held_min"] = [_minutes(b) - _minutes(a) for a, b in zip(t["entry_time"], t["exit_time"])]
    t["inside_costs"] = t["gross"].abs() < t["cost"]
    return t


def base_strategies(trades):
    """Strategies in the log, without the *_rvol rows, which are subsets of
    their base strategy's trades rather than separate trades."""
    names = [s for s in trades["strategy"].unique() if not str(s).endswith("_rvol")]
    return sorted(names, key=lambda s: (s != "orb45", s))


def split_sessions(sessions, share=TRAIN_SHARE):
    """Chronological split: (earlier, later)."""
    s = sorted(sessions)
    k = int(round(len(s) * share))
    return s[:k], s[k:]


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------

def group_stats(t, key, order=None):
    """Per group: trades, win%, gross and net per trade, total net, and the
    mean minutes held."""
    names = order if order is not None else sorted(t[key].dropna().unique(), key=str)
    rows = []
    for name in names:
        b = t[t[key] == name]
        if b.empty:
            continue
        rows.append(dict(group=str(name), trades=len(b), win=(b.pnl > 0).mean() * 100,
                         gross_tr=b.gross.mean(), net_tr=b.pnl.mean(), net=b.pnl.sum(),
                         held=b.held_min.mean()))
    return pd.DataFrame(rows)


def _bucket(values, edges, labels):
    out = []
    for v in values:
        if v is None or not np.isfinite(v):
            out.append("unknown")
            continue
        for (lo, hi), label in zip(edges, labels):
            if lo <= v < hi:
                out.append(label)
                break
        else:
            out.append("unknown")
    return out


ENTRY_EDGES = ((0, 600), (600, 630), (630, 660), (660, 720), (720, 780), (780, 24 * 60))
ENTRY_LABELS = ("before 10:00", "10:00-10:30", "10:30-11:00", "11:00-12:00", "12:00-13:00", "13:00 on")
HELD_EDGES = ((0, 30), (30, 60), (60, 120), (120, 240), (240, 24 * 60))
HELD_LABELS = ("< 30 min", "30-60 min", "1-2 h", "2-4 h", "4 h +")
STOP_EDGES = ((0, 1), (1, 2), (2, 3), (3, 100))
STOP_LABELS = ("< 1%", "1-2%", "2-3%", ">= 3%")
SIZE_EDGES = ((0, 7000), (7000, 9000), (9000, 1e12))
SIZE_LABELS = ("< Rs7k", "Rs7-9k", ">= Rs9k")


def add_buckets(t):
    t = t.copy()
    t["entry_bucket"] = _bucket([_minutes(x) for x in t["entry_time"]], ENTRY_EDGES, ENTRY_LABELS)
    t["held_bucket"] = _bucket(t["held_min"], HELD_EDGES, HELD_LABELS)
    t["stop_bucket"] = _bucket(t["sl_pct"], STOP_EDGES, STOP_LABELS)
    t["size_bucket"] = _bucket(t["deployed"], SIZE_EDGES, SIZE_LABELS)
    t["tier"] = [f"{v:.2f}%/leg" if np.isfinite(v) else "unknown" for v in t["slip_pct"]]
    t["weekday"] = pd.to_datetime(t["date"]).dt.day_name().str[:3]
    return t


# --------------------------------------------------------------------------
# Levers
# --------------------------------------------------------------------------

def lever_result(t, keep_mask, sessions):
    """Kept versus dropped trades in the earlier and later sessions."""
    early, late = split_sessions(sessions)
    out = {}
    for half, days in (("early", early), ("late", late)):
        in_half = t["date"].isin(days)
        kept = t[in_half & keep_mask]
        dropped = t[in_half & ~keep_mask]
        out[half] = dict(kept_n=len(kept), dropped_n=len(dropped),
                         kept_gross=kept.gross.mean() if len(kept) else np.nan,
                         dropped_gross=dropped.gross.mean() if len(dropped) else np.nan,
                         kept_net=kept.pnl.mean() if len(kept) else np.nan,
                         all_net=t[in_half].pnl.mean() if in_half.any() else np.nan)
        if half == "late":
            lo, hi = stats.session_total_ci(kept, "pnl", sessions=days)
            # Kept minus dropped gross per trade, one draw per session that
            # has both, so same-day trades are not counted as independent.
            k = kept.groupby("date")["gross"].mean()
            d = dropped.groupby("date")["gross"].mean()
            both = k.index.intersection(d.index)
            _, dlo, dhi = stats.mean_ci(k[both] - d[both])
            out[half].update(kept_total=kept.pnl.sum(), lo=lo, hi=hi,
                             diff_lo=dlo, diff_hi=dhi, diff_sessions=len(both))
    return out


def lever_verdict(r):
    e, l = r["early"], r["late"]
    if min(e["kept_n"], l["kept_n"]) < 20 or min(e["dropped_n"], l["dropped_n"]) < 20:
        return "too few trades on one side"
    if not (e["kept_gross"] > e["dropped_gross"]):
        return "no: not better in the earlier part"
    if l.get("diff_lo") is None or not l["diff_lo"] > 0:
        return "no: no clear difference in the later third"
    if l.get("lo") is not None and l["lo"] > 0:
        return "PROFITABLE in the later third: paper-test it"
    return "picks better trades, still not profitable"


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _n(x, w=8, d=1):
    return f"{'-':>{w}}" if x is None or not np.isfinite(x) else f"{x:>{w},.{d}f}"


def print_groups(title, tab, note=None):
    print(f"\n  {title}" + (f"   ({note})" if note else ""))
    print(f"    {'group':<14}{'trades':>7}{'win%':>7}{'gross/tr':>10}{'net/tr':>9}{'net':>10}{'avg held':>10}")
    for r in tab.itertuples():
        held = f"{r.held:.0f} min" if np.isfinite(r.held) else "-"
        print(f"    {r.group:<14}{r.trades:>7}{r.win:>6.1f}%{_n(r.gross_tr, 10)}{_n(r.net_tr, 9)}"
              f"{_n(r.net, 10, 0)}{held:>10}")


def print_report(log, trades, focus):
    t = add_buckets(prepare(trades))
    sessions = sorted(t["date"].unique())
    names = base_strategies(t)
    early, late = split_sessions(sessions)
    width = 96
    print("=" * width)
    print(f"TRADE DIAGNOSTICS   {log['label']} | {len(sessions)} sessions | {sessions[0]} to {sessions[-1]}")
    print("=" * width)

    print("\nA. Where the money goes, per strategy")
    print(f"  {'strategy':<11}{'trades':>7}{'gross/tr':>10}{'cost/tr':>9}{'net/tr':>8}"
          f"{'moved < costs':>15}   exit mix")
    for name in names:
        s = t[t.strategy == name]
        mix = "  ".join(f"{k} {v:.0f}%" for k, v in
                        (s.reason.value_counts(normalize=True) * 100).sort_index().items())
        print(f"  {name:<11}{len(s):>7}{_n(s.gross.mean(), 10)}{_n(s.cost.mean(), 9)}{_n(s.pnl.mean(), 8)}"
              f"{s.inside_costs.mean() * 100:>14.0f}%   {mix}")
    print("  'moved < costs': trades whose whole gross move, up or down, was smaller than their")
    print("  costs. They cannot win; every one of them is friction paid for a non-event.")

    s = t[t.strategy == focus]
    if s.empty:
        print(f"\nNo trades for {focus}.")
    else:
        print(f"\nB. {focus} in detail. Description only: a good-looking row here is a lead, not a rule.")
        print_groups("by exit reason", group_stats(s, "reason"))
        print_groups("by side", group_stats(s, "side", ["LONG", "SHORT"]))
        print_groups("by entry time", group_stats(s, "entry_bucket", list(ENTRY_LABELS) + ["unknown"]),
                     "later entries have less time to move, but pay the same costs")
        print_groups("by time held", group_stats(s, "held_bucket", list(HELD_LABELS) + ["unknown"]))
        print_groups("by stop distance", group_stats(s, "stop_bucket", list(STOP_LABELS) + ["unknown"]),
                     "stop as % of entry, from daily ATR")
        print_groups("by liquidity tier", group_stats(s, "tier"), "modelled slippage per leg")
        print_groups("by slot used", group_stats(s, "size_bucket", list(SIZE_LABELS) + ["unknown"]),
                     "whole shares leave part of a Rs10k slot unspent")
        print_groups("by weekday", group_stats(s, "weekday", ["Mon", "Tue", "Wed", "Thu", "Fri"]))
        amb = int(s["ambiguous"].astype(str).str.lower().eq("true").sum()) if "ambiguous" in s else 0
        if amb:
            print(f"\n  {amb} trades hit stop and target inside one bar; counted as stops (the worse case).")

    print(f"\nC. Tuning levers, out of sample. Chosen-on: first {len(early)} sessions "
          f"({early[0]} to {early[-1]}). Judged-on: last {len(late)} ({late[0]} to {late[-1]}).")
    print("  'kept vs dropped' is gross per trade of the trades the lever keeps versus the ones it")
    print("  drops. To count, a lever must be ahead early AND clearly ahead late (95% interval,")
    print("  one draw per session).")
    for name in names:
        st = t[t.strategy == name]
        if st.empty:
            continue
        print(f"\n  {name}   (all trades: net/tr {_n(st[st.date.isin(early)].pnl.mean(), 0)} early, "
              f"{_n(st[st.date.isin(late)].pnl.mean(), 0)} late)")
        print(f"    {'lever':<24}{'kept vs dropped, early':>24}{'kept vs dropped, late':>24}"
              f"{'late net/tr':>12}   verdict")
        for label, rule in LEVERS:
            mask = rule(st).fillna(False).astype(bool)
            r = lever_result(st, mask, sessions)
            e, l = r["early"], r["late"]
            print(f"    {label:<24}{_n(e['kept_gross'], 10)} vs{_n(e['dropped_gross'], 9)}   "
                  f"{_n(l['kept_gross'], 10)} vs{_n(l['dropped_gross'], 9)}   {_n(l['kept_net'], 9)}"
                  f"   {lever_verdict(r)}")
    print("\n  Seven levers on several strategies is many tries: expect one or two to pass by luck.")
    print("  Anything that passes goes to paper trading on new sessions before it touches the config.")
    print("\nResearch only. No orders placed, no config changed.")


def run(log_id=None, focus="orb45", root=ROOT):
    from regime_report import pick_log
    log = pick_log(log_id, root)
    trades = calendar_data.load_trades(log)
    if trades.empty:
        raise SystemExit(f"{log['label']} has no trades.")
    print_report(log, trades, focus)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", help="trade log id (compare, orb, paper, archive:<file>); default: compare")
    ap.add_argument("--strategy", default="orb45", help="strategy shown in detail (default orb45)")
    args = ap.parse_args(argv)
    run(args.log, args.strategy)


if __name__ == "__main__":
    main()
