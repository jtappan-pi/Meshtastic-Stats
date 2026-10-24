@echo off
title RAK4631 Meshtastic Dashboard
color 0A

echo.
echo  RAK4631 Meshtastic Health Dashboard
echo  WisMesh Pocket v2 - 915 MHz
echo  ====================================
echo.

:: Python used to CREATE .venv-win (only matters on the first run).
:: Leave empty to auto-detect, or point it at a python.exe, e.g.
::   set BASE_PYTHON=%USERPROFILE%\anaconda3\python.exe
set BASE_PYTHON=

:: Run from this script's folder. cmd.exe cannot use a \\wsl.localhost\ UNC path as
:: its working directory, so pushd maps it to a temporary drive letter.
pushd "%~dp0" || (
    echo  [ERROR] Could not change to %~dp0
    pause
    exit /b 1
)

:: Windows virtual environment lives inside the project folder (.venv-win).
:: The Linux side uses .venv; both are git-ignored.
set VENV=%CD%\.venv-win
set PY=%VENV%\Scripts\python.exe

if exist "%PY%" goto :have_venv

echo  [INFO] No .venv-win yet - looking for a Python to build it with...
call :find_python
if "%BASE_PYTHON%"=="" (
    echo  [ERROR] No working Python found. Tried, in order:
    echo            BASE_PYTHON ^(set at the top of this file^)
    echo            py -3
    echo            %USERPROFILE%\anaconda3\python.exe  and  miniconda3
    echo            %LOCALAPPDATA%\Programs\Python\Python3*\python.exe
    echo          Install Python from python.org, or set BASE_PYTHON to your python.exe.
    popd
    pause
    exit /b 1
)
echo  [INFO] Using %BASE_PYTHON%
echo  [INFO] Creating virtual environment at %VENV% - slow over the WSL share, be patient...
"%BASE_PYTHON%" -m venv "%VENV%"
if not exist "%PY%" (
    echo  [ERROR] Could not create the virtual environment.
    popd
    pause
    exit /b 1
)

:have_venv
:: Check if meshtastic is installed; install if not
"%PY%" -c "import meshtastic" >nul 2>&1
if errorlevel 1 (
    echo  [INFO] Installing dependencies - several minutes over the WSL share...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo  [ERROR] Failed to install dependencies.
        popd
        pause
        exit /b 1
    )
)

echo  [INFO] Starting dashboard server...
echo  [INFO] Open http://localhost:5000 in your browser
echo.
echo  REMINDER: Turn off Bluetooth on your Android phone before clicking
echo             "Poll Radio" in the dashboard. Turn it back on after.
echo.

:: Launch browser after a short delay
start /b "" cmd /c "timeout /t 2 >nul && start http://localhost:5000"

:: Run the server
"%PY%" server.py

popd
pause
exit /b 0

:: ---------------------------------------------------------------------------
:: find_python: set BASE_PYTHON to the first interpreter that actually runs.
:: The bare "python" command is deliberately not tried: on a machine without
:: Python it is the Microsoft Store stub, which only prints a misleading message.
:: ---------------------------------------------------------------------------
:find_python
if not "%BASE_PYTHON%"=="" (
    call :probe && goto :eof
    echo  [WARN] BASE_PYTHON does not run: %BASE_PYTHON% - auto-detecting instead
)
set BASE_PYTHON=

:: 1. py launcher, resolved to the real python.exe it would use
for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "BASE_PYTHON=%%P"
call :probe && goto :eof

:: 2. Anaconda / Miniconda base interpreter
set "BASE_PYTHON=%USERPROFILE%\anaconda3\python.exe"
call :probe && goto :eof
set "BASE_PYTHON=%USERPROFILE%\miniconda3\python.exe"
call :probe && goto :eof

:: 3. python.org per-user installs (a folder without python.exe is a broken remnant and is skipped)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    set "BASE_PYTHON=%%D\python.exe"
    call :probe && goto :eof
)

set BASE_PYTHON=
goto :eof

:: probe: succeed only if BASE_PYTHON names an existing file that runs
:probe
if "%BASE_PYTHON%"=="" exit /b 1
if not exist "%BASE_PYTHON%" exit /b 1
"%BASE_PYTHON%" -c "import sys" >nul 2>&1
exit /b
