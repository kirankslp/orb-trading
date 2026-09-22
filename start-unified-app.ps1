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
Start-Sleep -Seconds 2
Start-Process 'http://127.0.0.1:5174'
Write-Host 'Kite Research Desk is running at http://127.0.0.1:5174'
