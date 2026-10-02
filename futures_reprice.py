"""
Would the same trades have survived in stock futures instead of shares?

Takes an existing trade log and re-prices every trade in a stock that has
futures, as ONE LOT of the nearest-expiry future, entered and exited at the
same prices. Three cost columns on identical trades and identical size:

  cash at lot size   the shares, but bought in the same rupee size as a lot,
                     so the Rs 20 brokerage cap applies just as it does to the
                     future. Isolates the instrument from the position size.
  futures, STT 0.02% and 0.05%
                     futures charges at both sell-side STT rates in question
                     (0.02% before the 2026 Budget, 0.05% after, as I
                     understand it; check Zerodha's charges page).

Assumptions, all stated in the report:
  * Intraday, a stock future moves rupee for rupee with the share. The basis
    (future minus spot) barely moves within a day, so the share price path
    stands in for the future's. No futures history is needed.
  * Slippage per leg is the trade's own cash tier from the log. Futures in
    liquid names usually trade one or two ticks wide, similar to the shares;
    the report also prints the slippage at which each strategy breaks even.
  * Lot sizes are today's, from Kite. NSE revises them a few times a year.
  * Margin is taken as MARGIN_PCT of contract value, only to size the capital
    a one-lot-per-trade version would have needed. Real SPAN + exposure
    margin varies by stock.

Research only. Nothing here trades.

    python futures_reprice.py                      latest comparison
    python futures_reprice.py --strategy orb15     detail for another strategy
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import calendar_data
import orb_backtest as ob
import stats
from kite_data import kite_symbol

ROOT = Path(__file__).resolve().parent

# Zerodha F&O (futures) charges. Brokerage and GST match the equity model.
FUT_BROKERAGE_PCT = 0.0003       # 0.03% per order...
FUT_BROKERAGE_CAP = 20           # ...or Rs 20, whichever is lower
FUT_EXCH_PCT = 0.0000173         # NSE futures transaction charge, both legs
FUT_SEBI_PCT = 0.000001          # Rs 10 per crore, both legs
FUT_STAMP_BUY_PCT = 0.00002      # buy leg only
FUT_GST_PCT = 0.18               # on brokerage + exchange + SEBI
STT_SCENARIOS = (("fut STT 0.02%", 0.0002), ("fut STT 0.05%", 0.0005))
MARGIN_PCT = 0.20                # assumed; only sizes the capital estimate


def futures_charges(buy_val, sell_val, stt_pct):
    """Round-trip statutory + broker charges on one futures trade, in rupees."""
    brok = (min(FUT_BROKERAGE_CAP, buy_val * FUT_BROKERAGE_PCT)
            + min(FUT_BROKERAGE_CAP, sell_val * FUT_BROKERAGE_PCT))
    exch = (buy_val + sell_val) * FUT_EXCH_PCT
    sebi = (buy_val + sell_val) * FUT_SEBI_PCT
    stt = sell_val * stt_pct
    stamp = buy_val * FUT_STAMP_BUY_PCT
    gst = (brok + exch + sebi) * FUT_GST_PCT
    return brok + exch + sebi + stt + stamp + gst


def _slip_pct(row):
    """Per-leg slippage as a fraction: the log's own tier, else the model's."""
    v = row.get("slip_pct")
    if v is not None and np.isfinite(v):
        return float(v) / 100
    return ob.slippage_for(row.get("turnover_cr") if pd.notna(row.get("turnover_cr")) else None)


def reprice(trades, lots):
    """One row per trade in a stock with futures, sized at one lot.

    Columns: lot, notional, gross, turnover (both legs), slip, cash_cost and
    one cost column per STT scenario. Trades in stocks without futures are
    dropped; the caller reports how many.
    """
    rows = []
    for r in trades.to_dict("records"):
        lot = lots.get(kite_symbol(r["symbol"]))
        if not lot or pd.isna(r.get("entry")) or pd.isna(r.get("exit")):
            continue
        entry, exit_px = float(r["entry"]), float(r["exit"])
        long = r["side"] == "LONG"
        gross = ((exit_px - entry) if long else (entry - exit_px)) * lot
        buy_val, sell_val = ((entry * lot, exit_px * lot) if long
                             else (exit_px * lot, entry * lot))
        slip = (buy_val + sell_val) * _slip_pct(r)
        out = dict(date=r["date"], strategy=r["strategy"], symbol=r["symbol"], side=r["side"],
                   lot=lot, notional=entry * lot, gross=gross,
                   turnover=buy_val + sell_val, slip=slip,
                   cash_charges=ob.charges(buy_val, sell_val))
        for name, stt in STT_SCENARIOS:
            out[name] = futures_charges(buy_val, sell_val, stt)
        rows.append(out)
    return pd.DataFrame(rows)


def breakeven_slip(fr, charges_col):
    """Per-leg slippage (fraction) at which the strategy's total net is zero
    under these charges. Negative means it loses even with free fills."""
    turnover = fr["turnover"].sum()
    if not turnover:
        return float("nan")
    return (fr["gross"].sum() - fr[charges_col].sum()) / turnover


def scenario_rows(fr, sessions):
    """Per cost scenario: costs and net per trade, total net and its
    session-clustered 95% interval, and the break-even slippage."""
    out = []
    for label, col in [("cash at lot size", "cash_charges")] + [(n, n) for n, _ in STT_SCENARIOS]:
        net = fr["gross"] - fr[col] - fr["slip"]
        lo, hi = stats.session_total_ci(fr.assign(net=net), "net", sessions=sessions)
        out.append(dict(scenario=label, cost_tr=(fr[col] + fr["slip"]).mean(),
                        net_tr=net.mean(), net=net.sum(), lo=lo, hi=hi,
                        cost_bps=(fr[col] + fr["slip"]).sum() / fr["notional"].sum() * 1e4,
                        be_slip=breakeven_slip(fr, col)))
    return out


def capital_needed(fr, margin_pct=MARGIN_PCT):
    """Margin for all of a session's positions at once, one lot each. ORB
    positions are almost all open together from late morning to 15:15, so
    summing a day's trades is close to the real peak."""
    if fr.empty:
        return float("nan"), float("nan")
    per_day = fr.groupby("date")["notional"].sum() * margin_pct
    return per_day.median(), per_day.max()


