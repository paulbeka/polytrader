@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Save this file in the polytrader repository root.
rem Usage: run_time_arbitrage.bat 600
rem Duration is in seconds. Each scanner opens in its own CMD window.

if "%~1"=="" goto usage
if not "%~2"=="" goto usage
set "DURATION=%~1"
for /f "delims=0123456789" %%A in ("%DURATION%") do goto usage

pushd "%~dp0"
if errorlevel 1 exit /b 1
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
set "CONFIG_DIR=%CD%\src\polytrader\bot\config"

if not exist "%PYTHON_EXE%" (
    echo ERROR: Python was not found at "%PYTHON_EXE%".
    echo Save this script in your repo root and create the .venv there.
    popd
    exit /b 1
)

"%PYTHON_EXE%" -c "import sys; sys.exit(0 if int(sys.argv[1]) > 0 else 1)" "%DURATION%"
if errorlevel 1 (
    popd
    goto usage
)

rem Check all five configs before starting any scanner.
set "MISSING="
for %%C in (
    hormuz-normalisation
    russia-ukraine-ceasefire
    openai-millennium-prize
    us-iran-ceasefire-continuation
    iranian-blockade-end
) do (
    if not exist "%CONFIG_DIR%\%%C.toml" (
        echo ERROR: Missing "%CONFIG_DIR%\%%C.toml".
        set "MISSING=1"
    )
)
if defined MISSING (
    popd
    exit /b 1
)

echo Starting five scanners in parallel for %DURATION% seconds each...
set "LAUNCH_FAILED="
for %%C in (
    hormuz-normalisation
    russia-ukraine-ceasefire
    openai-millennium-prize
    us-iran-ceasefire-continuation
    iranian-blockade-end
) do call :launch %%C

popd
if defined LAUNCH_FAILED (
    echo ERROR: At least one window could not be launched. Check the other windows.
    exit /b 1
)
echo Launch requests sent. Check each window for scanner startup or validation errors.
echo Windows remain open after the scanners finish so you can inspect their output.
echo To stop a scanner early, press Ctrl+C in its window.
exit /b 0

:launch
rem START returns immediately; no /WAIT, so all five scanners run concurrently.
rem CMD /K keeps the window open after Python exits. -u streams Python output.
start "Time arbitrage: %~1" "%ComSpec%" /D /K ""%PYTHON_EXE%" -u -m polytrader.bot.time_arbitrage --config "%CONFIG_DIR%\%~1.toml" --duration %DURATION%"
if errorlevel 1 set "LAUNCH_FAILED=1"
exit /b 0

:usage
echo Usage: %~nx0 DURATION_SECONDS
echo Example: %~nx0 600
echo Specify a positive whole number of seconds.
exit /b 2
