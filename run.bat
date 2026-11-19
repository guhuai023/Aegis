@echo off
rem ============================================================
rem  Aegis launcher (ASCII only!)
rem  This file must stay ASCII: cmd.exe reads .bat in the OEM
rem  code page (936 on Chinese Windows), so any non-ASCII byte
rem  inside echoes/parentheses breaks command parsing.
rem  All Chinese messages and the real logic live in run.ps1,
rem  which is UTF-8 with BOM and reads fine in PowerShell.
rem ============================================================
setlocal
cd /d "%~dp0"

where powershell >nul 2>nul
if errorlevel 1 goto no_ps

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
if errorlevel 1 goto failed
goto done

:no_ps
echo.
echo [ERROR] PowerShell was not found on this machine.
echo         Manual steps (works without PowerShell):
echo           1. python -m venv .venv
echo           2. .venv\Scripts\python.exe -m pip install -r requirements.txt
echo           3. copy .env.example .env       ^(then fill DEEPSEEK_API_KEY if you have one^)
echo           4. .venv\Scripts\python.exe scripts\setup_demo_data.py
echo           5. .venv\Scripts\python.exe -m streamlit run app.py --server.port 8507
echo         Full guide: docs\HOW-TO-RUN.txt
goto done

:failed
echo.
echo [ERROR] Launcher failed. See the messages above.
echo         Full guide: docs\HOW-TO-RUN.txt
pause

:done
if "%AEGIS_NO_PAUSE%"=="1" goto :eof
echo.
pause