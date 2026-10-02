"""
Long-term momentum: hold last year's strongest stocks, refresh monthly.

Rules, stated before any run (do not tune them on the output):

  * At each month's last session, rank every eligible stock by its return
    from 12 months ago to 1 month ago ("12-1 momentum"; the latest month is
    skipped because very recent winners tend to give some of it back).
  * Eligible: at least a year of history, close >= Rs MIN_PRICE, and median
    daily turnover over the last 3 months >= LIQ_FLOOR. All measured with
    data up to and including that session only.
  * Hold the top TOP_N. Trade at the NEXT session's open: sell what left the
    list, buy what joined it with an equal share (portfolio value / TOP_N),
    leave continuing holdings alone. Whole shares only.
  * Delivery costs: 0.1% STT on both legs, exchange, SEBI, stamp duty on the
    buy, GST, a DP charge per stock sold, and SLIPPAGE_PCT per leg at the open.

Benchmarks:
  * Nifty 50, price index, buy and hold.
  * Equal weight of the SAME eligible universe each month, frictionless. The
    universe is today's listed stocks, so it never contains the companies that
    collapsed and were delisted along the way (survivorship bias), and that
    flatters any strategy built on it. Both the momentum portfolio and this
    benchmark carry the same bias, so momentum MINUS this benchmark is the
    cleaner measure of what momentum itself adds.

The one verdict: is the mean monthly excess return over each benchmark above
zero with a 95% interval that excludes zero? Everything else is description.

Tax (a view, not exact): realised gains taxed at 20% under a year's holding,
12.5% above Rs 1.25 lakh a year after it, short losses set against long gains
within a financial year, no carry-forward. Current rates are applied to every
year. Dividends are ignored for both the portfolio and the price index.

Research only. Nothing here trades.

    python momentum_backtest.py                 8 years, first run fetches and caches
    python momentum_backtest.py --years 10
    python momentum_backtest.py --refresh       ignore the cache
"""

import argparse
import datetime as dt
import math
from pathlib import Path

import numpy as np
import pandas as pd

import stats
import symbol_screener as sc
from kite_data import KiteDataError, KiteInstrumentError

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache" / "daily"
TRADES_FILE = ROOT / "momentum_orders.csv"
MONTHLY_FILE = ROOT / "momentum_monthly.csv"

# --- rules (stated before any run) ----------------------------------------
TOP_N = 20
LOOKBACK = 252          # sessions in "12 months"
SKIP = 21               # sessions in "the latest month", left out of the score
MIN_PRICE = 50.0
LIQ_DAYS = 63           # "3 months" for the turnover median
LIQ_FLOOR = 10e7        # Rs 10 cr median daily turnover
START_CAPITAL = 100000.0
YEARS = 8
SUSPECT_MOVE = 0.5      # a one-day move this large is almost always an
                        # unadjusted split or bonus, not a real return
MAX_FETCH_FAILURE_PCT = 0.15

# --- delivery costs (Zerodha equity delivery) ------------------------------
STT_PCT = 0.001         # 0.1% on buy AND sell
EXCH_PCT = 0.0000297    # NSE, both legs
SEBI_PCT = 0.000001
STAMP_BUY_PCT = 0.00015
GST_PCT = 0.18          # on exchange + SEBI (brokerage is zero for delivery)
DP_CHARGE = 15.34       # per stock sold per day, incl. GST; check the current figure
SLIPPAGE_PCT = 0.001    # per leg, at the opening auction

# --- tax view ---------------------------------------------------------------
STCG_RATE, LTCG_RATE, LTCG_EXEMPT, LT_DAYS = 0.20, 0.125, 125000.0, 365


# --------------------------------------------------------------------------
# Costs and tax
# --------------------------------------------------------------------------

def buy_cost(value):
    return value * (STT_PCT + EXCH_PCT + SEBI_PCT + STAMP_BUY_PCT + (EXCH_PCT + SEBI_PCT) * GST_PCT)


def sell_cost(value):
    return value * (STT_PCT + EXCH_PCT + SEBI_PCT + (EXCH_PCT + SEBI_PCT) * GST_PCT) + DP_CHARGE


