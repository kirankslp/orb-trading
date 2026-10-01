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
