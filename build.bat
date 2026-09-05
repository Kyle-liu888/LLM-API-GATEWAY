@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1

echo ========================================
echo  LLM API Gateway - Build Script
echo ========================================

where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv is not installed or is not on PATH.
    goto :failed
)
where npm.cmd >nul 2>&1
if errorlevel 1 (
    echo ERROR: Node.js and npm are required to build the frontend.
    goto :failed
)
if not exist ".env" (
    echo ERROR: Create .env from .env.example and configure it before building.
    goto :failed
)

echo [1/3] Creating project environment and syncing build dependencies...
uv sync --locked --extra build
if errorlevel 1 goto :failed

echo [2/3] Building frontend...
pushd frontend
if errorlevel 1 goto :failed
if exist package-lock.json (
    call npm.cmd ci
) else (
    call npm.cmd install
)
if errorlevel 1 goto :frontend_failed
call npm.cmd run build
if errorlevel 1 goto :frontend_failed
popd

echo [3/3] Building executable...
uv run --locked --extra build pyinstaller copilot_proxy.spec --noconfirm --clean
if errorlevel 1 goto :failed
if not exist "dist\LLM-API-Gateway.exe" goto :failed

echo Build successful: %CD%\dist\LLM-API-Gateway.exe
popd
if /i not "%~1"=="--no-pause" pause
exit /b 0

:frontend_failed
popd
:failed
echo ERROR: Build failed. See the output above.
popd
if /i not "%~1"=="--no-pause" pause
exit /b 1
