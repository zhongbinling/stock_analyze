#!/bin/sh
# stock_analyze 服务管理 (macOS/Linux) —— 用法: ./service.sh start|stop|status|restart|install|uninstall
cd "$(dirname "$0")/.." || exit 1
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="python3"; command -v "$PY" >/dev/null 2>&1 || PY="python"; fi
exec "$PY" scripts/svc.py "$@"
