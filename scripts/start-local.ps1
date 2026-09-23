# Blessing AI - Local Development Stack Launcher (Zero Cloud Cost)
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Blessing AI - Local-First Development Stack" -ForegroundColor Green
Write-Host "  Mode: PAPER (Simulated) | Cost: $0.00 (Offline)" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan

$env:PERSISTENCE_MODE = "DISABLED"
$env:EXECUTION_MODE = "PAPER"
$env:PORT = "3000"
$env:WORKER_URL = "http://127.0.0.1:8000"
$env:CONTROL_PLANE_URL = "http://localhost:3000"
$env:CONTROL_PLANE_ALLOW_UNAUTHENTICATED_LOCAL = "true"

Write-Host "`n[1/2] Launching Python Trading Worker on http://127.0.0.1:8000..." -ForegroundColor Yellow
$workerProcess = Start-Process pwsh -ArgumentList "-NoExit", "-Command", "`$env:PORT='8000'; `$env:PERSISTENCE_MODE='DISABLED'; `$env:EXECUTION_MODE='PAPER'; python -m apps.trading_worker.main" -PassThru

Write-Host "[2/2] Launching Control Plane & Vite UI on http://localhost:3000..." -ForegroundColor Yellow
Start-Sleep -Seconds 2

try {
    npm run dev
} finally {
    if ($workerProcess -and !$workerProcess.HasExited) {
        Write-Host "Stopping Trading Worker process..." -ForegroundColor Gray
        Stop-Process -Id $workerProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