def worst_run(daily):
    """Worst single day and the deepest peak-to-trough drawdown of a daily
    P&L series, in rupees (both <= 0)."""
    if daily.empty:
        return 0.0, 0.0
    equity = daily.cumsum()
    dd = equity - equity.cummax().clip(lower=0)
    return float(min(0.0, daily.min())), float(min(0.0, dd.min()))


def _rs(x):
    return f"-Rs{abs(x):,.0f}" if x < 0 else f"Rs{x:,.0f}"


def _r(x, w=9):
    return f"{'-':>{w}}" if x is None or not np.isfinite(x) else f"{x:>{w},.0f}"


def print_report(log, trades, lots, focus):
    sessions = sorted(trades["date"].unique())
    strategies = sorted(trades["strategy"].unique(), key=lambda s: (s != "orb45", s))
    width = 100
    print("=" * width)
    print(f"FUTURES RE-PRICING   {log['label']} | {len(sessions)} sessions | one lot per trade, "
          f"same entries and exits")
    print("=" * width)
    print(f"Lot sizes: today's, for {len(lots)} stocks with futures (Kite NFO). "
          "Futures assumed to move rupee for rupee with the share intraday.")
    print("Slippage per leg: each trade's cash tier from the log. 'cash at lot size' is the same")
    print("trade in shares at the same rupee size, so only the instrument differs.")

    frames = {}
    for name in strategies:
        mine = trades[trades["strategy"] == name]
        fr = reprice(mine, lots)
        frames[name] = fr
        print(f"\n{name}: {len(fr)} of {len(mine)} trades in stocks with futures"
              + (f" | median contract Rs{fr['notional'].median():,.0f}"
                 f" | gross Rs{fr['gross'].mean():,.0f}/trade"
                 f" ({fr['gross'].sum() / fr['notional'].sum() * 1e4:.1f} bps)" if len(fr) else ""))
        if fr.empty:
            continue
        print(f"  {'scenario':<18}{'cost/tr':>9}{'cost bps':>9}{'net/tr':>9}{'net':>11}"
              f"   95% CI on net (by session)    break-even slippage/leg")
        for s in scenario_rows(fr, sessions):
            ci = f"{_r(s['lo'], 10)} .. {_r(s['hi'], 10)}"
            mark = ("  >0" if s["lo"] is not None and s["lo"] > 0
                    else "  <0" if s["hi"] is not None and s["hi"] < 0 else "  ~0")
            be = s["be_slip"] * 100
            be_txt = f"{be:.3f}%" if be > 0 else "loses even with free fills"
            print(f"  {s['scenario']:<18}{_r(s['cost_tr'])}{s['cost_bps']:>9.1f}{_r(s['net_tr'])}"
                  f"{_r(s['net'], 11)}   {ci}{mark}    {be_txt}")
    print("  >0 / <0: the 95% interval sits wholly above / below zero. ~0: indistinguishable from zero.")

    fr = frames.get(focus)
    if fr is None or fr.empty:
        return
    med, peak = capital_needed(fr)
    worst_col = STT_SCENARIOS[-1][0]
    daily = (fr.assign(net=fr["gross"] - fr[worst_col] - fr["slip"])
               .groupby("date")["net"].sum().reindex(sessions, fill_value=0.0))
    worst_day, max_dd = worst_run(daily)
    print(f"\nWhat {focus} at one lot per trade would have taken (STT 0.05%):")
    print(f"  margin at an assumed {MARGIN_PCT:.0%} of contract value: median day {_rs(med)}, "
          f"busiest day {_rs(peak)}")
    print(f"  worst single day {_rs(worst_day)} | deepest drawdown {_rs(max_dd)}")
    lots_1l = int(100000 // (fr['notional'].median() * MARGIN_PCT)) if len(fr) else 0
    print(f"  Rs1,00,000 covers about {lots_1l} median-sized lot(s) at a time, against the "
          f"{fr.groupby('date').size().median():.0f} positions a typical day opened here.")
    print("\nResearch only. No orders placed, no config changed.")


def run(log_id=None, focus="orb45", market_data=None, root=ROOT):
    from regime_report import pick_log
    log = pick_log(log_id, root)
    trades = calendar_data.load_trades(log)
    if trades.empty:
        raise SystemExit(f"{log['label']} has no trades.")
    missing = [c for c in ("symbol", "side", "entry", "exit") if c not in trades.columns]
    if missing:
        raise SystemExit(f"{log['label']} lacks {', '.join(missing)}; re-pricing needs them.")
    if market_data is None:
        from kite_data import KiteMarketData
        market_data = KiteMarketData()
    print_report(log, trades, market_data.futures_lots(), focus)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", help="trade log id (compare, orb, archive:<file>); default: compare")
    ap.add_argument("--strategy", default="orb45", help="strategy for the capital section")
    args = ap.parse_args(argv)
    run(args.log, args.strategy)


if __name__ == "__main__":
    main()
