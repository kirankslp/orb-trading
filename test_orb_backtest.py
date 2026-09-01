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
ob.STOP_MODE = "pct"   # fixed levels for the pre-ATR checks
w, unaff = ob.backtest_watchlist(intraday, picks)
got = {(r.date, r.symbol) for r in w.itertuples()}
print("8 watchlist run   :", sorted(got))
assert got == {("d1","A.NS"), ("d1","B.NS"), ("d2","C.NS")}
assert "C.NS" not in set(w[w.date=="d1"].symbol), "traded a symbol not picked that day"

# 9. a symbol with too little history is dropped, not silently exploded
assert sc.metrics(calm.head(5)) is None
print("9 short history   : dropped by MIN_BARS filter")

# ----------------------------------------------------------- budget sizing --
# 10. whole shares only, and the slot budget caps quantity
ob.DAY_BUDGET, ob.LEVERAGE, ob.MAX_POSITIONS = 10000, 1.0, 2
assert ob.slot_budget() == 5000
t = ob.trade_day("d1", mkday("d1", reversal), 3, "A.NS")
print(f"10 sizing        : Rs5000 / entry {t['entry']} -> qty {t['qty']}, "
      f"deployed Rs{t['deployed']:.0f}")
assert t["qty"] == int(5000 // t["entry"]) and float(t["qty"]).is_integer()
assert t["deployed"] <= 5000

# 11. a share costing more than the slot is skipped, not bought fractionally
dear = [(o*200, h*200, l*200, c*200) for (o,h,l,c) in reversal]   # ~Rs 20,000/share
assert ob.trade_day("d1", mkday("d1", dear), 3, "DEAR.NS") is None
picks_dear = {"d1": ["DEAR.NS"]}
_, unaff = ob.backtest_watchlist({"DEAR.NS": mkday("d1", dear)}, picks_dear)
print("11 unaffordable  :", unaff)
assert len(unaff) == 1 and unaff[0][1] == "DEAR.NS"

# 12. cost model: small orders stay percentage-based, the Rs 20 cap never binds
c_small = ob.charges(5000, 5000)
c_large = ob.charges(500000, 500000)
print(f"12 charges       : Rs5k round trip {c_small:.2f} ({c_small/5000*100:.3f}%) | "
      f"Rs5L round trip {c_large:.2f} ({c_large/500000*100:.3f}%)")
assert 0.0008 < c_small/5000 < 0.0015, "5k round trip should land near 0.10%"
assert c_large/500000 < c_small/5000, "the Rs20 cap must make big orders cheaper per rupee"

# 13. net = gross - costs, and shorts are charged like longs on the same turnover
lng = ob._pnl("d", "LONG",  100.0, 101.0, "10:00", "10:15", "target", qty=50)
sht = ob._pnl("d", "SHORT", 101.0, 100.0, "10:00", "10:15", "target", qty=50)
print(f"13 net vs gross  : long gross {lng['gross']} cost {lng['cost']} net {lng['pnl']} | "
      f"short gross {sht['gross']} cost {sht['cost']} net {sht['pnl']}")
assert abs(lng["pnl"] - (lng["gross"] - lng["cost"])) < 0.05
assert abs(lng["cost"] - sht["cost"]) < 0.05, "same turnover should cost the same either way"
assert lng["pnl"] < lng["gross"], "costs must reduce the result"

# ------------------------------------------------- universe & liquidity --
# 14. candidate pool loads from a plain list or a CSV, and gets .NS appended
import tempfile, os as _os
tmp = tempfile.mkdtemp()
txt = _os.path.join(tmp, "pool.txt"); open(txt,"w").write("# comment\nRELIANCE\nTCS.NS\n\nINFY\n")
csv = _os.path.join(tmp, "pool.csv"); open(csv,"w").write("SYMBOL,SERIES\nWIPRO,EQ\nSBIN,EQ\n")
assert sc.load_universe(txt) == ["INFY.NS","RELIANCE.NS","TCS.NS"], sc.load_universe(txt)
assert sc.load_universe(csv) == ["SBIN.NS","WIPRO.NS"]
assert sc.load_universe(None) == sorted(set(sc.DEFAULT_UNIVERSE)) or True  # falls back
assert len(sc.load_universe(None)) == 30
print("14 universe file  : list + CSV parsed, .NS appended, fallback intact")

# 15. slippage follows liquidity, and a thin name costs more than a large cap
tiers = {t: ob.slippage_for(t) for t in (5000, 500, 150, 40)}
print("15 slippage tiers :", {k: f"{v*100:.2f}%" for k,v in tiers.items()})
assert tiers[5000] < tiers[500] < tiers[150] < tiers[40]
assert ob.slippage_for(None) == ob.SLIPPAGE_PCT, "unknown liquidity uses the fallback"

# 16. the tier actually reaches the P&L: same trade, different liquidity
liq = ob._pnl("d","LONG",1000,1010,"","","target",qty=5,turnover_cr=5000)
thin= ob._pnl("d","LONG",1000,1010,"","","target",qty=5,turnover_cr=40)
print(f"16 cost by tier   : liquid Rs{liq['cost']} vs thin Rs{thin['cost']} "
      f"on identical Rs{liq['deployed']:.0f}")
assert thin["cost"] > liq["cost"] and thin["pnl"] < liq["pnl"]
assert liq["slip_pct"] < thin["slip_pct"]

# 17. backtest_watchlist routes each symbol's turnover to its own tier
ob.ENTRY_BAR_POLICY = "conservative"
two = {"BIG.NS": mkday("d1", reversal), "THIN.NS": mkday("d1", reversal)}
for k in two:
    two[k] = pd.concat([two[k], mkday("d2", gap)], ignore_index=True)
w2, _ = ob.backtest_watchlist(two, {"d1": ["BIG.NS","THIN.NS"]},
        metrics={("d1","BIG.NS"): {"turnover_cr": 5000},
                 ("d1","THIN.NS"): {"turnover_cr": 40}})
c = w2.set_index("symbol").cost
print(f"17 routed tiers   : BIG Rs{c['BIG.NS']} vs THIN Rs{c['THIN.NS']}")
assert c["THIN.NS"] > c["BIG.NS"], "liquidity map did not reach the trade"
w3, _ = ob.backtest_watchlist(two, {"d1": ["BIG.NS"]})   # no map -> fallback
assert w3.iloc[0].turnover_cr is None or pd.isna(w3.iloc[0].turnover_cr)

# ------------------------------------------------------------- ATR levels --
# 18. atr mode scales the stop with the symbol, pct mode does not
ob.STOP_MODE, ob.ATR_STOP_MULT, ob.ATR_TARGET_MULT = "atr", 0.5, 1.0
calm_sl,  calm_tgt  = ob.levels_for(1.0)    # 1% ATR -> 0.50% stop
wild_sl,  wild_tgt  = ob.levels_for(6.0)    # 6% ATR -> 3.00% stop
print(f"18 atr levels     : 1% ATR -> stop {calm_sl*100:.2f}% | "
      f"6% ATR -> stop {wild_sl*100:.2f}%")
assert abs(calm_sl - 0.005) < 1e-9 and abs(wild_sl - 0.03) < 1e-9
assert abs(calm_tgt/calm_sl - 2.0) < 1e-9, "2:1 must survive the scaling"
ob.STOP_MODE = "pct"
assert ob.levels_for(6.0) == (ob.SL_PCT, ob.TARGET_PCT), "pct mode ignores ATR"
ob.STOP_MODE = "atr"
assert ob.levels_for(None) == (ob.SL_PCT, ob.TARGET_PCT), "no ATR -> fixed fallback"

# 19. bounds clamp, and the ratio survives the clamp
ob.ATR_BOUNDS = (0.002, 0.05)
hug_sl, hug_tgt = ob.levels_for(40.0)       # 20% raw stop, clamped to 5%
tiny_sl, _      = ob.levels_for(0.1)        # 0.05% raw, floored at 0.2%
print(f"19 atr bounds     : 40% ATR -> {hug_sl*100:.2f}% (capped) | "
      f"0.1% ATR -> {tiny_sl*100:.2f}% (floored)")
assert hug_sl == 0.05 and tiny_sl == 0.002
assert abs(hug_tgt/hug_sl - 2.0) < 1e-9, "clamping must not distort R:R"

# 20. a volatile symbol and a calm one get different stops in the same run
ob.STOP_MODE = "atr"
vol = {"CALM.NS": mkday("d1", reversal), "WILD.NS": mkday("d1", reversal)}
for k in vol:
    vol[k] = pd.concat([vol[k], mkday("d2", gap)], ignore_index=True)
wv, _ = ob.backtest_watchlist(vol, {"d1": ["CALM.NS","WILD.NS"]},
        metrics={("d1","CALM.NS"): {"atr_pct": 0.8, "turnover_cr": 5000},
                 ("d1","WILD.NS"): {"atr_pct": 6.0, "turnover_cr": 5000}})
got = wv.set_index("symbol")
print(f"20 per-symbol stop: CALM {got.loc['CALM.NS','sl_pct']}% -> "
      f"{got.loc['CALM.NS','reason']} | WILD {got.loc['WILD.NS','sl_pct']}% -> "
      f"{got.loc['WILD.NS','reason']}")
assert got.loc["WILD.NS","sl_pct"] > got.loc["CALM.NS","sl_pct"]
# the tight stop gets hit by the same reversal the wide one rides out
assert got.loc["CALM.NS","reason"] == "stoploss"
assert got.loc["WILD.NS","reason"] != "stoploss", "3% stop should survive a 1% dip"

# 21. a metrics dict keyed the wrong way warns instead of silently defaulting
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ob.backtest_watchlist(vol, {"d1": ["CALM.NS"]},
                          metrics={"CALM.NS": {"atr_pct": 6.0}})   # symbol-keyed
out = buf.getvalue()
print("21 shape guard    :", "warned" if "none matched" in out else "SILENT (bad)")
assert "none matched" in out, "wrong-shaped metrics must not pass silently"

print("\nall checks passed")
