@echo off
REM Double-click me (Windows).
REM
REM Does the whole safe sequence in order and stops before anything is
REM written to your live Nookal account:
REM
REM   1. sets up Python and installs the one dependency
REM   2. runs the offline test suite
REM   3. checks the API key authenticates          (no writes)
REM   4. full rehearsal of every phase             (no writes)
REM
REM The real run, which does create records, has to be asked for explicitly
REM at the end. Nothing here writes to Nookal on its own.

setlocal
cd /d "%~dp0"

REM ---------------------------------------------------------------- python

echo.
echo ==^> Checking Python
set "PY="
for %%C in (py python) do (
    if not defined PY (
        %%C -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=%%C"
    )
)
if not defined PY (
    echo.
    echo   STOPPED: Python 3.9 or newer is not installed.
    echo   Install it from https://www.python.org/downloads/ and run this again.
    echo   Tick "Add Python to PATH" in the installer.
    goto :end
)
%PY% --version

REM ------------------------------------------------------------ dependency

echo.
echo ==^> Setting up the environment
if not exist .venv (
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo   STOPPED: Could not create the Python environment.
        goto :end
    )
)
set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo.
    echo   STOPPED: The Python environment looks broken.
    echo   Delete the .venv folder and run this again.
    goto :end
)
"%VENV_PY%" -m pip install --quiet --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo   STOPPED: Could not install the 'requests' package. Are you online?
    goto :end
)
echo   OK  ready

REM ---------------------------------------------------------------- config

echo.
echo ==^> Checking for your API key
if not exist nookal_config.json (
    echo.
    echo   STOPPED: nookal_config.json not found in this folder.
    echo.
    echo   Copy config.example.json to nookal_config.json, then open it and
    echo   replace PASTE-YOUR-NOOKAL-API-KEY-HERE with your key from Nookal
    echo   Practice Setup. Keep the quotes around it.
    goto :end
)
"%VENV_PY%" -c "import json,sys; k=json.load(open('nookal_config.json')).get('api_key',''); sys.exit(0 if k and 'PASTE-YOUR' not in k else 1)" 2>nul
if errorlevel 1 (
    echo.
    echo   STOPPED: nookal_config.json has no API key in it yet, or is not
    echo   valid JSON. Open it and replace PASTE-YOUR-NOOKAL-API-KEY-HERE
    echo   with your real key.
    goto :end
)
echo   OK  key found ^(not shown^)

REM ----------------------------------------------------------------- tests

echo.
echo ==^> Running the offline tests ^(about a minute, nothing leaves this machine^)
"%VENV_PY%" -m unittest discover -t . -s tests
if errorlevel 1 (
    echo.
    echo   STOPPED: The offline tests did not pass. Send me the output above
    echo   before going any further - do not run the live diagnostic yet.
    goto :end
)
echo   OK  all tests passed

REM --------------------------------------------------------------- phase 0

echo.
echo ==^> Checking your API key against Nookal ^(read-only, nothing is written^)
"%VENV_PY%" diagnostic.py --dry-run --yes --phases 0
if errorlevel 1 (
    echo.
    echo   STOPPED: Could not authenticate with Nookal. Check the key in
    echo   nookal_config.json, then run this again.
    goto :end
)
echo   OK  authenticated
echo   !!  If the line above says GET rather than POST, open
echo   !!  nookal_config.json and change "http_method": "POST" to "GET"
echo   !!  before continuing.

REM ------------------------------------------------------------- rehearsal

echo.
echo ==^> Full rehearsal - every phase, no writes
"%VENV_PY%" diagnostic.py --dry-run --yes

echo.
echo =======================================================
echo   OK  Rehearsal finished. Nothing was created in Nookal.
echo.
echo The report so far is in this folder: diagnostic_report.txt

REM ------------------------------------------------------------- live run

echo.
echo The real run creates records in your LIVE Nookal account:
echo   - a throwaway patient, ZZTEST APIDIAG
echo   - a case on that patient, plus fake Medicare details and a test PDF
echo   - optionally a test entry in the clinic-wide case Title dropdown
echo.
echo It asks before each one, and you can skip any of them.
echo You will need to delete the test patient in Nookal afterwards.
echo.
set "answer="
set /p "answer=Type LIVE and press Enter to do the real run, or just press Enter to stop: "

if /i "%answer%"=="LIVE" (
    echo.
    echo ==^> Real run - read each prompt before answering
    "%VENV_PY%" diagnostic.py
    echo.
    echo   OK  Done. Send diagnostic_report.txt back, then in Nookal:
    echo      1. delete the patient ZZTEST APIDIAG
    echo      2. remove 'ZZ DIAGNOSTIC TITLE TEST' from the case Title
    echo         dropdown if it was added
) else (
    echo.
    echo Stopped before the live run. Nothing was written to Nookal.
    echo Run this again and type LIVE when you are ready.
)

:end
echo.
pause
endlocal
