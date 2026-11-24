@echo off
rem Starts Power BI Extraction Insights on http://127.0.0.1:5000 (creates .venv on first run).
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating the virtual environment in .venv ...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Could not create .venv. Install Python 3.11 or later from python.org and try again.
        exit /b 1
    )
)
call ".venv\Scripts\activate.bat"
python -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo Installing the requirements failed. See the messages above.
    exit /b 1
)
python app.py
