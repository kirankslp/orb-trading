# Strategy freeze: 30-session paper-trading run

The strategy config is **frozen** for the duration of this forward test. This
file records what is frozen, why, and what is still allowed to change.

## The config under test

| Parameter | Value |
|---|---|
| `DAY_BUDGET` | ₹1,00,000 |
| `MAX_POSITIONS` | 20 |
| Slot budget | ₹5,000 per position |
| `STOP_MODE` | `atr` |
| `ATR_STOP_MULT` / `ATR_TARGET_MULT` | 0.5 / 1.0 |
| `OR_MINUTES` | 45 |
| `SQUAREOFF_TIME` | 15:15 |
| `ENTRY_BAR_POLICY` | `conservative` |
| `MIN_AVG_TURNOVER` | ₹500 cr |

`paper_broker.config_fingerprint()` hashes every parameter that changes what a
trade is. That covers three groups:

- the strategy and the whole cost model (`FROZEN_PARAMS`)
- the screener's filters and ranking weights (`FROZEN_SCREENER_PARAMS`)
- the **resolved universe itself**, hashed by contents rather than by filename,
  so editing `EQUITY_L.csv` or setting `ORB_UNIVERSE_FILE` both move the hash

Selection is as much a part of the strategy as the entry rule: the universe and
the filters decide which trades can exist at all. Each committed plan stores the
hash, and both `paper_broker.py status` and `paper_report.py` shout if more than
one fingerprint shows up in the ledger. The freeze is therefore auditable rather
than a promise.

## Why freeze at all

At 20 trades a day over ~21 sessions the P&L of a good strategy and a bad one
overlap almost entirely. On this config's economics:

- a **45% win-rate (winning) strategy loses money over 30 days 23% of the time**
- a **35% win-rate (losing) strategy makes money over 30 days 28% of the time**
- the loser out-earns the winner over the same 30 days **15% of the time**

Tuning parameters against results that noisy does not improve the strategy, it
fits it to the noise. Every change kept because it coincided with a good week is
a change that will not survive live trading. A month of daily tuning produces a
beautifully curve-fit strategy and no information.

## What a 30-session run can and cannot settle

**Cannot:** whether the strategy has an edge. Time to an 80%-confidence verdict
at 20 trades/day, adjusting for same-day correlation (ρ≈0.25):

| True win rate | Sessions | Calendar |
|---|---|---|
| 45% | 92 | ~4.4 months |
| 50% | 30 | ~1.4 months |

Note that 20 trades a day is not 10x the information of 2. Same-day intraday
positions share market direction, so 10x the trades is roughly 2 to 2.5x the
*independent* samples.

**Can:** whether the execution assumptions hold. These are per-trade
measurements rather than win/loss bits, so they converge roughly an order of
magnitude faster:

- **Entry drift**, published level vs actual fill. `AGENT.md` flags slippage as
  an estimate worth about half of all friction and the first thing to validate.
  42 fills pin it to about ±0.015pp, enough to catch a 2x error.
- **Ambiguous rate**, trades whose outcome is the `ENTRY_BAR_POLICY` assumption
  rather than observed data.
- **Trigger rate**, how often a published level is reached at all.
- **Cost share**, friction as a fraction of gross.

If the forward run disagrees with the backtest on the same days and symbols,
that gap is diagnostic of lookahead or a bad slippage tier, and it shows up far
sooner than an edge estimate would.

## Allowed to change during the freeze

- Bug fixes in the harness, reporting, or data plumbing
- Logging, diagnostics, tests, documentation
- Anything in `paper_broker.py`, `paper_report.py`, or the web UI that does not
  alter the levels a plan produces

## Not allowed to change during the freeze

- Anything in `FROZEN_PARAMS` (`paper_broker.py`): budget, position count, stop
  and target sizing, range length, square-off, entry-bar policy, and the entire
  cost model
- `MIN_AVG_TURNOVER`, the ranking weights, `TOP_N`, `MIN_PRICE`,
  `LOOKBACK_DAYS` or `ATR_PERIOD` in `symbol_screener.py`
- The screener's ranking logic
- The universe: `UNIVERSE_FILE`, the `ORB_UNIVERSE_FILE` override, or the
  contents of whatever file they point at

If a parameter change looks warranted, write the hypothesis down, then test it
against the **full Kite history** (which lifts Yahoo's 60-day cap, so years are
available) rather than against the last few sessions. That is where an edge
estimate comes from. The forward run is for validating execution, not for
searching parameter space.

## Daily routine

```bash
python paper_broker.py plan       # 10:05 IST, after the 45m range closes
python paper_broker.py resolve    # 15:45 IST, after the close
python paper_report.py            # review
python paper_broker.py status     # freeze integrity + unresolved plans
```

A plan is committed once and never rewritten. Resolution replays the session
against the levels recorded that morning, never against freshly computed ones.
That single rule is what makes this a forward test rather than a backtest with
extra steps.

## Reading the report

The report leads with a 95% confidence interval and states plainly when the
result is indistinguishable from zero. **When it says that, it means it.** A
positive P&L whose interval straddles zero is not evidence of an edge, and the
correct response is to keep collecting sessions, not to change anything.
