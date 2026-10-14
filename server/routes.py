# -*- coding: utf-8 -*-
"""基础路由: 首页 / 健康检查 / 主题偏好 (S1 起补 login/register/me/mkt 等)"""
from flask import Blueprint, jsonify, render_template, session

bp = Blueprint("bp", __name__)


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "stock_analyze", "stage": "S0"})


@bp.post("/api/theme")
def api_theme():
    """主题偏好写入会话(账号级持久化在 S1 /me 接入)"""
    from flask import request
    t = (request.get_json(silent=True) or {}).get("theme")
    if t in ("light", "dark"):
        session["theme"] = t
    return jsonify({"ok": True, "theme": session.get("theme")})
