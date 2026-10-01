"""
Intraday strategies beyond ORB, on the same data, costs and picks.

    vwap       VWAP mean reversion: fade a stretch away from the session VWAP,
               exit back at VWAP
    bollinger  Bollinger mean reversion: fade a close outside the 20/2 bands,
               exit at the middle band
    ema        9/20 EMA crossover: enter on the cross, exit on the opposite
               cross

These follow the rule shapes in AlgoTest's "6 Popular Algo Trading Strategies"
(Mar 2026). The article gives entries and exits but no stops, and a mean
reversion trade with no stop is an unbounded short squeeze, so every strategy
here also carries the same ATR-sized stop the ORB uses and the same 15:15
square-off.

Causality is the rule that matters. Every indicator at bar i is computed from
bars <= i only, a signal is read at bar i's CLOSE, and the fill is at bar i+1's
OPEN. Filling at the signal bar's own close would assume you can see a close
and trade at it in the same instant, which is the most common way a backtest
of an indicator strategy flatters itself.

Costs go through orb_backtest._pnl, so a rupee of friction here is the same
rupee as in the ORB backtest. The only thing that differs between strategies is
the rule. Research only: none of this is in the paper-trading freeze, and
nothing here places orders.
"""

import numpy as np
import pandas as pd

import orb_backtest as ob

# ---- shared ---------------------------------------------------------------
ENTRY_CUTOFF   = "14:30"   # no new positions after this; leaves time to work
MIN_BARS_TODAY = 2         # skip the first bars: VWAP is one print at 09:15

# ---- VWAP mean reversion ---------------------------------------------------
# "Significantly above VWAP" needs a unit. A fixed % is meaningless across a
# 1%-ATR bank and a 5%-ATR smallcap, so the stretch is measured in the symbol's
# own daily ATR, the same unit the stops already use.
VWAP_ENTRY_ATR = 0.5       # enter when |close - VWAP| >= 0.5 x daily ATR

# ---- Bollinger mean reversion ---------------------------------------------
BB_PERIOD = 20             # bars; on 15m candles that spans prior sessions,
BB_STD    = 2.0            # which is what lets the bands exist from the open

# ---- EMA crossover ---------------------------------------------------------
EMA_FAST, EMA_SLOW = 9, 20


# --------------------------------------------------------------------------
# indicators: every column at row i depends on rows <= i only
# --------------------------------------------------------------------------

def add_vwap(g):
    """Session VWAP on typical price. Resets each day by definition."""
    g = g.copy()
    tp = (g["High"] + g["Low"] + g["Close"]) / 3.0
    vol = g["Volume"].clip(lower=0).fillna(0)
    pv = (tp * vol).groupby(g["date"]).cumsum()
    cv = vol.groupby(g["date"]).cumsum()
    # A zero-volume opening print has no VWAP yet; fall back to typical price
    # rather than dividing by zero.
    g["vwap"] = np.where(cv > 0, pv / cv.replace(0, np.nan), tp)
    return g


def add_bollinger(g, period=BB_PERIOD, k=BB_STD):
    g = g.copy()
    mid = g["Close"].rolling(period, min_periods=period).mean()
    sd = g["Close"].rolling(period, min_periods=period).std(ddof=0)
    g["bb_mid"], g["bb_up"], g["bb_lo"] = mid, mid + k * sd, mid - k * sd
    return g


def add_ema(g, fast=EMA_FAST, slow=EMA_SLOW):
    g = g.copy()
    g["ema_fast"] = g["Close"].ewm(span=fast, adjust=False).mean()
    g["ema_slow"] = g["Close"].ewm(span=slow, adjust=False).mean()
    # Recursive EMAs are defined from the first bar, but they are only
    # meaningful once enough bars have fed them.
    g.loc[g.index[:slow], ["ema_fast", "ema_slow"]] = np.nan
    return g


