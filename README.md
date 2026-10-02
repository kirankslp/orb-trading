# ORB trading

NSE algorithmic-trading workspace using Kite Connect market data. Scheduled
ORB scripts remain analysis-only; the local React app can submit a manually
reviewed recommendation order after an explicit confirmation.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
export KITE_CREDENTIALS_FILE=/path/to/creds.txt  # file containing api_key
export KITE_ACCESS_TOKEN=...                     # fresh daily Kite session
.venv/bin/python test_orb_backtest.py
.venv/bin/python -m unittest test_kite_data.py
```

The local scheduler defaults `KITE_CREDENTIALS_FILE` to the existing sibling
project's `../zerodha-arth/creds.txt`, so this machine can reuse its API key
without copying any credential into this repository. Set it explicitly if that
is not your credentials location. Kite access tokens expire at 06:00 IST; a
fresh `KITE_REQUEST_TOKEN` plus `api_secret` may be supplied instead, but no
token is saved by this project.

Use `python daily_plan.py --premarket`, `python daily_plan.py`, or
`python orb_backtest.py` after authenticating.

## Strategy comparison

`strategy_backtest.py` runs five intraday strategies on the **same**
point-in-time screener picks, sessions and cost model, so only the rule differs:

| Name | Rule |
|---|---|
| `orb45` | the frozen ORB: 45-minute range on 15m bars (baseline) |
| `orb15` | ORB on the first 15 minutes, on 5m bars |
| `vwap` | fade a close 0.5x daily ATR away from session VWAP, exit at VWAP |
| `bollinger` | fade a close outside the 20/2 bands, exit at the middle band |
| `ema` | 9/20 EMA crossover, exit on the opposite cross |

Each ORB also gets an `_rvol` row: the same trades restricted to stocks whose
opening-range volume was at least 2x the median of that window over their
previous 20 sessions. Relative volume is a catalyst proxy that needs no news
feed: it is known the moment the range closes and its baseline uses only
earlier sessions. The 2x threshold was fixed before any run; the report's other
volume buckets are descriptive, not a menu to pick from.

Signals are read at a bar's close and filled at the next bar's open, and every
strategy carries the ORB's ATR stop and 15:15 square-off. The report leads with
a paired, session-by-session comparison against `orb45` and says when a
difference is not statistically distinguishable. It also shows how far price
travelled after each ORB entry, in ATR and in opening-range widths, to diagnose
which target unit the market actually reaches.

```powershell
.\run-backtest.ps1 -Compare -RequestToken <fresh token>
.\run-backtest.ps1 -Compare -Days 365 -RequestToken <fresh token>   # a year
```

`-Days` sets the history window for that run only. Kite caps how much history
one request may cover (about 180 days of 15m bars, 90 of 5m), so longer windows
are fetched in several requests per symbol and stitched. Backtests only ever
include sessions that have closed: a run during market hours ignores today.

Research only. None of these strategies is part of the paper-trading freeze.

## Measuring real slippage

A 179-session comparison left every strategy except Bollinger about Rs4-5 a
trade short of break-even, and the largest cost line, slippage, is an
assumption. `spread_probe.py` measures it from Kite's live order book without
placing anything: for each of the day's picks it prices a slot-sized market
order against the five-level book and records the cost per leg versus the mid.

```powershell
.\run-backtest.ps1 -Spreads -RequestToken <fresh token>   # after 09:15; runs to the close
python spread_probe.py report --gap 5.1                    # measured vs modelled
```

The watchlist is ranked once a day and cached, so only the first snapshot pays
for the daily fetch. Treat the result as a lower bound: breakout entries are
stop orders that fire while price moves fast, when books thin, and scheduled
snapshots mostly catch calmer moments.

## P&L calendar

The **P&L calendar** tab in the local app shows any trade log on disk (paper
ledger, latest comparison or ORB backtest, or an archived run) as one square per
session: green for a profitable day, red for a loss, brighter for a larger day,
an outline for break-even and a dashed square for a weekday with no trades.
Hover for the day's numbers, click for its trades, or switch to a table. The
colour scale is checked for colour-blind and normal-vision separation, and every
value also carries its sign.

## Market regime report

Did the strategies do better or worse in particular market conditions?

```powershell
.\run-backtest.ps1 -Regime -RequestToken <fresh request_token>
.\run-backtest.ps1 -Regime -LogId archive:strategy_trades_<stamp>.csv -Strategy ema
```

`regime_report.py` reads an existing trade log (no re-run) and fetches only
Nifty 50 and India VIX daily candles. It reports:

1. **One pre-stated test, with a verdict:** are sessions that open after India
   VIX closed at 15 or higher better than the rest, per strategy? The previous
   close is known before the open, so a yes could become a real filter. It also
   says how many separate stretches the high-VIX sessions fall in: one stretch
   is one market episode, not a pattern.
2. Previous-day VIX buckets, Nifty day type (up, sideways, down) and Nifty day
   range, for one strategy. Day type and range are known only after the close,
   so they describe and cannot filter.
3. A month table with Nifty's move and the average VIX.

Every group carries a 95% interval with sessions as the draws. Only the
pre-stated test gets a verdict; the other tables are description, and a pattern
in them is a lead to state in advance and check on new sessions, not a result.

## Futures re-pricing

Would the same trades have survived in stock futures?

```powershell
.\run-backtest.ps1 -Futures -RequestToken <fresh request_token>
```

`futures_reprice.py` takes an existing trade log and re-prices each trade in a
stock that has futures as one lot of its nearest future, with the same entry and
exit prices. Per strategy it shows three cost scenarios on identical trades:

- **cash at lot size:** the shares at the same rupee size as a lot, so the
  brokerage cap applies equally. This separates the effect of size from the
  effect of the instrument.
- **futures STT 0.02%** and **futures STT 0.05%:** the sell-side STT before and
  after the 2026 Budget change. Check Zerodha's current charges.

Each scenario shows net per trade, total net with a session-clustered 95%
interval, and the per-leg slippage at which the strategy breaks even. A last
section shows the margin a one-lot-per-trade version would have needed, its
worst day and deepest drawdown.

Assumptions:
- A future moves rupee for rupee with the share intraday.
- Lot sizes are today's.
- Slippage is the trade's cash tier.
- Margin is taken as 20% of contract value.

## Paper trading

A point-in-time forward test at Rs 1,00,000 across 10 slots of Rs 10,000,
ranked out of NSE's full equity list. The strategy
config is frozen for the run; see `FREEZE.md` for what that covers and why.

```bash
python paper_broker.py plan       # 10:05 IST, after the 45m range closes
python paper_broker.py resolve    # 15:45 IST, after the close
python paper_report.py            # review
python paper_broker.py status     # freeze integrity, unresolved plans
```

A plan is committed once to `paper/plans/<date>.json` and never rewritten.
Resolution replays the session against the levels recorded that morning rather
than recomputing them, which is what keeps the forward test free of the
lookahead a backtest cannot rule out. The ledger at `paper/ledger.csv` is
append-only.

`paper_report.py` leads with a confidence interval, not the P&L, and says so
when a result is indistinguishable from zero. At this trade count a month of
P&L cannot separate a winning strategy from a losing one; what it does measure
well is execution drift, the gap between a published level and the actual fill.

No orders are placed by any of this.

## Unified local app

The React/Vite app combines this ORB project with the NIFTY 100 SMA scanner in
`C:\Users\Kiran\Documents\ChatGPT\algotrading`. It has no order endpoints.
Create the local Python environment and web dependencies once, then launch it:

```powershell
C:\Users\Kiran\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m venv .unified-venv
.\.unified-venv\Scripts\python.exe -m pip install -r requirements.txt
npm install --prefix web
.\start-unified-app.ps1
```

The page shows the Kite login URL directly above its **Kite refresh token** box
(Kite's official name is `request_token`). The token is exchanged locally and
kept only in the API process's memory.

The strategy tabs are ordered as NIFTY 100 Scanner, ORB Workspace, Momentum,
Execution & Risk, Global Context, Intraday Budget Plan, and Daily
Recommendations. **Refresh all
strategies** updates the daily SMA scan, price/volume momentum, execution
levels, ORB plan, major US/European/Asian indices, CBOE and India VIX, and
market-moving world headlines, then ranks a final consensus list. Global
context changes confidence by at most one level; it never creates or reverses
a strategy signal. Limit, stop-limit, trailing-stop, iceberg, and
market-not-held concepts from the source article are treated as execution/risk
methods, not as independent predictive signals.

Daily Recommendations includes batched Kite LTP snapshots and a **Place order**
button on each row. WAIT rows cannot be ordered. An actionable row opens a
review ticket; the backend locks the side to the recommendation, accepts only
LIMIT or stop-limit orders, validates quantity, product, tick size, trigger
relationship, and distance from the latest quote, and submits only after the
live-order checkbox is confirmed. A returned order ID is an OMS acknowledgement,
not proof that the exchange accepted or filled the order; verify every order in
the Kite order book.

The Intraday Budget Plan defaults to ₹10,000 and accepts an adjustable cash
budget from ₹500 to ₹1,00,00,000. It splits cash equally across at most two
highest-ranked affordable directional ideas, uses whole-share quantities,
shows deployed and unused capital plus stop-risk and potential gross reward,
and does not place orders automatically. No leverage is assumed.
