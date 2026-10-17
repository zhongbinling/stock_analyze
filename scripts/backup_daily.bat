@echo off
chcp 65001 >nul
rem 每日备份入口(任务计划程序调用): 快照全部资产 + GFS清理
cd /d %~dp0..
if not exist backups mkdir backups
python coreackup.py daily >> backupsackup.log 2>&1
