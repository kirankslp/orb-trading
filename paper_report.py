"""
Paper-trading report: what the ledger actually supports concluding.

    python paper_report.py
    python paper_report.py --days 7

The headline number is deliberately NOT the P&L. Over a 30-session forward test
at this trade count the P&L distribution of a good strategy and a bad one
overlap almost completely: a 45%-win-rate edge still loses money about a
quarter of the time, and a 35% loser still makes money about a quarter of the
time. Reading a month of P&L as a verdict is the single easiest way to end up
tuning on noise.

So this report leads with the confidence interval and says plainly when the
result is indistinguishable from zero. What it treats as real signal is the
execution diagnostics, because those are per-trade measurements rather than
win/loss bits and they converge roughly an order of magnitude faster:

  entry drift   - published level vs actual fill, the slippage assumption
                  AGENT.md flags as unvalidated and worth half of all friction
  trigger rate  - how often a planned level is reached at all
  ambiguous     - trades whose outcome is a modelling assumption, not data
  cost share    - friction as a fraction of gross

Nothing here places orders or edits config.
"""

import argparse
import math

import pandas as pd

import orb_backtest as ob
import paper_broker as pb


def _wilson(k, n, z=1.96):
    """Wilson score interval. Beats normal approximation at small n, which is
    the only regime this report ever runs in."""
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def _pnl_ci(pnl, z=1.96):
    """CI on total P&L, from the per-trade spread. n<2 has no spread to use."""
    n = len(pnl)
    if n < 2:
        return None, None
    se_total = pnl.std(ddof=1) * math.sqrt(n)
    total = pnl.sum()
    return total - z * se_total, total + z * se_total


def breakeven_win_rate(led):
    """Break-even win rate implied by the trades actually taken.

    Uses realised average win and loss rather than the configured R:R, so it
    reflects the costs and fills that happened, not the ones assumed.
    """
    wins, losses = led[led.pnl > 0], led[led.pnl <= 0]
    if wins.empty or losses.empty:
        return None
    w, l = wins.pnl.mean(), abs(losses.pnl.mean())
    return l / (w + l)


def summarise(led):
    n = len(led)
    wins = led[led.pnl > 0]
    win_rate = len(wins) / n if n else 0.0
    lo_w, hi_w = _wilson(len(wins), n)
    lo_p, hi_p = _pnl_ci(led.pnl)
    be = breakeven_win_rate(led)
    return dict(n=n, sessions=led.plan_date.nunique(), win_rate=win_rate,
                win_lo=lo_w, win_hi=hi_w, pnl=led.pnl.sum(),
                pnl_lo=lo_p, pnl_hi=hi_p, gross=led.gross.sum(),
                cost=led.cost.sum(), breakeven=be)