# --------------------------------------------------------------------------
# strategy rules. Each sees one row (or two) whose values are all known at
# that row's close, and says what to do at the NEXT bar's open.
# --------------------------------------------------------------------------

class Strategy:
    name = "base"

    def prepare(self, hist):
        """Add indicator columns to a symbol's full, time-sorted history."""
        return hist

    def entry(self, prev, row, atr_frac):
        """'LONG', 'SHORT' or None, from information at `row`'s close."""
        return None

    def target(self, row, side):
        """Price level to exit at, known at `row`'s close, or None."""
        return None

    def exit_signal(self, prev, row, side):
        """True to exit at the next bar's open, from `row`'s close."""
        return False


class VWAPReversion(Strategy):
    name = "vwap"

    def prepare(self, hist):
        return add_vwap(hist)

    def entry(self, prev, row, atr_frac):
        if not atr_frac or not np.isfinite(row["vwap"]):
            return None
        stretch = (row["Close"] - row["vwap"]) / row["vwap"]
        if stretch >= VWAP_ENTRY_ATR * atr_frac:
            return "SHORT"
        if stretch <= -VWAP_ENTRY_ATR * atr_frac:
            return "LONG"
        return None

    def target(self, row, side):
        return row["vwap"]


class BollingerReversion(Strategy):
    name = "bollinger"

    def prepare(self, hist):
        return add_bollinger(hist)

    def entry(self, prev, row, atr_frac):
        if not np.isfinite(row["bb_up"]):
            return None
        if row["Close"] > row["bb_up"]:
            return "SHORT"
        if row["Close"] < row["bb_lo"]:
            return "LONG"
        return None

    def target(self, row, side):
        return row["bb_mid"]


class EMACross(Strategy):
    name = "ema"

    def prepare(self, hist):
        return add_ema(hist)

    @staticmethod
    def _cross(prev, row):
        if prev is None or not np.isfinite(prev["ema_slow"]) or not np.isfinite(row["ema_slow"]):
            return None
        was = prev["ema_fast"] - prev["ema_slow"]
        now = row["ema_fast"] - row["ema_slow"]
        if was <= 0 < now:
            return "LONG"
        if was >= 0 > now:
            return "SHORT"
        return None

    def entry(self, prev, row, atr_frac):
        return self._cross(prev, row)

    def exit_signal(self, prev, row, side):
        c = self._cross(prev, row)
        return c is not None and c != side


STRATEGIES = {s.name: s for s in (VWAPReversion(), BollingerReversion(), EMACross())}


# --------------------------------------------------------------------------
# one symbol, one session
# --------------------------------------------------------------------------

