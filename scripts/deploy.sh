#!/bin/sh
# ============================================================
# stock_analyze 一键部署 (macOS / Linux 通用)
# 用法: ./scripts/deploy.sh    (或通过 deploy_mac.sh / deploy_linux.sh 调用)
# 流程: 检查Python → 建.venv → 装依赖 → 迁移 → 超管(如无) → 询问自启 → 启动 → 健康检查
# ============================================================
set -e
cd "$(dirname "$0")/.."

echo "[1/7] 检查 Python (需 3.10+)…"
PY_SYS=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY_SYS="$c"; break; fi
done
[ -z "$PY_SYS" ] && { echo "  ✗ 未找到 Python 3.10+, 请先安装"; exit 1; }
echo "  ✓ $($PY_SYS -c 'import sys;print(sys.version.split()[0])')"

echo "[2/7] 创建虚拟环境 .venv…"
[ -x ".venv/bin/python" ] && echo "  · 已存在, 跳过" || $PY_SYS -m venv .venv
PY=".venv/bin/python"

echo "[3/7] 安装依赖(首次约1~3分钟)…"
$PY -m pip install -q --upgrade pip
$PY -m pip install -q -r requirements.txt
echo "  ✓ 依赖就绪"

echo "[4/7] 数据库迁移…"
SA_NO_JOBS=1 FLASK_APP="server:create_app()" $PY -m flask db upgrade
echo "  ✓ 数据库就绪"

echo "[5/7] 检查管理员账号…"
if "$PY" scripts/check_admin.py; then
    echo "  · 已有账号, 跳过"
else
    echo "  · 尚无任何账号, 开始创建超级管理员:"
    $PY -m server.bootstrap --username admin
fi

echo "[6/7] 开机自启(可选)…"
printf "    注册开机自启? (y/N): "
read AUTO
if [ "$AUTO" = "y" ] || [ "$AUTO" = "Y" ]; then
    sh scripts/service.sh install || echo "  ⚠ 自启安装失败, 可稍后手动执行 scripts/service.sh install"
fi

echo "[7/7] 启动服务…"
sh scripts/service.sh start

echo ""
echo "============================================================"
echo " 部署完成!  http://127.0.0.1:8501"
echo " 管理端 /admin/login · 用户行情页 /mkt"
echo " 服务管理: scripts/service.sh start|stop|status|restart"
echo " 运行日志: data/service.log"
echo "============================================================"