def report(led, days=None):
    if led.empty:
        print("Ledger is empty. Run `python paper_broker.py resolve` first.")
        return

    led = led.copy()
    led["plan_date"] = led["plan_date"].astype(str)
    if days:
        keep = sorted(led.plan_date.unique())[-days:]
        led = led[led.plan_date.isin(keep)]

    s = summarise(led)
    equity = pb.STARTING_CAPITAL + s["pnl"]

    print("=" * 74)
    print(f"PAPER TRADING  {led.plan_date.min()} .. {led.plan_date.max()}")
    print(f"{s['sessions']} sessions, {s['n']} trades, "
          f"Rs{ob.DAY_BUDGET:,} budget over {ob.MAX_POSITIONS} slots")
    print("=" * 74)

    print(f"\nNet P&L         Rs{s['pnl']:>12,.1f}   "
          f"({s['pnl']/pb.STARTING_CAPITAL*100:+.2f}% on capital)")
    print(f"Equity          Rs{equity:>12,.1f}")
    print(f"Gross           Rs{s['gross']:>12,.1f}")
    print(f"Costs           Rs{s['cost']:>12,.1f}"
          + (f"   ({s['cost']/abs(s['gross'])*100:.1f}% of gross)"
             if s["gross"] else ""))

    # --- the guard rail -------------------------------------------------
    if s["pnl_lo"] is not None:
        print(f"\n95% CI on P&L   Rs{s['pnl_lo']:,.0f} .. Rs{s['pnl_hi']:,.0f}")
        if s["pnl_lo"] <= 0 <= s["pnl_hi"]:
            print("  VERDICT: indistinguishable from zero. This sample does not")
            print("  show an edge and does not rule one out. Do not tune on it.")
        elif s["pnl_lo"] > 0:
            print("  VERDICT: profit is significant at this sample size.")
        else:
            print("  VERDICT: loss is significant at this sample size.")

    print(f"\nWin rate        {s['win_rate']*100:>11.1f}%   "
          f"95% CI {s['win_lo']*100:.1f}% .. {s['win_hi']*100:.1f}%")
    if s["breakeven"] is not None:
        print(f"Break-even      {s['breakeven']*100:>11.1f}%   "
              "(from realised average win and loss)")
        if s["win_lo"] <= s["breakeven"] <= s["win_hi"]:
            print("  The break-even rate sits inside the confidence interval, so")
            print("  the win rate cannot yet be called better or worse than it.")

    # --- what this sample DOES measure ---------------------------------
    print("\n" + "-" * 74)
    print("EXECUTION DIAGNOSTICS  (converge far faster than P&L)")
    print("-" * 74)

    drift = led["entry_drift_pct"]
    se = drift.std(ddof=1) / math.sqrt(len(drift)) if len(drift) > 1 else 0.0
    print(f"\nEntry drift     {drift.mean():>+11.4f}%  per fill, "
          f"95% CI {drift.mean()-1.96*se:+.4f}% .. {drift.mean()+1.96*se:+.4f}%")
    print("  Published level vs actual fill. Compare against the slippage tier")
    print("  assumed for these names:")
    for tier, sl in ob.SLIPPAGE_TIERS:
        print(f"    >{tier:>5} cr/day: {sl*100:.2f}% assumed per leg")

    amb = led["ambiguous"].astype(str).str.lower().isin(["true", "1"]).mean()
    lo_a, hi_a = _wilson(int(amb * len(led)), len(led))
    print(f"\nAmbiguous       {amb*100:>11.1f}%  of trades, "
          f"95% CI {lo_a*100:.1f}% .. {hi_a*100:.1f}%")
    print("  Candle spanned both stop and target, so the outcome is the")
    print(f"  ENTRY_BAR_POLICY='{ob.ENTRY_BAR_POLICY}' assumption, not observed data.")
    if amb > 0.15:
        print("  ABOVE 15%: too much of this result is assumption. Flag it.")

    print("\nExit reasons")
    for reason, count in led["reason"].value_counts().items():
        sub = led[led.reason == reason]
        print(f"  {reason:<12} {count:>4}  Rs{sub.pnl.sum():>10,.1f}")

    print("\nBy side")
    for side, sub in led.groupby("side"):
        wr = (sub.pnl > 0).mean() * 100
        print(f"  {side:<6} {len(sub):>4} trades  win {wr:>5.1f}%  "
              f"Rs{sub.pnl.sum():>10,.1f}")

    # --- daily equity ---------------------------------------------------
    daily = led.groupby("plan_date").pnl.sum()
    curve = pb.STARTING_CAPITAL + daily.cumsum()
    peak = curve.cummax()
    dd = (curve - peak)
    print(f"\nMax drawdown    Rs{dd.min():>12,.1f}   "
          f"({dd.min()/pb.STARTING_CAPITAL*100:.2f}% of capital)")
    print(f"Best session    Rs{daily.max():>12,.1f}")
    print(f"Worst session   Rs{daily.min():>12,.1f}")

    print("\nLast 10 sessions")
    for d, v in daily.tail(10).items():
        print(f"  {d}  Rs{v:>10,.1f}   equity Rs{curve[d]:>12,.1f}")

    # --- freeze integrity ----------------------------------------------
    fps = led["plan_fingerprint"].dropna().unique()
    print("\n" + "-" * 74)
    if len(fps) > 1:
        print(f"WARNING: {len(fps)} config fingerprints in this window. The")
        print("strategy changed mid-sample, so these trades are not one")
        print("experiment and must not be pooled.")
    else:
        print(f"Config frozen throughout: fingerprint {fps[0] if len(fps) else 'n/a'}")

    print("\nPaper trades against recorded levels. Fills are modelled, not real.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="limit to the last N sessions")
    a = ap.parse_args()
    report(pb.read_ledger(), a.days)


if __name__ == "__main__":
    main()
