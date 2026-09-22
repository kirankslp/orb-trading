"""
Paper broker: a point-in-time trade journal for forward-testing the ORB plan.

    python paper_broker.py plan       # 10:05 IST, after the opening range closes
    python paper_broker.py resolve    # 15:45 IST, after the close
    python paper_broker.py status

Why this exists rather than just running the backtest again
-----------------------------------------------------------
A backtest computes the plan and the outcome from the same dataframe, so any
lookahead in the code is invisible: the levels already "know" how the day went.
Forward paper trading is immune to that, but ONLY if the plan is committed
before the outcome exists. So `plan` writes an immutable file at 10:05 and
`resolve` is forbidden from recomputing levels: it replays the session against
the levels exactly as they were recorded that morning.

That single rule is the whole point of the module. Everything else here is
bookkeeping in service of it:

  paper/plans/YYYY-MM-DD.json   written once, never rewritten
  paper/ledger.csv              append-only, one row per resolved trade

Exit accounting delegates to orb_backtest._resolve_exit and _pnl so the paper
ledger and the backtest agree on costs to the rupee. If they disagree, the
difference is real signal about the data, not an artifact of two cost models.

No orders are placed. Nothing here talks to a broker.
"""

import argparse
import datetime
import hashlib
import json
import os

import pandas as pd

import daily_plan as dp
import orb_backtest as ob

PAPER_DIR = os.environ.get("PAPER_DIR", "paper")
PLANS_DIR = os.path.join(PAPER_DIR, "plans")
LEDGER = os.path.join(PAPER_DIR, "ledger.csv")

# Starting capital for the equity curve. The ledger stores P&L per trade; this
# is only the base the report adds it to.
STARTING_CAPITAL = 100000.0

# Every parameter that changes what a trade is. The plan records the hash so a
# mid-run config edit is detectable after the fact instead of quietly splitting
# the sample into two different strategies.
FROZEN_PARAMS = (
    "DAY_BUDGET", "LEVERAGE", "MAX_POSITIONS", "OR_MINUTES", "SQUAREOFF_TIME",
    "STOP_MODE", "ATR_STOP_MULT", "ATR_TARGET_MULT", "ATR_BOUNDS",
    "SL_PCT", "TARGET_PCT", "INTERVAL", "ENTRY_BAR_POLICY",
    "BROKERAGE_PCT", "BROKERAGE_CAP", "STT_SELL_PCT", "EXCH_TXN_PCT",
    "SEBI_PCT", "STAMP_BUY_PCT", "GST_PCT", "SLIPPAGE_TIERS",
)


class PlanExists(Exception):
    """A plan for this date is already committed."""


class PlanMissing(Exception):
    """Nothing was planned for this date."""


def config_fingerprint():
    """Hash of the frozen strategy parameters, plus the values behind it."""
    values = {}
    for name in FROZEN_PARAMS:
        v = getattr(ob, name)
        values[name] = list(v) if isinstance(v, (tuple, list)) else v
    blob = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16], values


def plan_path(day):
    return os.path.join(PLANS_DIR, f"{day}.json")


def _now_ist():
    return pd.Timestamp.now(tz=dp.IST).isoformat()


# --------------------------------------------------------------------------
# 10:05 — commit the plan
# --------------------------------------------------------------------------

