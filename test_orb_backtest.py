"""Deterministic checks for the entry-bar exit logic and the daily-pick wiring."""
import sys, os, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
import orb_backtest as ob
import symbol_screener as sc

TIMES = ["09:15","09:30","09:45","10:00","10:15","10:30","10:45"]

def mkday(day, bars, symbol=None):
    rows = []
    for t, (o,h,l,c) in zip(TIMES, bars):
        rows.append(dict(Open=o, High=h, Low=l, Close=c, date=day, time=t,
                         dt=pd.Timestamp(f"2026-01-05 {t}")))
    return pd.DataFrame(rows)

def run(bars, policy="conservative"):
    ob.ENTRY_BAR_POLICY = policy
    return ob.backtest(mkday("d1", bars))

OR = [(99.5,100,99,99.8), (99.8,100,99,99.9), (99.9,100,99.2,99.9)]  # OR hi 100 / lo 99

# ---------------------------------------------------------------- entry bar --
# 1. breakout candle that reverses straight through the stop
reversal = OR + [(99.9,100.5,99.0,99.2),      # pokes 100.5, then dives to 99.0
                 (99.7,101.0,99.7,100.9),     # would have run to the target
                 (100.9,101.0,100.8,100.9),
                 (100.9,101.0,100.8,100.9)]
cons = run(reversal, "conservative")
skip = run(reversal, "skip")
print("1 conservative:", cons.iloc[0].reason, cons.iloc[0].exit, "@", cons.iloc[0].exit_time)
print("1 skip        :", skip.iloc[0].reason, skip.iloc[0].exit, "@", skip.iloc[0].exit_time)
assert cons.iloc[0].reason == "stoploss" and cons.iloc[0].exit_time == "10:00"
assert skip.iloc[0].reason == "target",  "old behaviour should still book a winner"

# 2. entry candle spanning BOTH levels -> stop assumed, flagged ambiguous
both = OR + [(99.9,101.0,99.0,100.0)] + OR[:1]*3
amb = run(both, "conservative")
print("2 ambiguous   :", amb.iloc[0].reason, "ambiguous=", amb.iloc[0].ambiguous)
assert amb.iloc[0].reason == "stoploss" and bool(amb.iloc[0].ambiguous) is True
opt = run(both, "optimistic")
print("2 optimistic  :", opt.iloc[0].reason, "ambiguous=", opt.iloc[0].ambiguous)
assert opt.iloc[0].reason == "target" and bool(opt.iloc[0].ambiguous) is True

# 3. candle that gaps clean through the level fills at the open, not the level
gap = OR + [(100.4,100.6,100.3,100.5), (100.5,100.6,100.4,100.5),
            (100.5,100.6,100.4,100.5), (100.5,100.6,100.4,100.5)]
g = run(gap, "conservative")
print("3 gap entry   :", g.iloc[0].entry, "(level was 100.0)")
assert g.iloc[0].entry == 100.4

# 4. no fresh entry once the square-off window is reached
late = [(99.5,100,99,99.8)]*3 + [(99.5,99.9,99.1,99.5)]*3
late_df = mkday("d1", late[:6] + [(99.9,101.0,99.9,100.9)])
late_df.loc[6, "time"] = "15:15"
ob.ENTRY_BAR_POLICY = "conservative"
assert ob.backtest(late_df).empty
print("4 late entry  : no trade opened at square-off bar")

# 5. short side mirrors long
short = OR + [(99.1,99.2,98.5,99.15), (99.15,99.9,99.1,99.2),
              (99.2,99.3,98.0,98.1), (98.1,98.2,98.0,98.1)]
s = run(short, "conservative")
print("5 short       :", s.iloc[0].side, s.iloc[0].reason, s.iloc[0].entry, "->", s.iloc[0].exit)
assert s.iloc[0].side == "SHORT"

# ------------------------------------------------------------- daily picks --
def mk_daily(vol_by_day, base=1000.0, vol_shares=5e6):
    """Daily OHLCV where each day's range is vol_by_day[i] (as a fraction)."""
    idx = pd.bdate_range("2025-09-01", periods=len(vol_by_day))
    c = np.full(len(vol_by_day), base)
    rng = np.asarray(vol_by_day)*base
    return pd.DataFrame(dict(Open=c, High=c+rng/2, Low=c-rng/2, Close=c,
                             Volume=np.full(len(vol_by_day), vol_shares)), index=idx)

N = 60
calm  = mk_daily([0.005]*N)
# SPIKE is calm until the very last session, where it explodes
spike_v = [0.005]*(N-1) + [0.30]
spike = mk_daily(spike_v)
daily = {"CALM.NS": calm, "SPIKE.NS": spike, "CALM2.NS": mk_daily([0.004]*N)}

last_day  = calm.index[-1].date()
prev_day  = calm.index[-2].date()

# 6. the spike day itself must NOT see its own bar -> ranking is blind to it
wl_on_spike_day = sc.watchlist_asof(daily, last_day, top_n=1)
wl_next_morning = sc.watchlist_asof(daily, last_day + datetime.timedelta(days=1), top_n=1)
print("6 asof spike day  :", wl_on_spike_day, "(must not be SPIKE.NS)")
print("6 asof next day   :", wl_next_morning, "(now it may be)")
assert wl_on_spike_day[0] != "SPIKE.NS", "look-ahead: ranked on a bar it could not have seen"
assert wl_next_morning[0] == "SPIKE.NS", "should rank once the bar is genuinely in the past"

# 7. asof is strictly-before, so two adjacent days give different views
assert sc.screen_asof(daily, prev_day).shape[0] > 0
a = sc.screen_asof(daily, last_day)
b = sc.screen_asof(daily, last_day + datetime.timedelta(days=1))
assert not a.set_index("symbol").atr_pct.equals(b.set_index("symbol").atr_pct)
print("7 asof window     : metrics shift by one session, as expected")

# 8. backtest_watchlist trades only the symbols picked for that day
ob.ENTRY_BAR_POLICY = "conservative"
intraday = {"A.NS": mkday("d1", reversal), "B.NS": mkday("d1", short),
            "C.NS": mkday("d1", gap)}
for k in intraday:
    intraday[k] = pd.concat([intraday[k], mkday("d2", gap)], ignore_index=True)
picks = {"d1": ["A.NS","B.NS"], "d2": ["C.NS"]}
w = ob.backtest_watchlist(intraday, picks)
got = {(r.date, r.symbol) for r in w.itertuples()}
print("8 watchlist run   :", sorted(got))
assert got == {("d1","A.NS"), ("d1","B.NS"), ("d2","C.NS")}
assert "C.NS" not in set(w[w.date=="d1"].symbol), "traded a symbol not picked that day"

# 9. a symbol with too little history is dropped, not silently exploded
assert sc.metrics(calm.head(5)) is None
print("9 short history   : dropped by MIN_BARS filter")

print("\nall checks passed")
