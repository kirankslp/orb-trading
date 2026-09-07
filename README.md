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
