#!/usr/bin/env bash
# ORB scheduled run, for local cron.
#
#   scripts/run_daily.sh premarket|plan|close|health
#
# cron runs with a minimal environment and a different working directory, which
# is the usual reason a job that works in a shell fails under cron. This wrapper
# resolves the repo from its own location and never relies on inherited state.
#
# Override the interpreter with ORB_PYTHON if you are not using ./.venv.

set -uo pipefail

MODE="${1:-plan}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

PY="${ORB_PYTHON:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    echo "no python found; set ORB_PYTHON to your interpreter" >&2
    exit 127
fi

STAMP="$(date +%Y-%m-%d)"
mkdir -p logs reports
LOG="logs/${STAMP}-${MODE}.log"

rc=0
{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z')  mode=$MODE  python=$PY"
    case "$MODE" in
        premarket) "$PY" daily_plan.py --premarket ;;
        plan)      "$PY" daily_plan.py ;;
        close)     "$PY" orb_backtest.py ;;
        health)    "$PY" test_orb_backtest.py && "$PY" orb_backtest.py ;;
        *)         echo "unknown mode: $MODE (premarket|plan|close|health)"; exit 2 ;;
    esac
} >>"$LOG" 2>&1 || rc=$?

# Snapshot outputs under a dated name so a later review has something durable to
# read. The scripts overwrite their CSVs in place on every run.
for f in daily_plan.csv orb_trades.csv watchlist.csv; do
    [ -f "$f" ] && cp "$f" "reports/${STAMP}-${f}"
done
cp "$LOG" "reports/${STAMP}-${MODE}.txt" 2>/dev/null || true

if [ "$rc" -ne 0 ]; then
    # goes to stderr so cron mails it, if MAILTO is set
    echo "ORB $MODE FAILED rc=$rc on $STAMP. Tail of $LOG:" >&2
    tail -n 15 "$LOG" >&2
    exit "$rc"
fi

echo "ORB $MODE ok -> $LOG"
