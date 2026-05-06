@echo off
REM Champions Comp Scraper — Windows Task Scheduler Setup
REM Run this file once as Administrator to register the daily 8 AM task.

SET PYTHON=C:\Users\JimmyDriver\AppData\Local\Python\pythoncore-3.14-64\python.exe
SET SCRIPT=C:\Users\JimmyDriver\OneDrive - crcapitaltx\CR Capital\comp-dashboard\scraper.py
SET TASK_NAME=Champions Comp Daily Scrape

echo Creating scheduled task: %TASK_NAME%
echo Runs daily at 8:00 AM CST using: %PYTHON%
echo.

schtasks /Create /TN "%TASK_NAME%" /TR "\"%PYTHON%\" \"%SCRIPT%\"" /SC DAILY /ST 08:00 /RU "%USERNAME%" /F

IF %ERRORLEVEL% EQU 0 (
    echo.
    echo SUCCESS: Task created. It will run every day at 8:00 AM.
    echo To verify: open Task Scheduler and look for "%TASK_NAME%"
    echo To run manually now: schtasks /Run /TN "%TASK_NAME%"
) ELSE (
    echo.
    echo ERROR: Could not create task. Try right-clicking this .bat file
    echo and selecting "Run as administrator".
)

pause
