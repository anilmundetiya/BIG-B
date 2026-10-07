@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo   BIG-B setup  (run once per PC)
echo ============================================================

set "PY="
py -3.13 --version >nul 2>nul && set "PY=py -3.13"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python313\python.exe""
if not defined PY (
  echo Python 3.13 not found.
  echo Install Python 3.13 from https://www.python.org/downloads/  then run setup.bat again.
  echo Do NOT use 3.14 - pandas has no build for it yet.
  pause & exit /b 1
)
echo Using: %PY%

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment .venv ...
  %PY% -m venv .venv || (echo venv creation failed & pause & exit /b 1)
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo pip install failed & pause & exit /b 1)

set "MISSING="
for %%F in (app\__init__.py app\siblings.py app\variant_scraper.py) do if not exist "%%F" set "MISSING=1" & echo   MISSING: %%F  - copy it from ten-asin\app
if not exist ".env" echo   MISSING: .env  - copy your ten-asin .env here (see .env.example)

echo.
if defined MISSING (echo Setup done, but copy the missing files above before start.bat) else (echo Setup done. Double-click start.bat)
pause
