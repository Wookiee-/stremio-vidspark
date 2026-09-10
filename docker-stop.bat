@echo off
REM Stop the Docker server.
where docker >nul 2>nul
if errorlevel 1 (
  echo Docker not found.
  exit /b 1
)
docker compose down
