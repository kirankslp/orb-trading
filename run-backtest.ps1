<#
.SYNOPSIS
    Run the ORB backtest on Windows and save a timestamped transcript.

.DESCRIPTION
    Resolves the Kite credentials, checks the session token is usable BEFORE
    starting a long run, then executes orb_backtest.py over PERIOD (60 days by
    default) and writes both the console transcript and orb_trades.csv into a
    results folder.

    No orders are placed. The access token is never written to disk or into
    the transcript.

.EXAMPLE
    .\run-backtest.ps1
    Uses KITE_ACCESS_TOKEN, or access_token from the credentials file.

.EXAMPLE
    .\run-backtest.ps1 -RequestToken abc123
    Exchanges a fresh one-time request_token for a session. Tokens expire at
    06:00 IST, so this is the usual path first thing in the morning.

.EXAMPLE
    .\run-backtest.ps1 -Compare -RequestToken abc123
    Runs strategy_backtest.py instead: the frozen ORB, a 15-minute ORB, VWAP
    and Bollinger mean reversion and a 9/20 EMA crossover, all on the same
    picks, days and costs, with a paired comparison against the ORB.

.EXAMPLE
    .\run-backtest.ps1 -UniverseFile EQUITY_L.csv
    Runs against the full NSE equity list instead of the 30 hardcoded large
    caps. Much slower; see the warning the script prints.
#>
[CmdletBinding()]
param(
    [string]$Python = "$PSScriptRoot\.unified-venv\Scripts\python.exe",
    [string]$CredentialsFile,
    [string]$AccessToken,
    [string]$RequestToken,
    [string]$UniverseFile,
    [switch]$Compare,
    [string]$OutDir = "$PSScriptRoot\backtests"
)

$ErrorActionPreference = 'Stop'

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# --- interpreter -----------------------------------------------------------
if (-not (Test-Path $Python)) {
    Fail @"
Python environment not found at $Python
Create it with:
  python -m venv .unified-venv
  .\.unified-venv\Scripts\python.exe -m pip install -r requirements.txt
Or pass an interpreter: .\run-backtest.ps1 -Python C:\path\to\python.exe
"@
}

# --- credentials file ------------------------------------------------------
# Same precedence the Python side uses: explicit argument, then the
# environment, then the sibling algotrading project's creds.txt.
if (-not $CredentialsFile) { $CredentialsFile = $env:KITE_CREDENTIALS_FILE }
if (-not $CredentialsFile) {
    $algoRoot = if ($env:ALGO_TRADING_ROOT) { $env:ALGO_TRADING_ROOT }
                else { 'C:\Users\Kiran\Documents\ChatGPT\algotrading' }
    $CredentialsFile = Join-Path $algoRoot 'creds.txt'
}
if (-not (Test-Path $CredentialsFile)) {
    Fail @"
Credentials file not found: $CredentialsFile
It needs at least an api_key entry, and api_secret if you pass -RequestToken:
  api_key=xxxxxxxx
  api_secret=yyyyyyyy
Point somewhere else with -CredentialsFile, or set KITE_CREDENTIALS_FILE.
"@
}
$env:KITE_CREDENTIALS_FILE = $CredentialsFile

$apiKey = (Select-String -Path $CredentialsFile -Pattern '^\s*(kite_)?api_key\s*[=:]\s*(.+)$' |
           Select-Object -First 1).Matches.Groups[2].Value
if (-not $apiKey) { Fail "$CredentialsFile has no api_key entry." }
$apiKey = $apiKey.Trim()

# --- session token ---------------------------------------------------------
# Never echoed, never written to the transcript, never persisted.
if ($AccessToken)  { $env:KITE_ACCESS_TOKEN = $AccessToken }
if ($RequestToken) { $env:KITE_REQUEST_TOKEN = $RequestToken; $env:KITE_ACCESS_TOKEN = $null }

$haveToken = $env:KITE_ACCESS_TOKEN -or $env:KITE_REQUEST_TOKEN -or
             (Select-String -Path $CredentialsFile -Pattern '^\s*(kite_)?access_token\s*[=:]' -Quiet)

if (-not $haveToken) {
    Write-Host ''
    Write-Host 'No Kite session token found. Tokens expire at 06:00 IST.' -ForegroundColor Yellow
    Write-Host 'Open this, log in, then copy the request_token from the redirect URL:'
    Write-Host "  https://kite.zerodha.com/connect/login?v=3&api_key=$apiKey" -ForegroundColor Cyan
    $entered = Read-Host 'request_token (blank to abort)'
    if (-not $entered) { Fail 'No token supplied.' }
    $env:KITE_REQUEST_TOKEN = $entered.Trim()
}

