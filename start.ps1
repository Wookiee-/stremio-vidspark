# Runs the server in ONE window: Granian + addon in the foreground.
# Pure Python now — no sidecar process to manage.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-Addon {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8002/manifest.json" -TimeoutSec 3 -UseBasicParsing
    return $r.StatusCode -eq 200
  } catch { return $false }
}

if (Test-Addon) {
  Write-Host "addon already up on :8002 - nothing to do"
  exit 0
}

Write-Host "starting addon on :8002 ..."
Set-Location (Join-Path $Root "addon")
granian --interface asgi app.main:app --port 8002 --workers 1
