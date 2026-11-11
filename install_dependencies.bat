@echo off
cd /d "%~dp0"
python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Installation complete. Run start.bat.
pause
exit /b 0
:failed
echo Installation failed. Check Python and network access.
pause
exit /b 1
