@echo off
chcp 65001 >nul
rem 卸载本机每日备份计划任务(B1服务化后由服务调度接管; 手动: schtasks /Delete /TN stock_analyze_backup /F)
schtasks /Delete /TN stock_analyze_backup /F
pause
