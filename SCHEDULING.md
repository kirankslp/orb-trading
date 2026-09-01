# Scheduling

Three substrates, and they are not interchangeable. Pick per run, not once.

| | Local cron | GitHub Actions | Claude Routine |
|---|---|---|---|
| Market data reachable | yes | yes | **no** in the Anthropic cloud env |
| Fires on time | yes | 5-20 min queueing delay | scheduled, cloud-dependent |
| Cost per run | none | free minutes | tokens |
| Needs the machine awake | yes | no | no |
| Adds judgment | no | no | **yes** |

The split that matters: **fetching data is infrastructure, interpreting results
is judgment.** Only the second one needs a model.

## Recommended

- **Pre-open, post-range, post-close** -> local cron. Deterministic scripts that
  format their own output. An LLM in this path adds latency and variance for
  nothing, and the post-range run is time-critical.
- **Weekly health** -> Claude Routine. Reads committed reports, compares across
  weeks, escalates in prose. No market data needed, so the cloud block is
  irrelevant.
- **GitHub Actions** -> backup and durable record. Runs even when your machine is
  off, and commits `reports/` so the weekly Routine has something to read.

---

## Local cron

```bash
git clone https://github.com/kirankslp/orb-trading && cd orb-trading
python3 -m venv .venv && .venv/bin/pip install yfinance pandas numpy
.venv/bin/python test_orb_backtest.py     # must print "all checks passed"
./scripts/run_daily.sh premarket          # confirm it works by hand first
```

The wrapper resolves the repo from its own path and does not rely on inherited
environment, which is the usual reason a job that works in a shell fails under
cron. Set `ORB_PYTHON` if you are not using `./.venv`.

Then `crontab -e`:

```cron
CRON_TZ=Asia/Kolkata
MAILTO=kiran.kesiraju@pm.me

15 8  * * 1-5  /full/path/to/orb-trading/scripts/run_daily.sh premarket
5  10 * * 1-5  /full/path/to/orb-trading/scripts/run_daily.sh plan
45 15 * * 1-5  /full/path/to/orb-trading/scripts/run_daily.sh close
0  9  * * 6    /full/path/to/orb-trading/scripts/run_daily.sh health
```

Use the absolute path; cron has no useful `PATH`. Output lands in `logs/` and a
dated copy in `reports/`. A failed run exits non-zero with the log tail on
stderr, so `MAILTO` gets you the error.

**`CRON_TZ` is Vixie cron (most Linux).** On macOS it is ignored, so either set
the machine to IST or convert the times yourself. Verify with `date`. macOS also
sleeps: use `launchd` with `StartCalendarInterval` instead if the machine is not
always on, since cron jobs missed while asleep never fire.

**Windows:** Task Scheduler with `bash scripts/run_daily.sh plan` under WSL, or
port the wrapper to PowerShell.

### Checking it took

```bash
crontab -l                  # jobs are registered
tail -f logs/$(date +%F)-plan.log
```

If nothing runs, it is almost always the absolute path, the interpreter, or the
timezone. Run the exact command line from the crontab in a fresh shell.

---

## GitHub Actions

`.github/workflows/daily-plan.yml`, already wired to the same four schedules.

**Scheduled workflows only fire from the default branch.** Nothing runs until
the branch is merged to `main`.

Each run publishes to the job summary, uploads artifacts, and commits CSVs to
`reports/`. Tests gate the run, so no numbers get published if the engine broke.
Trigger a run by hand from the Actions tab with `workflow_dispatch`.

If you also run local cron, both write `reports/`. Either let Actions own that
directory and keep local output in `logs/`, or `git pull --rebase` before
committing local reports.

---

## Claude Routine

Currently `trig_0167WQ54FWMcBufCX4LoEk7F`, Saturdays 10:00 IST, half an hour
after the Actions health run commits.

It reads `reports/` from the repo, checks the `AGENT.md` thresholds, and pushes
a notification. It does **not** fetch market data, because the Anthropic cloud
environment's network policy denies Yahoo, NSE and Upstox. The prompt says so
explicitly so a firing does not waste itself trying.

To run a Routine on your own machine instead, create it from Claude Code on that
machine; a device-bound Routine cannot be created from a remote session. Worth
it only if you want the weekly interpretation without depending on the cloud
environment. For the daily runs, prefer plain cron: no reason to put a model in
the path of a deterministic script.

Manage it with `/routines` in Claude Code, or ask Claude to list, pause, or
delete it by that trigger id.
