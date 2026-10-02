"""
Did the strategies do better or worse in particular market conditions?

Splits a trade log's sessions by what the market was doing and reports each
group's results with session-clustered confidence intervals:

  1. Pre-stated test (the only verdict in this report): are sessions that open
     after India VIX closed at VIX_SPLIT or higher better than the rest? The
     previous close is known before the open, so if the answer were yes it
     could become a real filter. Stated before any run; not tuned.
  2. By previous-day VIX bucket.         Known before the open.
  3. By Nifty day type (up, down, sideways, open to close).
  4. By Nifty day range (high to low).   3 and 4 are known only after the close:
                                         they describe, they cannot filter.
  5. By month, with Nifty's move and the average VIX for context.

Only the pre-stated test gets a verdict. Every other table is description: with
four or five groups per table and seven strategies, some group will look
special by chance. A pattern found here is a lead to state in advance and check
on sessions that have not happened yet, not a result.

Market data is Nifty 50 and India VIX daily candles from Kite. Research only;
nothing here trades.

    python regime_report.py                      latest comparison, orb45 in detail
    python regime_report.py --strategy ema
    python regime_report.py --log archive:strategy_trades_2026-10-02_072231.csv
"""

import argparse
import datetime as dt
import math
from pathlib import Path

import pandas as pd

import calendar_data
import stats

ROOT = Path(__file__).resolve().parent

NIFTY = "NIFTY 50"
VIX = "INDIA VIX"

# Stated before any run. Do not tune these on the report's output.
VIX_SPLIT = 15.0        # the pre-stated test: previous VIX close >= this
VIX_BUCKETS = ((0.0, 12.0, "< 12"), (12.0, 15.0, "12-15"),
               (15.0, 20.0, "15-20"), (20.0, math.inf, ">= 20"))
TREND_PCT = 0.5         # Nifty open-to-close move (%) that makes an up/down day
RANGE_BUCKETS = ((0.0, 0.6, "< 0.6%"), (0.6, 1.2, "0.6-1.2%"),
                 (1.2, math.inf, ">= 1.2%"))
UNKNOWN = "unknown"
PAD_DAYS = 15           # calendar days before the first session, for its previous VIX


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------

def _daily(md, symbol, start, end):
    frame = md.candles(symbol, "day", start, end)
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["Datetime"]).dt.date.astype(str)
    return frame.drop_duplicates("date", keep="last").set_index("date").sort_index()


def market_days(md, first, last):
    """Per Nifty session, as a frame indexed by ISO date: Nifty open, high,
    low, close and the PREVIOUS session's India VIX close.

    The previous VIX is the last VIX close strictly before the date, so it is
    known before that session opens.
    """
    start = dt.date.fromisoformat(first) - dt.timedelta(days=PAD_DAYS)
    end = dt.date.fromisoformat(last)
    nifty = _daily(md, NIFTY, start, end)
    vix = _daily(md, VIX, start, end)
    out = nifty[["Open", "High", "Low", "Close"]].rename(columns=str.lower)
    if vix.empty or out.empty:
        out["vix_prev"] = float("nan")
        return out
    left = pd.DataFrame({"d": pd.to_datetime(out.index)})
    right = pd.DataFrame({"d": pd.to_datetime(vix.index), "vix_prev": vix["Close"].astype(float).values})
    merged = pd.merge_asof(left.sort_values("d"), right.sort_values("d"), on="d",
                           allow_exact_matches=False)
    out["vix_prev"] = merged["vix_prev"].values
    return out


def _bucket(value, buckets):
    if value is None or not math.isfinite(value):
        return UNKNOWN
    for lo, hi, name in buckets:
        if lo <= value < hi:
            return name
    return UNKNOWN


def classify(mkt):
    """Adds move_pct, range_pct, day_type, range_bucket and vix_bucket."""
    m = mkt.copy()
    # Rounded so a move of exactly 0.6% lands on the edge it is written as,
    # not a float hair below it.
    m["move_pct"] = ((m["close"] - m["open"]) / m["open"] * 100).round(6)
    m["range_pct"] = ((m["high"] - m["low"]) / m["open"] * 100).round(6)
    m["day_type"] = ["up" if x >= TREND_PCT else "down" if x <= -TREND_PCT else "sideways"
                     for x in m["move_pct"]]
    m["range_bucket"] = [_bucket(x, RANGE_BUCKETS) for x in m["range_pct"]]
    m["vix_bucket"] = [_bucket(x, VIX_BUCKETS) for x in m["vix_prev"]]
    return m


# --------------------------------------------------------------------------
# Sessions and groups
# --------------------------------------------------------------------------

def sessions_of(trades):
    """Every session in the log. A strategy that did not trade on one of
    these days had a real zero, and it counts."""
    return sorted(trades["date"].unique())


