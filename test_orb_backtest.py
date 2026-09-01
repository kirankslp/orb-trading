"""Deterministic checks for the entry-bar exit logic."""
import sys, importlib
sys.path.insert(0, __import__("os").path.dirname(__file__) or ".")
import pandas as pd
import orb_backtest as ob

TIMES = ["09:15","09:30","09:45","10:00","10:15","10:30","10:45"]

def mkday(day, bars):
    rows = []
    for t, (o,h,l,c) in zip(TIMES, bars):
        rows.append(dict(Open=o, High=h, Low=l, Close=c, date=day, time=t,
                         dt=pd.Timestamp(f"2026-01-05 {t}")))
    return pd.DataFrame(rows)

def run(bars, policy="conservative"):
    ob.ENTRY_BAR_POLICY = policy
    return ob.backtest(mkday("d1", bars))

OR = [(99.5,100,99,99.8), (99.8,100,99,99.9), (99.9,100,99.2,99.9)]  # OR hi 100 / lo 99

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

print("\nall checks passed")
