@echo off
REM Start the server via Docker (single container, addon 0.0.0.0:7002).
where docker >nul 2>nul
if errorlevel 1 (
  echo Docker not found. Install Docker Desktop first: https://www.docker.com/products/docker-desktop/
  exit /b 1
)
docker compose up --build -d
echo.
echo manifest: http://localhost:7002/manifest.json
