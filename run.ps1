param(
    [int]$Port = 8077
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -r requirements.txt
}

Start-Process "http://127.0.0.1:$Port/"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port $Port
