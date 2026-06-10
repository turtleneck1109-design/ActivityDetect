@echo off
cd /d "%~dp0"
if not exist data mkdir data
if "%SJTU_API_KEY%"=="" if not exist "data\sjtu_api_key.txt" (
    echo SJTU API key is not configured.
    echo.
    echo Set the SJTU_API_KEY environment variable, or create:
    echo data\sjtu_api_key.txt
    echo.
    echo Put only the API key text in that file.
    echo.
    pause
    exit /b 1
)
echo Generating yesterday's work report and AI summary...
python work_tracker.py summary --day yesterday
if errorlevel 1 (
    echo.
    echo The AI summary could not be generated. See the messages above.
    echo.
    pause
    exit /b 1
)
echo.
echo Done. The summary is in the data folder:
echo - ai_summary_YYYY-MM-DD.txt
echo.
pause
