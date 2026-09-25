@echo off
cd /d "%~dp0"
netstat -ano | findstr ":8501 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8501 " ^| findstr "LISTENING"') do taskkill /f /pid %%p >nul 2>&1
    echo website stopped.
) else (
    echo website is not running.
)
