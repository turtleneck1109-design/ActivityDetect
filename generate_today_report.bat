@echo off
cd /d "%~dp0"
echo Generating today's work report and activity chart...
echo.
python work_tracker.py report --day today
echo.
echo Done. Files are in the data folder:
echo - work_log_YYYY-MM-DD.txt
echo - work_chart_YYYY-MM-DD.svg
echo.
pause
