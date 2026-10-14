# -*- coding: utf-8 -*-
"""权限体系: 等级映射 / require_level 装饰器 / 登录限速 / 审计"""
import time
from functools import wraps

from flask import abort, current_app, jsonify, redirect, request, url_for
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
    """等级门槛: 匿名且门槛>1 → 跳登录(带next); 已登录不足 → 403"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if current_user.is_authenticated:
                if current_user.level >= level:
                    return fn(*args, **kwargs)
                if request.path.startswith("/api/"):
                    return jsonify({"error": "forbidden", "need_level": level}), 403
                abort(403)
            if level <= 1:                      # 匿名(等级1)可访问
                return fn(*args, **kwargs)
            return redirect(url_for("bp.login", next=request.path))
        return wrapper
    return deco


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
