@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run setup.bat first. & pause & exit /b 1)
for %%F in (app\__init__.py app\siblings.py app\variant_scraper.py) do if not exist "%%F" (echo Missing %%F - copy it from ten-asin\app & pause & exit /b 1)
if not exist ".env" (echo Missing .env - copy it from ten-asin. & pause & exit /b 1)
start "" http://localhost:2000
".venv\Scripts\python.exe" run.py
pause
