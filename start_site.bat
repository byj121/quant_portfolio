@echo off
cd /d "%~dp0"

echo [quant_portfolio] checking port 8501 ...
netstat -ano | findstr ":8501 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo old server found, stopping ...
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8501 " ^| findstr "LISTENING"') do taskkill /f /pid %%p >nul 2>&1
    timeout /t 2 /nobreak >nul
)

echo starting website as an independent process ...
start "quant_portfolio_server" .venv\Scripts\python.exe -m streamlit run app.py --server.headless true --server.port 8501
timeout /t 4 /nobreak >nul
start "" "http://localhost:8501"
echo website started: http://localhost:8501
echo tips: edit code then refresh browser to apply, no restart needed.
echo to stop, double-click stop_site.bat.