def write_plan(day=None, market_data=None, force=False):
    """Commit today's levels. Refuses to overwrite an existing plan.

    Overwriting is what would silently reintroduce lookahead: a plan rewritten
    at 15:00 with the benefit of the day's price action is no longer a
    prediction. `force` exists only for reruns of a plan that failed to produce
    any rows at all, and it records that it happened.
    """
    day = day or dp.today_ist()
    path = plan_path(day)
    if os.path.exists(path) and not force:
        raise PlanExists(
            f"{path} already exists. A committed plan is never rewritten; that "
            f"is what keeps the forward test free of lookahead.")

    fingerprint, values = config_fingerprint()
    budget, n_or = ob.slot_budget(), ob.or_candles()

    # Ranked on closes through YESTERDAY, so the picks and the ATR/turnover
    # behind them are all strictly point-in-time. Fetched once: a second call
    # could return different numbers and the plan would no longer describe the
    # thing that was actually decided.
    syms, liq = dp.todays_watchlist(market_data=market_data)
    rows, pending = [], []
    liquidity = {}

    if syms:
        intraday = ob.load_many(syms, market_data=market_data)
        for s in syms:
            df = intraday.get(s)
            if df is None or df[df["date"] == day].empty:
                pending.append(f"{s}: no bars for {day} (holiday, halt, or feed lag)")
                continue
            r = dp.opening_range(df, day, n_or)
            if r is None:
                pending.append(f"{s}: range still forming, need {n_or} candles")
                continue
            hi, lo, _ = r
            m = liq.get(s) or {}
            atr_pct, turnover_cr = m.get("atr_pct"), m.get("turnover_cr")
            sl_pct, tgt_pct = ob.levels_for(atr_pct)
            qty_long, qty_short = int(budget // hi), int(budget // lo)
            if not qty_long and not qty_short:
                pending.append(f"{s}: one share costs more than the slot")
                continue
            liquidity[s] = turnover_cr
            # The plan stores the opening range and both distances, never the
            # resolved side. Which way it breaks is information the 10:05 run
            # does not have and must not pretend to.
            rows.append(dict(symbol=s, or_high=round(hi, 2), or_low=round(lo, 2),
                             sl_pct=sl_pct, tgt_pct=tgt_pct, atr_pct=atr_pct,
                             turnover_cr=turnover_cr,
                             qty_long=qty_long, qty_short=qty_short))
    else:
        pending.append("screener returned nothing affordable")

    plan = {
        "date": str(day),
        "generated_at": _now_ist(),
        "config_fingerprint": fingerprint,
        "config": values,
        "slot_budget": budget,
        "day_budget": ob.DAY_BUDGET,
        "max_positions": ob.MAX_POSITIONS,
        "watchlist": syms,
        "liquidity": liquidity,
        "rows": sorted(rows, key=lambda r: r["symbol"]),
        "note": "; ".join(pending) if pending else None,
        "rewritten": bool(force and os.path.exists(path)),
    }

    os.makedirs(PLANS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2, default=str)
    return plan


def read_plan(day):
    path = plan_path(day)
    if not os.path.exists(path):
        raise PlanMissing(f"No plan at {path}. Nothing was committed for {day}.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# 15:45 — resolve against what actually happened
# --------------------------------------------------------------------------

def _replay(day, g, row, n_or, turnover_cr):
    """Replay one symbol against levels fixed at plan time.

    Mirrors ob.trade_day's sequencing, but the opening range and both distances
    are READ from the plan instead of recomputed from the bars. Same exit
    primitives, so costs match the backtest exactly.
    """
    g = g.sort_values("dt").reset_index(drop=True)
    if len(g) <= n_or:
        return None

    or_high, or_low = row.get("or_high"), row.get("or_low")
    sl_pct, tgt_pct = row["sl_pct"], row["tgt_pct"]

    side = entry = sl = tgt = entry_time = qty = None
    planned_trigger = None

    for i in range(n_or, len(g)):
        r = g.iloc[i]
        o, h, l, c, t = r["Open"], r["High"], r["Low"], r["Close"], r["time"]

        if side is None:
            if t >= ob.SQUAREOFF_TIME:
                return None
            if or_high is not None and h > or_high:
                side, entry_time, planned_trigger = "LONG", t, or_high
                entry = max(or_high, o)
                sl, tgt = entry * (1 - sl_pct), entry * (1 + tgt_pct)
                qty = row.get("qty_long")
            elif or_low is not None and l < or_low:
                side, entry_time, planned_trigger = "SHORT", t, or_low
                entry = min(or_low, o)
                sl, tgt = entry * (1 + sl_pct), entry * (1 - tgt_pct)
                qty = row.get("qty_short")
            else:
                continue
            if not qty:
                return None

            if ob.ENTRY_BAR_POLICY != "skip":
                hit = ob._resolve_exit(side, sl, tgt, h, l,
                                       allow_stop=ob.ENTRY_BAR_POLICY == "conservative")
                if hit:
                    px, reason, amb = hit
                    return _row(day, side, entry, px, entry_time, t, reason, amb,
                                row["symbol"], qty, turnover_cr, sl_pct, tgt_pct,
                                planned_trigger)
            continue

        if t >= ob.SQUAREOFF_TIME:
            return _row(day, side, entry, c, entry_time, t, "squareoff", False,
                        row["symbol"], qty, turnover_cr, sl_pct, tgt_pct, planned_trigger)
        hit = ob._resolve_exit(side, sl, tgt, h, l)
        if hit:
            px, reason, amb = hit
            return _row(day, side, entry, px, entry_time, t, reason, amb,
                        row["symbol"], qty, turnover_cr, sl_pct, tgt_pct, planned_trigger)

    if side is not None:
        last = g.iloc[-1]
        return _row(day, side, entry, last["Close"], entry_time, last["time"],
                    "eod", False, row["symbol"], qty, turnover_cr, sl_pct,
                    tgt_pct, planned_trigger)
    return None


def _row(day, side, entry, exit_px, t_in, t_out, reason, amb, symbol, qty,
         turnover_cr, sl_pct, tgt_pct, planned_trigger):
    """One ledger row: the backtest's P&L dict plus plan-versus-fill drift."""
    d = ob._pnl(day, side, entry, exit_px, t_in, t_out, reason, amb, symbol,
                qty, turnover_cr, sl_pct, tgt_pct)
    # Gap between the level we published at 10:05 and where the breakout
    # actually filled. This is the measurement the backtest cannot make, and
    # AGENT.md flags slippage as roughly half of all friction and unvalidated.
    d["planned_trigger"] = round(planned_trigger, 2)
    drift = (entry - planned_trigger) if side == "LONG" else (planned_trigger - entry)
    d["entry_drift"] = round(drift, 4)
    d["entry_drift_pct"] = round(drift / planned_trigger * 100, 4)
    return d


def resolve(day=None, market_data=None):
    """Replay a committed plan against the session's bars and append to the ledger."""
    day = day or dp.today_ist()
    plan = read_plan(day)
    if str(day) in set(_ledger_dates()):
        raise PlanExists(f"{day} is already in the ledger. It is append-only.")

    fingerprint, _ = config_fingerprint()
    rows = plan.get("rows") or []
    if not rows:
        return pd.DataFrame(), plan, fingerprint

    symbols = [r["symbol"] for r in rows]
    intraday = ob.load_many(symbols, market_data=market_data)
    n_or = ob.or_candles()

    # Turnover sets the slippage tier and has to be the value known that
    # morning, so it comes off the plan, not off today's fresh screener run.
    liq = plan.get("liquidity") or {}
    day_obj = pd.Timestamp(day).date()

    out = []
    for r in rows:
        df = intraday.get(r["symbol"])
        if df is None:
            continue
        g = df[df["date"] == day_obj]
        if g.empty:
            continue
        t = _replay(day_obj, g, r, n_or, liq.get(r["symbol"]))
        if t:
            t["plan_date"] = str(day)
            t["plan_fingerprint"] = plan.get("config_fingerprint")
            t["resolve_fingerprint"] = fingerprint
            t["resolved_at"] = _now_ist()
            out.append(t)

    tr = pd.DataFrame(out)
    if not tr.empty:
        append_ledger(tr)
    return tr, plan, fingerprint


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

def append_ledger(tr):
    os.makedirs(PAPER_DIR, exist_ok=True)
    header = not os.path.exists(LEDGER)
    tr.to_csv(LEDGER, mode="a", header=header, index=False)


def read_ledger():
    if not os.path.exists(LEDGER):
        return pd.DataFrame()
    return pd.read_csv(LEDGER)


def _ledger_dates():
    led = read_ledger()
    return [] if led.empty else led["plan_date"].astype(str).unique().tolist()


def planned_dates():
    if not os.path.isdir(PLANS_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(PLANS_DIR) if f.endswith(".json"))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("action", choices=["plan", "resolve", "status"])
    ap.add_argument("--date", help="YYYY-MM-DD, defaults to today IST")
    ap.add_argument("--force", action="store_true",
                    help="rewrite an existing plan (recorded in the file)")
    a = ap.parse_args()
    day = datetime.date.fromisoformat(a.date) if a.date else dp.today_ist()

    if a.action == "plan":
        try:
            plan = write_plan(day, force=a.force)
        except PlanExists as exc:
            raise SystemExit(str(exc))
        print(f"plan {plan['date']} committed  fingerprint {plan['config_fingerprint']}")
        print(f"  Rs{plan['day_budget']:,} over {plan['max_positions']} slots "
              f"= Rs{plan['slot_budget']:,.0f}/position")
        print(f"  {len(plan['rows'])} symbols with levels, "
              f"watchlist {len(plan['watchlist'])}")
        if plan.get("note"):
            print(f"  note: {plan['note']}")

    elif a.action == "resolve":
        try:
            tr, plan, fp = resolve(day)
        except (PlanMissing, PlanExists) as exc:
            raise SystemExit(str(exc))
        if fp != plan.get("config_fingerprint"):
            print("WARNING: config changed since this plan was written "
                  f"({plan.get('config_fingerprint')} -> {fp}). The sample now "
                  "spans two different strategies and cannot be pooled.")
        if tr.empty:
            print(f"{day}: no trades triggered.")
            return
        print(f"{day}: {len(tr)} trades resolved, "
              f"net Rs{tr.pnl.sum():,.1f} (gross Rs{tr.gross.sum():,.1f}, "
              f"costs Rs{tr.cost.sum():,.1f}) -> {LEDGER}")

    else:
        led, plans = read_ledger(), planned_dates()
        print(f"plans committed : {len(plans)}"
              + (f"  ({plans[0]} .. {plans[-1]})" if plans else ""))
        if led.empty:
            print("ledger          : empty")
            return
        unresolved = sorted(set(plans) - set(led["plan_date"].astype(str)))
        print(f"sessions in ledger: {led['plan_date'].nunique()}")
        print(f"trades            : {len(led)}")
        print(f"net P&L           : Rs{led.pnl.sum():,.1f}")
        print(f"equity            : Rs{STARTING_CAPITAL + led.pnl.sum():,.1f}")
        if unresolved:
            print(f"UNRESOLVED plans  : {', '.join(unresolved)}")
        fps = led["plan_fingerprint"].dropna().unique()
        if len(fps) > 1:
            print(f"WARNING: {len(fps)} different config fingerprints in the "
                  "ledger. The strategy was not frozen across this sample.")


if __name__ == "__main__":
    main()