def session_frame(trades, strategy, sessions, mkt):
    """One row per session: trades, gross, pnl for `strategy`, plus market
    columns. Sessions without market data get 'unknown' groups."""
    mine = trades[trades["strategy"] == strategy]
    g = mine.groupby("date")
    s = pd.DataFrame({"trades": g.size(), "gross": g["gross"].sum(), "pnl": g["pnl"].sum()})
    s = s.reindex(sessions, fill_value=0).rename_axis("date")
    s["trades"] = s["trades"].astype(int)
    cols = ["move_pct", "range_pct", "day_type", "range_bucket", "vix_bucket", "vix_prev", "open", "close"]
    s = s.join(mkt[cols], how="left")
    for c in ("day_type", "range_bucket", "vix_bucket"):
        s[c] = s[c].fillna(UNKNOWN)
    s["month"] = [d[:7] for d in s.index]
    return s


def group_table(sess, key, order=None):
    """Per group: sessions, trades, gross and net per trade, net total and the
    mean net per session with its 95% interval (sessions as the draws)."""
    names = order or sorted(sess[key].unique())
    rows = []
    for name in list(names) + ([UNKNOWN] if order and UNKNOWN not in names else []):
        b = sess[sess[key] == name]
        if b.empty:
            continue
        n_tr = int(b["trades"].sum())
        mean, lo, hi = stats.mean_ci(b["pnl"])
        rows.append(dict(group=name, sessions=len(b), trades=n_tr,
                         gross_tr=b["gross"].sum() / n_tr if n_tr else float("nan"),
                         net_tr=b["pnl"].sum() / n_tr if n_tr else float("nan"),
                         net=b["pnl"].sum(), per_session=mean, lo=lo, hi=hi))
    return pd.DataFrame(rows)


def stretches(flags):
    """Number of separate runs of True in date order: how many distinct
    episodes a group of sessions really is."""
    runs, prev = 0, False
    for f in flags:
        if f and not prev:
            runs += 1
        prev = bool(f)
    return runs


def vix_test(sess, split=VIX_SPLIT):
    """The pre-stated test: mean net per session after a VIX close >= split,
    minus the same after a close below it. Sessions with no VIX are left out."""
    known = sess[sess["vix_prev"].notna()]
    high = known[known["vix_prev"] >= split]
    low = known[known["vix_prev"] < split]
    d, lo, hi = stats.diff_ci(high["pnl"], low["pnl"])
    return dict(high_n=len(high), low_n=len(low),
                high_mean=high["pnl"].mean() if len(high) else float("nan"),
                low_mean=low["pnl"].mean() if len(low) else float("nan"),
                high_gross_tr=high["gross"].sum() / max(1, high["trades"].sum()),
                low_gross_tr=low["gross"].sum() / max(1, low["trades"].sum()),
                diff=d, lo=lo, hi=hi,
                stretches=stretches(known["vix_prev"] >= split))


def verdict(lo, hi):
    if lo is None or hi is None:
        return "too few sessions to tell"
    if lo > 0:
        return "BETTER after high VIX"
    if hi < 0:
        return "WORSE after high VIX"
    return "not distinguishable"


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _r(x, width=9):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return f"{'-':>{width}}"
    return f"{x:>{width},.0f}"


def _r1(x, width=7):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return f"{'-':>{width}}"
    return f"{x:>{width}.1f}"


def print_groups(title, note, table):
    print(f"\n{title}")
    if note:
        print(f"  {note}")
    print(f"  {'group':<10}{'sessions':>9}{'trades':>8}{'gross/tr':>10}{'net/tr':>8}"
          f"{'net':>10}{'net/session':>13}   95% CI per session")
    for r in table.itertuples():
        ci = f"{_r(r.lo, 7)} .. {_r(r.hi, 7)}" if r.lo is not None and math.isfinite(r.lo) else "      -"
        print(f"  {r.group:<10}{r.sessions:>9}{r.trades:>8}{_r1(r.gross_tr, 10)}{_r1(r.net_tr, 8)}"
              f"{_r(r.net, 10)}{_r(r.per_session, 13)}   {ci}")


def month_table(sess):
    rows = []
    for month, b in sess.groupby("month", sort=True):
        known = b.dropna(subset=["open", "close"])
        nifty = ((known["close"].iloc[-1] / known["open"].iloc[0] - 1) * 100
                 if len(known) else float("nan"))
        rows.append(dict(month=month, sessions=len(b), trades=int(b["trades"].sum()),
                         net=b["pnl"].sum(), up=int((b["pnl"] > 0).sum()),
                         down=int((b["pnl"] < 0).sum()), nifty=nifty,
                         vix=b["vix_prev"].mean()))
    return pd.DataFrame(rows)


