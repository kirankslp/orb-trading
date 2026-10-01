"""
Daily P&L for the calendar view, from any trade log the project writes.

Sources, newest first in the picker:
    paper/ledger.csv              forward paper trading (real point-in-time)
    strategy_trades.csv           latest strategy comparison (all strategies)
    orb_trades.csv                latest ORB backtest
    backtests/*_trades_*.csv      archived runs from run-backtest.ps1

A log is chosen by an id from available_logs(), never by a path the client
sends, so the endpoint cannot be pointed at an arbitrary file.

Each day is summed across the positions open that session, because that is the
unit the account actually experiences: ten positions on one morning are one
day's result, not ten. Research only; nothing here trades.
"""

import os
from pathlib import Path

import pandas as pd


def available_logs(root):
    """Every trade log on disk, as [{id, label, kind, path, modified}]."""
    root = Path(root)
    found = []

    def add(log_id, label, kind, path):
        if path.is_file():
            found.append(dict(id=log_id, label=label, kind=kind, path=str(path),
                              modified=path.stat().st_mtime))

    add("paper", "Paper trading ledger", "paper", root / "paper" / "ledger.csv")
    add("compare", "Latest strategy comparison", "compare", root / "strategy_trades.csv")
    add("orb", "Latest ORB backtest", "orb", root / "orb_trades.csv")
    archive = root / "backtests"
    if archive.is_dir():
        for p in sorted(archive.glob("*_trades_*.csv"), key=lambda p: p.stat().st_mtime,
                        reverse=True):
            kind = "compare" if p.name.startswith("strategy_trades") else "orb"
            stamp = p.stem.split("_trades_")[-1]
            what = "comparison" if kind == "compare" else "ORB backtest"
            add(f"archive:{p.name}", f"Archived {what} {stamp}", kind, p)
    return found


def load_trades(log):
    """One frame with date, strategy, pnl, gross, cost for any log kind."""
    df = pd.read_csv(log["path"])
    if df.empty:
        return df
    if log["kind"] == "paper":
        df["date"] = df["plan_date"]
        df["strategy"] = "paper"
    elif "strategy" not in df.columns:
        df["strategy"] = "orb45"
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
    for col in ("pnl", "gross", "cost"):
        if col not in df.columns:
            df[col] = 0.0
    return df


def daily(trades):
    """Per session: net, gross, costs, trade count and winning trades."""
    if trades.empty:
        return []
    g = trades.groupby("date")
    days = pd.DataFrame({
        "pnl": g.pnl.sum(), "gross": g.gross.sum(), "cost": g.cost.sum(),
        "trades": g.size(), "wins": g.pnl.apply(lambda s: int((s > 0).sum())),
    }).reset_index().sort_values("date")
    return [dict(date=r.date, pnl=round(float(r.pnl), 2), gross=round(float(r.gross), 2),
                 cost=round(float(r.cost), 2), trades=int(r.trades), wins=int(r.wins))
            for r in days.itertuples()]


def _streak(days, test):
    best = cur = 0
    for d in days:
        cur = cur + 1 if test(d["pnl"]) else 0
        best = max(best, cur)
    return best


def summary(days):
    if not days:
        return dict(sessions=0)
    pnl = [d["pnl"] for d in days]
    best = max(days, key=lambda d: d["pnl"])
    worst = min(days, key=lambda d: d["pnl"])
    up = sum(p > 0 for p in pnl)
    down = sum(p < 0 for p in pnl)
    return dict(
        sessions=len(days), net=round(sum(pnl), 2),
        gross=round(sum(d["gross"] for d in days), 2),
        cost=round(sum(d["cost"] for d in days), 2),
        profitable_days=up, losing_days=down, flat_days=len(days) - up - down,
        best=dict(date=best["date"], pnl=best["pnl"]),
        worst=dict(date=worst["date"], pnl=worst["pnl"]),
        longest_winning_streak=_streak(days, lambda p: p > 0),
        longest_losing_streak=_streak(days, lambda p: p < 0),
    )


def _plain(value):
    """JSON-safe scalar. The API encodes with json.dumps(default=str), which
    would turn a numpy integer into a string and write NaN as a bare token that
    browsers refuse to parse."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and value != value:
        return None
    return value


TRADE_FIELDS = ("symbol", "side", "qty", "entry", "exit", "entry_time", "exit_time",
                "reason", "gross", "cost", "pnl")


def calendar_payload(root, log_id=None, strategy=None):
    """Everything the calendar view needs for one log and one strategy."""
    logs = available_logs(root)
    public = [{k: v for k, v in l.items() if k != "path"} for l in logs]
    if not logs:
        return dict(logs=[], log=None, strategies=[], strategy=None, days=[],
                    summary=dict(sessions=0), trades={})
    by_id = {l["id"]: l for l in logs}
    if log_id is not None and log_id not in by_id:
        raise ValueError(f"Unknown trade log {log_id!r}.")
    log = by_id[log_id] if log_id else logs[0]
    trades = load_trades(log)
    strategies = sorted(trades["strategy"].unique()) if not trades.empty else []
    if strategy is not None and strategy not in strategies:
        raise ValueError(f"{log['label']} has no strategy {strategy!r}.")
    # Open on the baseline the others are judged against, not on whichever
    # name happens to sort first.
    default = next((s for s in ("paper", "orb45") if s in strategies),
                   strategies[0] if strategies else None)
    strategy = strategy or default
    mine = trades[trades["strategy"] == strategy] if strategy else trades.iloc[0:0]
    days = daily(mine)
    cols = [c for c in TRADE_FIELDS if c in mine.columns]
    by_date = {d: [{k: _plain(v) for k, v in rec.items()} for rec in g[cols].to_dict("records")]
               for d, g in mine.groupby("date")} if len(mine) else {}
    return dict(logs=public, log={k: v for k, v in log.items() if k != "path"},
                strategies=strategies, strategy=strategy, days=days,
                summary=summary(days), trades=by_date)
