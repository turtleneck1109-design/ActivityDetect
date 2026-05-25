@echo off
cd /d "%~dp0"
echo Generating today's work report and activity chart...
echo.
choice /C YN /N /M "Open the generated report and chart when finished? [Y/N] "
if errorlevel 2 (
    python work_tracker.py report --day today
) else (
    python work_tracker.py report --day today --open
)
if errorlevel 1 (
    echo.
    echo The report could not be generated or opened. See the messages above.
    echo.
    pause
    exit /b 1
)
echo.
echo Done. Files are in the data folder:
echo - work_log_YYYY-MM-DD.txt
echo - work_chart_YYYY-MM-DD.svg
echo.
pause
