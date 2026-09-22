param(
    [string]$Python = "$PSScriptRoot\.unified-venv\Scripts\python.exe"
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path $Python)) {
    throw "Python environment not found at $Python. Create it with: C:\\Users\\Kiran\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe -m venv .unified-venv; .\\.unified-venv\\Scripts\\python.exe -m pip install -r requirements.txt"
}
if (-not (Test-Path "$PSScriptRoot\web\node_modules")) {
    throw "Web dependencies are missing. Run: npm install --prefix web"
}

Start-Process -FilePath $Python -ArgumentList 'unified_api.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
Start-Process -FilePath 'npm.cmd' -ArgumentList 'run','dev','--','--host','127.0.0.1' -WorkingDirectory "$PSScriptRoot\web" -WindowStyle Hidden
function Wait-ForEndpoint {
    param([string]$Url, [string]$Name, [int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null
            return $true
        } catch {
            # A non-2xx status still means the server is listening and can answer.
            # Only a connection failure means it is not up yet.
            if ($_.Exception.Response) { return $true }
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Warning "$Name did not answer at $Url within $TimeoutSeconds seconds."
    return $false
}

# Both servers cold-start slower than any fixed sleep can predict: the API
# imports pandas, numpy and kiteconnect, and Vite builds its first bundle. The
# page needs /api/config for the Kite login URL, so opening the browser before
# the API answers is what used to leave that link dead.
$apiReady = Wait-ForEndpoint -Url 'http://127.0.0.1:8788/api/config' -Name 'The local API'
$webReady = Wait-ForEndpoint -Url 'http://127.0.0.1:5174' -Name 'The web UI'
if (-not $apiReady) {
    Write-Warning 'Open the Kite login link only after the API is up; the page offers a Retry button.'
}
if (-not $webReady) {
    throw 'The web UI never came up on 127.0.0.1:5174. Check the npm run dev output.'
}

Start-Process 'http://127.0.0.1:5174'
Write-Host 'Kite Research Desk is running at http://127.0.0.1:5174'
