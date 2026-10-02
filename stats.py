"""Shared statistics for the backtest and paper-trading reports.

Positions opened on the same session share that day's market move, so they are
not independent draws. A confidence interval built from per-trade spread times
sqrt(trade count) treats them as if they were, and comes out too narrow: on a
real 179-session run it put the ORB's gross edge clearly above zero when a
correlation-adjusted interval reached below it.

The fix is to cluster by session: sum each session to one number, then build
the interval from the spread of those daily totals. Whatever the correlation
between positions inside a day, it is inside the daily total, so it is counted.
"""

import math


def session_total_ci(frame, column="pnl", by="date", sessions=None, z=1.96):
    """95% interval on the TOTAL of `column`, one session per draw.

    `sessions` lists every session in the sample. Pass it whenever the frame
    holds trades only: a session with no trade is a real zero, and leaving it
    out would understate the spread. Returns (lo, hi), or (None, None) when
    there are fewer than two sessions to measure a spread from.
    """
    if frame is None or frame.empty:
        return None, None
    daily = frame.groupby(by)[column].sum()
    if sessions is not None:
        daily = daily.reindex(list(sessions), fill_value=0.0)
    n = len(daily)
    if n < 2:
        return None, None
    half = z * daily.std(ddof=1) * math.sqrt(n)
    total = daily.sum()
    return total - half, total + half


def mean_ci(values, z=1.96):
    """(mean, lo, hi) for the mean of independent draws, e.g. one per session.
    lo and hi are None with fewer than two values."""
    v = [float(x) for x in values]
    n = len(v)
    if n == 0:
        return None, None, None
    m = sum(v) / n
    if n < 2:
        return m, None, None
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    half = z * sd / math.sqrt(n)
    return m, m - half, m + half


def diff_ci(a, b, z=1.96):
    """(difference, lo, hi) for mean(a) - mean(b), two independent groups of
    sessions with unequal spreads (Welch). None bounds if either group has
    fewer than two values."""
    a, b = [float(x) for x in a], [float(x) for x in b]
    if not a or not b:
        return None, None, None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    d = ma - mb
    if len(a) < 2 or len(b) < 2:
        return d, None, None
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    half = z * math.sqrt(va / len(a) + vb / len(b))
    return d, d - half, d + half
