$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
}
& .venv\Scripts\python.exe -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ['fastapi','uvicorn','httpx','akshare']) else 1)"
if ($LASTEXITCODE -ne 0) {
    & .venv\Scripts\python.exe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
}
if (-not (Test-Path -LiteralPath 'node_modules')) {
    npm.cmd ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Node dependency installation failed.' }
}
npm.cmd run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
Write-Host 'Bond Workbench (AKShare / Wind MCP): http://127.0.0.1:8765'
Write-Host 'Press Ctrl+C to stop the local server.'
& .venv\Scripts\python.exe -m uvicorn backend.api:app --host 127.0.0.1 --port 8765