BUY_COST_PCT = buy_cost(1.0)


def tax_for(short_gain, long_gain):
    """Tax on one financial year's realised gains (the simplified view)."""
    if short_gain < 0:
        long_gain += short_gain
        short_gain = 0.0
    return STCG_RATE * short_gain + LTCG_RATE * max(0.0, long_gain - LTCG_EXEMPT)


def fiscal_year(day):
    return day.year if day.month >= 4 else day.year - 1


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def _frame(candles):
    f = candles.copy()
    f["date"] = pd.to_datetime(f["Datetime"]).dt.tz_localize(None).dt.normalize()
    return f.drop_duplicates("date", keep="last").set_index("date")[["Open", "Close", "Volume"]]


def _cached(sym, start, end, cache_dir, md, refresh):
    path = Path(cache_dir) / f"{sym.replace('&', '_and_')}.csv"
    if path.is_file() and not refresh:
        f = pd.read_csv(path, parse_dates=["date"]).set_index("date")
        if len(f) and f.index[0] <= pd.Timestamp(start) + pd.Timedelta(days=10) \
                and f.index[-1] >= pd.Timestamp(end) - pd.Timedelta(days=7):
            return f
    f = _frame(md.candles(sym, "day", start, end))
    path.parent.mkdir(parents=True, exist_ok=True)
    f.reset_index().to_csv(path, index=False)
    return f


def fetch_panel(md, universe, start, end, cache_dir=CACHE_DIR, refresh=False):
    """{symbol: frame of Open, Close, Volume by date}. Symbols Kite cannot
    serve are skipped and reported; too many failures abort the run."""
    out, failures = {}, []
    for i, sym in enumerate(universe):
        if i and i % 250 == 0:
            print(f"  ...{i} of {len(universe)} symbols")
        try:
            f = _cached(sym, start, end, cache_dir, md, refresh)
        except (KiteDataError, KiteInstrumentError) as exc:
            failures.append(str(exc))
            continue
        if f.empty:
            failures.append(f"no daily candles for {sym}")
            continue
        out[sym] = f
    if failures:
        share = len(failures) / max(1, len(universe))
        if share > MAX_FETCH_FAILURE_PCT:
            raise SystemExit(f"{len(failures)} of {len(universe)} symbols failed ({share:.0%}); "
                             f"usually a dead token or network problem: {failures[0]}")
    return out, failures


def matrices(panel):
    """Aligned Open, Close and turnover (Close x Volume) frames, dates x symbols."""
    o = pd.DataFrame({s: f["Open"] for s, f in panel.items()}).sort_index()
    c = pd.DataFrame({s: f["Close"] for s, f in panel.items()}).sort_index()
    v = pd.DataFrame({s: f["Volume"] for s, f in panel.items()}).sort_index()
    return o, c, c * v


def suspect_symbols(close, threshold=SUSPECT_MOVE):
    """Symbols with any one-day move of `threshold` or more: in daily data that
    is almost always a split or bonus the prices were not adjusted for. They
    are excluded outright rather than half-trusted."""
    r = close.pct_change(fill_method=None).abs()
    return sorted(r.columns[(r >= threshold).any()])


def month_ends(index, start):
    """Last session of each month, from `start` on."""
    s = pd.Series(index, index=index)
    last = s.groupby([index.year, index.month]).max()
    return [d for d in last.sort_values() if d >= pd.Timestamp(start)]


# --------------------------------------------------------------------------
# Signal
# --------------------------------------------------------------------------

def eligible(close, turnover, day, excluded=()):
    """Symbols tradeable at `day` by the stated rules, using data up to `day`."""
    pos = close.index.get_loc(day)
    if pos < LOOKBACK:
        return []
    window = close.iloc[pos - LOOKBACK:pos + 1]
    have = window.notna().sum() >= LOOKBACK * 0.9
    price_ok = close.iloc[pos] >= MIN_PRICE
    liq = turnover.iloc[max(0, pos - LIQ_DAYS + 1):pos + 1].median() >= LIQ_FLOOR
    ok = have & price_ok & liq
    return sorted(s for s in ok.index[ok.fillna(False)] if s not in set(excluded))


