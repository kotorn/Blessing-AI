@echo off
title Blessing AI - Local Development Stack
echo ==========================================================
echo   Blessing AI - Local-First Development Stack
echo   Mode: PAPER (Simulated) ^| Cost: $0.00 (Offline)
echo ==========================================================

set PERSISTENCE_MODE=DISABLED
set EXECUTION_MODE=PAPER
set PORT=3000
set WORKER_URL=http://127.0.0.1:8000
set CONTROL_PLANE_URL=http://localhost:3000
set CONTROL_PLANE_ALLOW_UNAUTHENTICATED_LOCAL=true

echo.
echo [1/2] Launching Python Trading Worker on port 8000...
start "Blessing Trading Worker (Port 8000)" cmd /k "set PORT=8000&& set PERSISTENCE_MODE=DISABLED&& set EXECUTION_MODE=PAPER&& python -m apps.trading_worker.main"

echo [2/2] Launching Control Plane and Vite UI on port 3000...
timeout /t 2 /nobreak >nul
npm run dev
