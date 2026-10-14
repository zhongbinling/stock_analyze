# -*- coding: utf-8 -*-
"""管理端路由: 管理登录 / 工作台 / 账号管理 (≥5/≥9)"""
from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_user

from server import db
from server.auth import audit, level_name, require_level
from server.forms import LoginForm, ResetPwForm, UserAddForm
from server.models import AdminLog, User

bp_admin = Blueprint("admin", __name__, url_prefix="/admin")


# ---------------------------------------------------------------- 管理登录

@bp_admin.route("/login", methods=["GET", "POST"])
def admin_login():
    if current_user.is_authenticated and current_user.level >= 5:
        return redirect(url_for("admin.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        from server.routes import _do_login
        user = _do_login(form, admin_mode=True)
        if user:
            return redirect(url_for("admin.dashboard"))
    return render_template("login.html", form=form, admin_mode=True)


# ---------------------------------------------------------------- 工作台

@bp_admin.get("/")
@require_level(5)
def dashboard():
    users_n = User.query.filter_by(deleted_at=None).count()
    logs = AdminLog.query.order_by(AdminLog.id.desc()).limit(5).all()
    return render_template("admin/dashboard.html", users_n=users_n, logs=logs)


# ---------------------------------------------------------------- 账号管理 (9)

def _get_user(uid) -> User:
    return User.query.filter_by(id=uid, deleted_at=None).first()


@bp_admin.route("/accounts", methods=["GET", "POST"])
@require_level(9)
def accounts():
    form = UserAddForm()
    if form.validate_on_submit():
        u = User(username=form.username.data.strip(),
                 nickname=form.nickname.data.strip() or form.username.data.strip(),
                 level=form.level.data)
        u.set_password(form.password.data)
        db.session.add(u)
        db.session.commit()
        audit("user_add", target=u.username, detail=level_name(u.level))
        flash(f"已添加账号 {u.username} ({level_name(u.level)})", "ok")
        return redirect(url_for("admin.accounts"))

    users = (User.query.filter_by(deleted_at=None)
             .order_by(User.id).all())
    reset_form = ResetPwForm(prefix="rst")
    logs = AdminLog.query.order_by(AdminLog.id.desc()).limit(20).all()
    return render_template("admin/accounts.html", form=form,
                           users=users, reset_form=reset_form, logs=logs,
                           level_name=level_name)


def _guard_self(uid) -> bool:
    if current_user.id == uid:
        flash("不能对当前登录账号执行该操作", "error")
        return False
    return True


@bp_admin.post("/accounts/<int:uid>/toggle")
@require_level(9)
def account_toggle(uid):
    u = _get_user(uid)
    if not u:
        flash("账号不存在", "error")
    elif _guard_self(uid):
        u.enabled = 0 if u.enabled else 1
        db.session.commit()
        audit("user_toggle", target=u.username, detail="启用" if u.enabled else "停用")
        flash(f"{u.username} 已{'启用' if u.enabled else '停用'}", "ok")
    return redirect(url_for("admin.accounts"))


@bp_admin.post("/accounts/<int:uid>/resetpw")
@require_level(9)
def account_resetpw(uid):
    u = _get_user(uid)
    form = ResetPwForm(prefix="rst")
    if not u:
        flash("账号不存在", "error")
    elif form.validate_on_submit():
        u.set_password(form.new.data)
        u.enabled = 1                     # 重置密码同时解除停用, 便于解救误操作
        db.session.commit()
        audit("user_resetpw", target=u.username)
        flash(f"已重置 {u.username} 的密码", "ok")
    else:
        flash("新密码至少8位且两次一致", "error")
    return redirect(url_for("admin.accounts"))


@bp_admin.post("/accounts/<int:uid>/delete")
@require_level(9)
def account_delete(uid):
    u = _get_user(uid)
    if not u:
        flash("账号不存在", "error")
        return redirect(url_for("admin.accounts"))
    if not _guard_self(uid):
        return redirect(url_for("admin.accounts"))
    n_root = User.query.filter_by(deleted_at=None, enabled=1).filter(User.level >= 9).count()
    if u.level >= 9 and n_root <= 1:
        flash("系统至少需要保留一个可用的超级管理员", "error")
        return redirect(url_for("admin.accounts"))
    from datetime import datetime, timezone
    u.deleted_at = datetime.now(timezone.utc)
    u.enabled = 0
    db.session.commit()
    audit("user_delete", target=u.username, detail=level_name(u.level))
    flash(f"已删除账号 {u.username}(软删除, 保留审计)", "ok")
    return redirect(url_for("admin.accounts"))