# --- universe --------------------------------------------------------------
# UNIVERSE_FILE is None in symbol_screener.py, so the default pool is 30
# hardcoded large caps. At MAX_POSITIONS=20 that means trading most of the
# pool every day, which leaves the ranking doing very little work.
if ($UniverseFile) {
    if (-not (Test-Path (Join-Path $PSScriptRoot $UniverseFile)) -and -not (Test-Path $UniverseFile)) {
        Fail "Universe file not found: $UniverseFile"
    }
    $env:ORB_UNIVERSE_FILE = $UniverseFile   # read by symbol_screener.load_universe
    Write-Host "Universe override: $UniverseFile" -ForegroundColor Yellow
    Write-Host 'Kite is one request per instrument at 0.35s, so a 2565-name list' -ForegroundColor Yellow
    Write-Host 'takes ~15 minutes for daily bars alone, plus intraday per pick.' -ForegroundColor Yellow
}

# --- preflight -------------------------------------------------------------
# Authenticate before committing to a long run: a bad token should fail in
# seconds, not after fifteen minutes of fetching.
Write-Host ''
Write-Host 'Checking the Kite session...' -NoNewline
# Native commands that write to stderr raise NativeCommandError while
# ErrorActionPreference is 'Stop', which aborts on the first line of a Python
# traceback and throws the rest away. Relax it around every external call.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$check = & $Python -c @"
import sys, traceback
try:
    from kite_data import kite_client
    c = kite_client()
    p = c.profile()
    # A request_token is single-use and short-lived. This process has just
    # spent it, so hand the resulting session token to the backtest rather
    # than letting it try the same exchange again and fail.
    print('TOKEN ' + str(c.access_token))
    print('OK ' + str(p.get('user_name') or p.get('user_id') or ''))
except Exception:
    traceback.print_exc(); sys.exit(1)
"@ 2>&1
$ErrorActionPreference = $prevEAP
if ($LASTEXITCODE -ne 0) {
    Write-Host ' failed' -ForegroundColor Red
    $detail = ($check -join "`n")
    if ($detail -match 'TokenException|Token is invalid') {
        $detail += @"


A request_token is SINGLE-USE and expires within minutes. A token that has
already been exchanged, including by an earlier run of this script or by the
web app, cannot be reused. Get a fresh one:
  https://kite.zerodha.com/connect/login?v=3&api_key=$apiKey
then rerun with -RequestToken <new token>.
"@
    }
    Fail $detail
}

# Carry the live session forward and retire the spent request_token. The token
# is moved between processes by environment variable only: it is never echoed,
# never written to the transcript, and never persisted.
$token = ($check | Where-Object { $_ -match '^TOKEN ' } |
          Select-Object -First 1) -replace '^TOKEN ', ''
if ($token) {
    $env:KITE_ACCESS_TOKEN = $token
    $env:KITE_REQUEST_TOKEN = $null
}
$who = ($check | Where-Object { $_ -match '^OK ' } | Select-Object -First 1)
Write-Host " $who" -ForegroundColor Green

# --- run -------------------------------------------------------------------
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$log = Join-Path $OutDir "backtest_$stamp.txt"

$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$cfg = & $Python -c @"
import orb_backtest as ob, symbol_screener as sc
print(f'Rs{ob.DAY_BUDGET:,} over {ob.MAX_POSITIONS} slots = Rs{ob.slot_budget():,.0f}/slot | '
      f'{ob.PERIOD} of {ob.INTERVAL} bars | universe {len(sc.load_universe())} names')
"@ 2>&1
$ErrorActionPreference = $prevEAP
Write-Host ''
Write-Host "Config: $cfg"
Write-Host "Transcript: $log"
Write-Host ''

# Python writes its traceback to stderr. Merging that into the pipeline is
# what we want for the transcript, but it must not be treated as a terminating
# PowerShell error or the traceback is lost after its first line.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
if ($Compare) {
    $pyScript = 'strategy_backtest.py'
    $tradeLog = 'strategy_trades'
} else {
    $pyScript = 'orb_backtest.py'
    $tradeLog = 'orb_trades'
}
& $Python -X faulthandler $pyScript 2>&1 | Tee-Object -FilePath $log
$code = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

# Keep the trade log next to the transcript so a rerun cannot overwrite it.
$savedLog = Join-Path $OutDir "$($tradeLog)_$stamp.csv"
if (Test-Path "$PSScriptRoot\$tradeLog.csv") {
    Copy-Item "$PSScriptRoot\$tradeLog.csv" $savedLog
}

Write-Host ''
if ($code -ne 0) {
    Write-Host "Backtest exited with code $code." -ForegroundColor Red
    Write-Host "Last lines of $log :" -ForegroundColor Red
    if (Test-Path $log) { Get-Content $log -Tail 25 | ForEach-Object { Write-Host "  $_" } }
    exit $code
}
Write-Host 'Done.' -ForegroundColor Green
Write-Host "  transcript : $log"
Write-Host "  trade log  : $savedLog"
Write-Host ''
Write-Host 'Backtest only. No orders placed, no config changed.'
