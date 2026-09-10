# Stops the addon (whatever listens on :8002).
$lines = netstat -ano | Select-String "LISTENING" | Select-String ":8002"
foreach ($l in $lines) {
  $targetPid = ($l.ToString().Trim() -split "\s+")[-1]
  if ($targetPid -match "^\d+$" -and [int]$targetPid -gt 0) {
    Stop-Process -Id ([int]$targetPid) -Force -ErrorAction SilentlyContinue
    Write-Host "killed pid $targetPid on :8002"
  }
}