def trade_day(strategy, day, hist, symbol=None, budget=None, turnover_cr=None,
              atr_pct=None):
    """Run one strategy through one session of a prepared history frame.

    `hist` is the symbol's full history with the strategy's indicator columns
    already added (strategy.prepare), so bands and EMAs are warm at the open.
    Returns one trade dict (orb_backtest._pnl shape) or None. One trade per
    symbol per day, matching the ORB, so trade counts and costs compare.
    """
    budget = ob.slot_budget() if budget is None else budget
    sl_pct, _ = ob.levels_for(atr_pct)
    atr_frac = (atr_pct or 0) / 100.0
    g = hist[hist["date"] == day].sort_values("dt").reset_index(drop=True)
    if len(g) < MIN_BARS_TODAY + 2:
        return None

    side = entry = stop = qty = entry_time = None
    tgt = None
    pending_exit = False

    for i in range(MIN_BARS_TODAY, len(g)):
        row = g.iloc[i]
        prev = g.iloc[i - 1]
        o, h, l, c, t = row["Open"], row["High"], row["Low"], row["Close"], row["time"]

        if side is None:
            # The decision was taken at the PREVIOUS bar's close, so it fills
            # at this bar's open. Nothing about this bar was known to it.
            want =strategy.entry(g.iloc[i - 2] if i >= 2 else None, prev, atr_frac)
            if want is None or prev["time"] >= ENTRY_CUTOFF or t >= ob.SQUAREOFF_TIME:
                continue
            side, entry, entry_time = want, o, t
            qty = int(budget // entry)
            if qty == 0:
                return None
            stop = entry * (1 - sl_pct) if side == "LONG" else entry * (1 + sl_pct)
            tgt = strategy.target(prev, side)
            # A target already on the wrong side of the fill means the move it
            # was waiting for happened in the gap. There is nothing to capture.
            if tgt is not None and ((side == "LONG" and tgt <= entry) or
                                    (side == "SHORT" and tgt >= entry)):
                side = None
                continue

        # --- in a trade: square-off, a signalled exit, then stop and target ---
        if t >= ob.SQUAREOFF_TIME:
            return _close(day, side, entry, c, entry_time, t, "squareoff", False,
                          symbol, qty, turnover_cr, sl_pct)
        if pending_exit:
            return _close(day, side, entry, o, entry_time, t, "signal", False,
                          symbol, qty, turnover_cr, sl_pct)

        hit = _levels(side, stop, tgt, o, h, l)
        if hit:
            px, reason, amb = hit
            return _close(day, side, entry, px, entry_time, t, reason, amb,
                          symbol, qty, turnover_cr, sl_pct)

        # Levels for the next bar come from this bar's close. The target moves
        # with VWAP or the middle band, but only ever by information in hand.
        new_tgt = strategy.target(row, side)
        if new_tgt is not None and np.isfinite(new_tgt):
            tgt = new_tgt
        if strategy.exit_signal(prev, row, side):
            pending_exit = True

    if side is not None:
        last = g.iloc[-1]
        return _close(day, side, entry, last["Close"], entry_time, last["time"],
                      "eod", False, symbol, qty, turnover_cr, sl_pct)
    return None


def _levels(side, stop, tgt, o, h, l):
    """Resolve one bar against the stop and a (possibly absent) target.

    A gap through a level fills at the open, not at the level: through a stop
    that is the worse price, through a target the better one. A bar spanning
    both is unresolvable from OHLC, so the stop wins and the trade is flagged,
    the same conservative policy the ORB uses.
    """
    if side == "LONG":
        if o <= stop:
            return o, "stoploss", False
        if tgt is not None and o >= tgt:
            return o, "target", False
        hit_sl, hit_tgt = l <= stop, tgt is not None and h >= tgt
    else:
        if o >= stop:
            return o, "stoploss", False
        if tgt is not None and o <= tgt:
            return o, "target", False
        hit_sl, hit_tgt = h >= stop, tgt is not None and l <= tgt
    if hit_sl:
        return stop, "stoploss", bool(hit_tgt)
    if hit_tgt:
        return tgt, "target", False
    return None


def _close(day, side, entry, exit_px, t_in, t_out, reason, amb, symbol, qty,
           turnover_cr, sl_pct):
    return ob._pnl(day, side, entry, exit_px, t_in, t_out, reason, amb, symbol,
                   qty, turnover_cr, sl_pct, None)


def backtest_watchlist(strategy, intraday, picks, metrics=None):
    """Same contract as orb_backtest.backtest_watchlist, for any strategy here."""
    budget, metrics = ob.slot_budget(), metrics or {}
    prepared, trades = {}, []
    for day in sorted(picks):
        for sym in picks[day]:
            df = intraday.get(sym)
            if df is None:
                continue
            if sym not in prepared:
                prepared[sym] = strategy.prepare(df.sort_values("dt").reset_index(drop=True))
            hist = prepared[sym]
            if hist[hist["date"] == day].empty:
                continue
            m = metrics.get((day, sym)) or {}
            t = trade_day(strategy, day, hist, sym, budget,
                          m.get("turnover_cr"), m.get("atr_pct"))
            if t:
                t["strategy"] = strategy.name
                trades.append(t)
    return pd.DataFrame(trades)
