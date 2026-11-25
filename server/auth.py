# -*- coding: utf-8 -*-
"""权限体系: 等级映射 / require_level 装饰器 / 登录限速 / 审计"""
import time
from functools import wraps

from urllib.parse import urlsplit

from flask import (abort, current_app, jsonify, redirect, render_template, request,
                   url_for)
from flask_login import current_user

LEVEL_NAMES = {1: "匿名访客", 2: "注册用户", 3: "高级用户(预留)",
               5: "运营", 7: "管理员", 9: "超级管理员"}
MANAGER_LEVELS = [(2, "注册用户"), (3, "高级用户(预留)"),
                  (5, "运营"), (7, "管理员"), (9, "超级管理员")]


def level_name(level: int) -> str:
    return LEVEL_NAMES.get(level, f"等级{level}")


# ---------------------------------------------------------------- 登录限速(进程内存, 单进程 waitress 适用)
_MAX_FAILS = 5
_LOCK_SECONDS = 600
_failures: dict = {}   # {username: [失败次数, 锁定截止时间戳]}


def rate_limit_left(username: str) -> int:
    """剩余锁定秒数; 0 表示允许尝试"""
    rec = _failures.get(username)
    if not rec:
        return 0
    fails, until = rec
    if until > time.time():
        return int(until - time.time())
    if until and until <= time.time():
        _failures.pop(username, None)
    return 0


def register_failure(username: str):
    rec = _failures.setdefault(username, [0, 0])
    rec[0] += 1
    if rec[0] >= _MAX_FAILS:
        rec[1] = time.time() + _LOCK_SECONDS
        rec[0] = 0


def clear_failure(username: str):
    _failures.pop(username, None)


# ---------------------------------------------------------------- 权限装饰器

def require_level(level: int):
    """等级门槛: 匿名且门槛>1 → 跳登录(带next), 页面脚本发的请求回 401 JSON; 已登录不足 → 403"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if current_user.is_authenticated:
                if current_user.level >= level:
                    return fn(*args, **kwargs)
                if "/api/" in request.path:       # 含 /admin/api/* 前缀的接口
                    return jsonify({"error": "forbidden", "need_level": level, "ok": False,
                                    "msg": f"权限不足, 该操作需要{level_name(level)}(等级{level})"}), 403
                abort(403)
            if level <= 1:                      # 匿名(等级1)可访问
                return fn(*args, **kwargs)
            if request.is_json:                 # 页面脚本发的请求(各页 api() 都带 JSON 头)
                return jsonify({"ok": False, "msg": LOGIN_LOST_MSG}), 401
            return redirect(url_for("bp.login", next=request.path))
        return wrapper
    return deco


# ---------------------------------------------------------------- 登录态 / 防跨站令牌失效时, 给人看得懂的说明

# 页面脚本的请求在未登录时不能回"跳转登录页": fetch 会跟着跳转拿到登录页的 HTML(状态码 200),
# 页面把它当成空对象 {} 往下走 — "数据更新"会显示"● 数据已更新"而实际什么都没做。
LOGIN_LOST_MSG = "未登录或登录已失效, 请刷新页面重新登录"

# Flask-WTF 拒绝令牌的原因(英文原文) → 给用户的说明。令牌写死在页面里, 与签发它的会话绑定;
# 过期只在 WTF_CSRF_TIME_LIMIT 被改回时限时才会出现(默认已取消, 见 create_app)。
# 这些说明显示在页面各处的状态行里, 保持一句话。
_CSRF_MSG = {
    "The CSRF token has expired.": "页面安全令牌已过期, 请刷新页面后重试",
    "The CSRF tokens do not match.": "登录状态已变化, 请刷新页面后重试",
    "The CSRF session token is missing.": "登录状态已变化, 请刷新页面后重新登录",
    "The CSRF token is missing.": "请求未带安全令牌, 请刷新页面后重试",
    "The CSRF token is invalid.": "安全令牌无效, 请刷新页面后重试",
}


def _referrer_path(referrer) -> str:
    """同来源的 Referer 只取其路径+查询串; 缺失、外站或不是站内路径 → 空串(防开放重定向:
    "//host" 与 "/\\host" 浏览器都会当成协议相对地址跳到外站)"""
    if not referrer:
        return ""
    try:
        u = urlsplit(referrer)
    except ValueError:                           # 非法URL(如残缺的IPv6括号)
        return ""
    p = u.path
    if (u.scheme not in ("http", "https") or u.netloc != request.host
            or "\\" in p or p.startswith("//")):
        return ""
    return (p or "/") + (("?" + u.query) if u.query else "")


def csrf_rejected_response(reason: str):
    """CSRF 校验没通过(CSRFError, 400)时的响应。

    框架默认回一整页 HTML: 页面脚本读不出原因, 只能显示"HTTP 400"。所以页面脚本发的请求
    (JSON 请求头, 或路径带 /api/)回 JSON + 中文说明 — 各页 api() 取的就是 msg;
    浏览器表单提交回站内样式的错误页, "返回"链接指回来源页(刷新后页面里是新令牌)。"""
    msg = _CSRF_MSG.get(reason, "请求未通过安全校验, 请刷新页面后重试")
    if request.is_json or "/api/" in request.path:
        return jsonify({"ok": False, "csrf": True, "msg": msg}), 400
    return render_template("errors/csrf.html", msg=msg,
                           back=_referrer_path(request.referrer) or url_for("bp.index")), 400


# ---------------------------------------------------------------- 审计

def audit(action: str, target: str = "", detail: str = ""):
    """写入审计日志; 未登录场景(如注册)actor 留空"""
    from server import db
    from server.models import AdminLog
    actor_id = current_user.id if current_user.is_authenticated else None
    actor = (current_user.username if current_user.is_authenticated else "")
    db.session.add(AdminLog(actor_id=actor_id, actor=actor, action=action,
                            target=target, detail=detail[:255]))
    db.session.commit()
