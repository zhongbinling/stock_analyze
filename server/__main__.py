# -*- coding: utf-8 -*-
"""python -m server  →  waitress 启动 (win/mac/linux 通用)"""
from server import create_app, load_server_cfg
from waitress import serve


def main():
    cfg = load_server_cfg().get("server", {})
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 8501))
    app = create_app()
    print(f"stock_analyze 服务: http://{host}:{port}  (waitress, Ctrl+C 停止)")
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