def momentum_scores(close, day, symbols):
    """12-1 momentum: close a month ago over close a year ago, minus one."""
    pos = close.index.get_loc(day)
    if pos < LOOKBACK:
        return pd.Series(dtype=float)
    then = close.iloc[pos - LOOKBACK][symbols]
    recent = close.iloc[pos - SKIP][symbols]
    return (recent / then - 1).dropna()


def pick(close, turnover, day, excluded=(), n=None):
    """The top n by score; ties broken by symbol so the result is stable."""
    n = n or TOP_N
    sc_ = momentum_scores(close, day, eligible(close, turnover, day, excluded))
    if sc_.empty:
        return []
    ranked = sorted(sc_.items(), key=lambda kv: (-kv[1], kv[0]))
    return [s for s, _ in ranked[:n]]


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------

def simulate(opens, close, turnover, rebalance_days, excluded=(), capital=START_CAPITAL, n=None):
    """Run the portfolio. Returns dict(equity, trades, tax, costs)."""
    n = n or TOP_N
    cff = close.ffill()
    rebal = set(rebalance_days)
    days = close.index[close.index >= rebalance_days[0]]
    cash, held = capital, {}
    trades, equity, tax_paid = [], {}, {}
    fy_gain = {}
    pending = None
    cost_total = {"charges": 0.0, "dp": 0.0, "slippage": 0.0}

    def settle(fy):
        st, lt = fy_gain.pop(fy, (0.0, 0.0))
        tax_paid[fy] = tax_for(st, lt)

    current_fy = fiscal_year(days[0])
    for day in days:
        if fiscal_year(day) != current_fy:
            settle(current_fy)
            current_fy = fiscal_year(day)
        if pending is not None:
            target = pending
            pending = None
            for sym in sorted(set(held) - set(target)):
                px = opens.at[day, sym] if sym in opens.columns else np.nan
                if not np.isfinite(px):
                    continue                    # no price today: cannot sell, keep holding
                h = held.pop(sym)
                value = h["qty"] * px * (1 - SLIPPAGE_PCT)
                cost = sell_cost(value)
                cash += value - cost
                gain = value - cost - h["basis"]
                days_held = (day - h["bought"]).days
                st, lt = fy_gain.get(current_fy, (0.0, 0.0))
                fy_gain[current_fy] = (st + gain, lt) if days_held <= LT_DAYS else (st, lt + gain)
                cost_total["charges"] += cost - DP_CHARGE
                cost_total["dp"] += DP_CHARGE
                cost_total["slippage"] += h["qty"] * px * SLIPPAGE_PCT
                trades.append(dict(date=day.date(), symbol=sym, action="SELL", qty=h["qty"],
                                   price=round(px, 2), value=round(value, 2), cost=round(cost, 2),
                                   gain=round(gain, 2), days_held=days_held))
            value_now = cash + sum(h["qty"] * (opens.at[day, s] if np.isfinite(opens.at[day, s])
                                               else cff.at[day, s]) for s, h in held.items())
            slot = value_now / n
            for sym in [s for s in target if s not in held]:
                px = opens.at[day, sym]
                if not np.isfinite(px):
                    continue
                fill = px * (1 + SLIPPAGE_PCT)
                qty = int(min(slot, cash) // (fill * (1 + BUY_COST_PCT)))
                if qty <= 0:
                    continue
                value = qty * fill
                cost = buy_cost(value)
                cash -= value + cost
                held[sym] = dict(qty=qty, basis=value + cost, bought=day)
                cost_total["charges"] += cost
                cost_total["slippage"] += qty * px * SLIPPAGE_PCT
                trades.append(dict(date=day.date(), symbol=sym, action="BUY", qty=qty,
                                   price=round(px, 2), value=round(value, 2), cost=round(cost, 2),
                                   gain=0.0, days_held=0))
        equity[day] = cash + sum(h["qty"] * cff.at[day, s] for s, h in held.items())
        if day in rebal:
            pending = pick(close, turnover, day, excluded, n)
    settle(current_fy)
    return dict(equity=pd.Series(equity), trades=pd.DataFrame(trades), tax=tax_paid,
                costs=cost_total, held=held, cash=cash)


def equal_weight_monthly(close, turnover, rebalance_days, excluded=()):
    """Frictionless equal weight of the eligible universe, month to month."""
    cff = close.ffill()
    out = {}
    for d0, d1 in zip(rebalance_days[:-1], rebalance_days[1:]):
        syms = eligible(close, turnover, d0, excluded)
        if not syms:
            continue
        r = (cff.loc[d1, syms] / close.loc[d0, syms] - 1).dropna()
        if len(r):
            out[d1] = float(r.mean())
    return pd.Series(out)


def monthly_from_levels(levels, rebalance_days):
    lv = levels.reindex(rebalance_days).ffill()
    return lv.pct_change().dropna()


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def summary(monthly):
    if monthly.empty:
        return dict(cagr=np.nan, vol=np.nan, mdd=np.nan, growth=np.nan)
    growth = float((1 + monthly).prod())
    years = len(monthly) / 12
    curve = (1 + monthly).cumprod()
    mdd = float((curve / curve.cummax().clip(lower=1.0) - 1).min())
    return dict(cagr=growth ** (1 / years) - 1, vol=float(monthly.std(ddof=1) * math.sqrt(12)),
                mdd=min(0.0, mdd), growth=growth)


def calendar_years(series_by_name):
    years = sorted({d.year for s in series_by_name.values() for d in s.index})
    rows = []
    for y in years:
        row = {"year": y}
        for name, s in series_by_name.items():
            m = s[[d.year == y for d in s.index]]
            row[name] = float((1 + m).prod() - 1) if len(m) else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def verdict(lo, hi, name):
    if lo is None or hi is None:
        return "too few months to tell"
    if lo > 0:
        return f"BEATS {name}"
    if hi < 0:
        return f"TRAILS {name}"
    return "not distinguishable"


def _pct(x, w=8, signed=False):
    if x is None or not np.isfinite(x):
        return f"{'-':>{w}}"
    return f"{x * 100:>+{w}.1f}%" if signed else f"{x * 100:>{w}.1f}%"


def print_report(result, mom, ew, nifty, info):
    width = 92
    print("=" * width)
    print(f"MOMENTUM BACKTEST   12-1 momentum, top {TOP_N}, monthly | "
          f"{mom.index[0]:%Y-%m} to {mom.index[-1]:%Y-%m} | Rs{START_CAPITAL:,.0f} start")
    print("=" * width)
    print(f"Universe: {info['universe']} symbols listed TODAY | {info['skipped']} without Kite data | "
          f"{info['suspect']} excluded (one-day move >= {SUSPECT_MOVE:.0%}, likely unadjusted split)")
    print(f"Eligible per month: median {info['eligible_median']:.0f} "
          f"(>= {LOOKBACK} sessions, close >= Rs{MIN_PRICE:.0f}, median turnover >= Rs{LIQ_FLOOR / 1e7:.0f} cr)")
    print("SURVIVORSHIP: delisted companies are missing from this universe, which flatters both")
    print("the portfolio and the equal-weight benchmark. Read momentum against the equal-weight")
    print("line, not against zero.")

    rows = [("momentum, after costs", summary(mom)),
            ("equal-weight universe (no costs)", summary(ew)),
            ("Nifty 50 price index", summary(nifty))]
    print(f"\n{'':<34}{'CAGR':>9}{'volatility':>12}{'worst fall':>12}{'Rs1L became':>14}")
    for name, s in rows:
        grown = f"Rs{START_CAPITAL * s['growth']:,.0f}" if np.isfinite(s["growth"]) else "-"
        print(f"{name:<34}{_pct(s['cagr'], 8)}{_pct(s['vol'], 11)}{_pct(s['mdd'], 11)}{grown:>14}")

    print("\nPre-stated test: mean monthly excess return, 95% interval (months as draws)")
    for label, bench in (("equal-weight universe", ew), ("Nifty 50", nifty)):
        x = (mom - bench).dropna()
        m, lo, hi = stats.mean_ci(x)
        beat = (x > 0).mean() if len(x) else np.nan
        print(f"  vs {label:<23}{_pct(m, 7, True)} a month   "
              f"{_pct(lo, 6, True)} .. {_pct(hi, 6, True)}   ahead in {_pct(beat, 4)} of "
              f"{len(x)} months   {verdict(lo, hi, label)}")

    print("\nCalendar years")
    tab = calendar_years({"momentum": mom, "equal_weight": ew, "nifty": nifty})
    print(f"  {'year':<6}{'momentum':>11}{'equal-wt':>11}{'Nifty 50':>11}")
    for r in tab.itertuples():
        print(f"  {r.year:<6}{_pct(r.momentum, 10, True)}{_pct(r.equal_weight, 10, True)}"
              f"{_pct(r.nifty, 10, True)}")
    print("  The first and last years are partial.")

    tr, costs = result["trades"], result["costs"]
    years = max(len(mom) / 12, 1e-9)
    total_cost = costs["charges"] + costs["dp"] + costs["slippage"]
    avg_equity = float(result["equity"].mean())
    sells = tr[tr.action == "SELL"] if len(tr) else tr
    print("\nTrading and costs")
    print(f"  {len(tr)} orders, {len(tr) / years:.0f} a year | median holding "
          f"{sells['days_held'].median() if len(sells) else float('nan'):.0f} days")
    print(f"  charges Rs{costs['charges']:,.0f} + DP Rs{costs['dp']:,.0f} + slippage Rs{costs['slippage']:,.0f}"
          f" = Rs{total_cost:,.0f}, about {total_cost / years / avg_equity * 100:.2f}% of capital a year")
    tax = sum(result["tax"].values())
    print(f"  tax on realised gains (simplified view): Rs{tax:,.0f} over the period"
          f" | after it, Rs1L became about Rs{result['equity'].iloc[-1] - tax:,.0f}")
    print("  Buy-and-hold of the index pays no tax until it sells; this portfolio pays as it goes.")
    print("\nResearch only. No orders placed, no config changed.")


def run(years=YEARS, market_data=None, refresh=False, cache_dir=CACHE_DIR, universe=None):
    if market_data is None:
        from kite_data import KiteMarketData
        market_data = KiteMarketData()
    universe = universe or sc.load_universe()
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=int((years + 1.2) * 365.25))   # + a year of warm-up
    print(f"Fetching {len(universe)} daily histories from {start} (cached in {cache_dir})...")
    panel, failures = fetch_panel(market_data, universe, start, end, cache_dir, refresh)
    nifty = _frame(market_data.candles("NIFTY 50", "day", start, end))["Close"]
    opens, close, turnover = matrices(panel)
    close = close.reindex(close.index.union(nifty.index)).sort_index()
    opens, turnover = opens.reindex(close.index), turnover.reindex(close.index)
    excluded = suspect_symbols(close)
    test_start = close.index[min(LOOKBACK, len(close.index) - 1)]
    rebal = month_ends(close.index, test_start)
    if len(rebal) < 3:
        raise SystemExit("Not enough history for a monthly test.")
    result = simulate(opens, close, turnover, rebal, excluded)
    mom = monthly_from_levels(result["equity"].reindex(close.index).ffill()
                              .fillna(START_CAPITAL), rebal)
    ew = equal_weight_monthly(close, turnover, rebal, excluded)
    nif = monthly_from_levels(nifty, rebal)
    info = dict(universe=len(universe), skipped=len(failures), suspect=len(excluded),
                eligible_median=float(np.median([len(eligible(close, turnover, d, excluded))
                                                 for d in rebal])))
    print_report(result, mom, ew, nif, info)
    result["trades"].to_csv(TRADES_FILE, index=False)
    pd.DataFrame({"momentum": mom, "equal_weight": ew, "nifty50": nif}).to_csv(
        MONTHLY_FILE, index_label="month_end")
    print(f"Orders -> {TRADES_FILE.name} | monthly returns -> {MONTHLY_FILE.name}")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--years", type=float, default=YEARS, help=f"test years (default {YEARS})")
    ap.add_argument("--refresh", action="store_true", help="refetch instead of using the cache")
    args = ap.parse_args(argv)
    run(args.years, refresh=args.refresh)


if __name__ == "__main__":
    main()
