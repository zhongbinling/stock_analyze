@echo off
chcp 65001 >nul
REM ============================================================
REM stock_analyze 一键部署 (Windows)
REM 用法: 双击或 cmd 运行 scripts\deploy_win.bat
REM 流程: 检查Python → 建.venv → 装依赖 → 迁移 → 超管(如无) → 询问自启 → 启动 → 健康检查
REM ============================================================
setlocal
cd /d "%~dp0.."

echo [1/7] 检查 Python (需 3.10+)…
python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" || (
    echo   ✗ 未找到 Python 3.10+ , 请从 python.org 安装并勾选 Add to PATH
    exit /b 1
)
for /f %%v in ('python -c "import sys;print(sys.version.split()[0])"') do echo   ✓ Python %%v

echo [2/7] 创建虚拟环境 .venv…
if exist ".venv\Scripts\python.exe" (
    echo   · 已存在, 跳过
) else (
    python -m venv .venv || (echo   ✗ venv 创建失败 & exit /b 1)
)
set "PY=.venv\Scripts\python.exe"

echo [3/7] 安装依赖(首次约1~3分钟)…
"%PY%" -m pip install -q --upgrade pip
"%PY%" -m pip install -q -r requirements.txt || (echo   ✗ 依赖安装失败, 检查网络 & exit /b 1)
echo   ✓ 依赖就绪

echo [4/7] 数据库迁移…
set "SA_NO_JOBS=1"
set "FLASK_APP=server:create_app()"
"%PY%" -m flask db upgrade || (echo   ✗ 迁移失败 & exit /b 1)
echo   ✓ 数据库就绪
set "SA_NO_JOBS="
set "FLASK_APP="

echo [5/7] 检查管理员账号…
"%PY%" scripts\check_admin.py && (
    echo   · 已有账号, 跳过
) || (
    echo   · 尚无任何账号, 开始创建超级管理员:
    call "%PY%" -m server.bootstrap --username admin || exit /b 1
)

echo [6/7] 开机自启(可选)…
echo     注册登录自启? 输入 y 回车确认, 直接回车跳过:
set /p AUTO=
if /i "%AUTO%"=="y" call scripts\service.bat install

echo [7/7] 启动服务…
call scripts\service.bat start || exit /b 1

echo.
echo ============================================================
echo  部署完成!  http://127.0.0.1:8501
echo  管理端 /admin/login · 用户行情页 /mkt
echo  服务管理: scripts\service.bat start^|stop^|status^|restart
echo  运行日志: data\service.log
echo ============================================================
endlocal
