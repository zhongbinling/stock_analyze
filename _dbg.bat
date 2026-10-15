@echo on
echo === LINE 2 ===
REM stock_analyze 服务管理 (Windows) —— 用法: service.bat start|stop|status|restart|install|uninstall
echo === LINE 3 ===
setlocal
echo === LINE 4 ===
set "ROOT=%~dp0.."
echo === LINE 5 ===
set "PY=%ROOT%\.venv\Scripts\python.exe"
echo === LINE 6 ===
if not exist "%PY%" set "PY=python"
echo === LINE 7 ===
"%PY%" "%ROOT%\scripts\svc.py" %*
echo === LINE 8 ===
endlocal & exit /b %ERRORLEVEL%