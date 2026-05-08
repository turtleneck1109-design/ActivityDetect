@echo off
cd /d "%~dp0"
echo Stopping background work tracker...
echo.
python work_tracker.py stop
echo.
echo Done. If the tracker was running, today's report has been generated.
echo.
pause