def print_report(log, trades, mkt, focus):
    sessions = sessions_of(trades)
    strategies = sorted(trades["strategy"].unique(),
                        key=lambda s: (s != "orb45", s))
    width = 94
    print("=" * width)
    print(f"MARKET REGIME REPORT   {log['label']} | {len(sessions)} sessions | "
          f"{sessions[0]} to {sessions[-1]}")
    print("=" * width)
    missing = [d for d in sessions if d not in mkt.index]
    print(f"Market data: Nifty 50 and India VIX daily from Kite"
          + (f"; {len(missing)} session(s) without it are 'unknown'." if missing else "."))

    print(f"\n1. Pre-stated test: do sessions after a high-VIX close do better?")
    print(f"   Stated before any run: previous India VIX close >= {VIX_SPLIT:g} versus below it,")
    print(f"   compared on mean net P&L per session. The previous close is known before the open.")
    first = None
    print(f"   {'strategy':<11}{'high n':>7}{'low n':>7}{'net/sess hi':>13}{'net/sess lo':>13}"
          f"{'difference':>12}   95% CI              verdict")
    for name in strategies:
        sess = session_frame(trades, name, sessions, mkt)
        t = vix_test(sess)
        first = first or t
        ci = (f"{_r(t['lo'], 7)} .. {_r(t['hi'], 7)}" if t["lo"] is not None else f"{'-':>18}")
        print(f"   {name:<11}{t['high_n']:>7}{t['low_n']:>7}{_r(t['high_mean'], 13)}"
              f"{_r(t['low_mean'], 13)}{_r(t['diff'], 12)}   {ci:<19} {verdict(t['lo'], t['hi'])}")
    if first:
        print(f"   The high-VIX sessions fall in {first['stretches']} separate stretch(es). "
              "Few stretches means few independent")
        print("   market episodes: even a clear difference here may be one episode, not a pattern.")
        print("   Net per session also moves with how many trades a strategy took, so compare")
        print("   gross per trade as well, in the tables below.")

    if focus not in strategies:
        return
    sess = session_frame(trades, focus, sessions, mkt)
    print(f"\nDetail for {focus}. Description only: no verdicts below this line.")
    print_groups("2. By previous-day India VIX (known before the open)", None,
                 group_table(sess, "vix_bucket", [b[2] for b in VIX_BUCKETS]))
    print_groups(f"3. By Nifty day type, open to close (up >= +{TREND_PCT:g}%, down <= -{TREND_PCT:g}%)",
                 "Known only after the close: this describes, it cannot be a filter.",
                 group_table(sess, "day_type", ["up", "sideways", "down"]))
    print_groups("4. By Nifty day range, high to low as % of the open",
                 "Known only after the close. A breakout needs room to run.",
                 group_table(sess, "range_bucket", [b[2] for b in RANGE_BUCKETS]))

    print("\n5. By month")
    print(f"  {'month':<9}{'sessions':>9}{'trades':>8}{'net':>10}{'up':>5}{'down':>6}"
          f"{'Nifty':>9}{'avg VIX':>9}")
    for r in month_table(sess).itertuples():
        nifty = f"{r.nifty:+.1f}%" if math.isfinite(r.nifty) else "-"
        vix = f"{r.vix:.1f}" if math.isfinite(r.vix) else "-"
        print(f"  {r.month:<9}{r.sessions:>9}{r.trades:>8}{_r(r.net, 10)}{r.up:>5}{r.down:>6}"
              f"{nifty:>9}{vix:>9}")
    print("  Nifty is the move from the month's first open to its last close over these")
    print("  sessions; avg VIX is the mean previous-day close.")
    print("\nResearch only. No orders placed, no config changed.")


def pick_log(log_id=None, root=ROOT):
    logs = calendar_data.available_logs(root)
    if not logs:
        raise SystemExit("No trade logs found. Run .\\run-backtest.ps1 -Compare first.")
    if log_id is None:
        preferred = [l for l in logs if l["id"] == "compare"]
        return preferred[0] if preferred else logs[0]
    for l in logs:
        if l["id"] == log_id:
            return l
    raise SystemExit(f"Unknown log {log_id!r}. Available: {', '.join(l['id'] for l in logs)}")


def run(log_id=None, focus="orb45", market_data=None, root=ROOT):
    log = pick_log(log_id, root)
    trades = calendar_data.load_trades(log)
    if trades.empty:
        raise SystemExit(f"{log['label']} has no trades.")
    sessions = sessions_of(trades)
    if market_data is None:
        from kite_data import KiteMarketData
        market_data = KiteMarketData()
    mkt = classify(market_days(market_data, sessions[0], sessions[-1]))
    print_report(log, trades, mkt, focus)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", help="trade log id, as the P&L calendar lists them "
                                  "(compare, orb, paper, archive:<file>); default: compare")
    ap.add_argument("--strategy", default="orb45", help="strategy shown in detail (default orb45)")
    args = ap.parse_args(argv)
    run(args.log, args.strategy)


if __name__ == "__main__":
    main()
