@echo off
chcp 65001 >nul
rem 注册每日18:30备份任务(独立于Web服务): schtasks卸载: /Delete /TN stock_analyze_backup /F
set TASKDIR=%~dp0
schtasks /Create /TN stock_analyze_backup /TR "%TASKDIR%backup_daily.bat" /SC DAILY /ST 18:30 /F
if %errorlevel%==0 (echo 已注册每日备份任务 stock_analyze_backup ^(18:30^)) else (echo 注册失败, 请以管理员重试)
pause
