$ErrorActionPreference = 'Stop'
$env:WIND_DATA_MODE = 'demo'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
& .venv\Scripts\python.exe -m backend.worker --daily
if ($LASTEXITCODE -ne 0) { throw 'Demo daily update failed.' }
