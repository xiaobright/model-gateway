@echo off
setlocal
cd /d "%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8317"
if not exist ".venv\Scripts\pythonw.exe" (
  echo [Model Gateway] .venv not found. Run:
  echo    python -m venv .venv
  echo    .venv\Scripts\python -m pip install -r requirements.txt
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.py --tray --port %PORT% --open
curl -s -m 2 http://127.0.0.1:%PORT%/health >nul 2>nul && (
  echo [Model Gateway] is up on port %PORT%
  exit /b 0
)
for /L %%i in (1,1,15) do (
  curl -s -m 2 http://127.0.0.1:%PORT%/health >nul 2>nul && goto :ok
  ping -n 2 127.0.0.1 >nul
)
echo [Model Gateway] Failed to start within ~30s.
echo Check data\crash.log, or run in console to see the error:
echo    .venv\Scripts\python.exe main.py --no-tray --port %PORT%
pause
exit /b 1
:ok
endlocal
exit /b 0
