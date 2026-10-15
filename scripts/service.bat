@echo off
chcp 65001 >nul
REM stock_analyze 服务管理 (Windows) —— 用法: service.bat start|stop|status|restart|install|uninstall
setlocal
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%ROOT%\scripts\svc.py" %*
endlocal & exit /b %ERRORLEVEL%
