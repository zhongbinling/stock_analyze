# -*- coding: utf-8 -*-
"""公共路由: 首页/健康检查/主题/登录注册/账号信息/设置/行情数据(S3前占位)"""
from datetime import datetime, timezone

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_login import current_user, login_user, logout_user

from server import db
from server.auth import audit, clear_failure, level_name, rate_limit_left, register_failure, require_level
from server.forms import (LoginForm, NicknameForm, PasswordChangeForm,
                          RegisterForm, ThemeForm)
from server.models import User

bp = Blueprint("bp", __name__)


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/healthz")
def healthz():
    from server import __version__
    return jsonify({"status": "ok", "service": "stock_analyze",
                    "version": __version__, "stage": "S4"})


@bp.post("/api/theme")
def api_theme():
    """主题偏好写入会话; 已登录则同时写账号级偏好"""
    t = (request.get_json(silent=True) or {}).get("theme")
    if t in ("light", "dark"):
        session["theme"] = t
        if current_user.is_authenticated:
            current_user.pref_theme = t
            db.session.commit()
    return {"ok": True, "theme": session.get("theme")}


# ---------------------------------------------------------------- 认证

def _do_login(form: LoginForm, admin_mode: bool):
    """登录公共逻辑; admin_mode 要求等级>=5"""
    username = form.username.data.strip()
    left = rate_limit_left(username)
    if left > 0:
        flash(f"尝试次数过多, 请 {left} 秒后再试", "error")
        return None
    user = User.query.filter_by(username=username, deleted_at=None, enabled=1).first()
    if user and user.check_password(form.password.data):
        if admin_mode and user.level < 5:
            audit("admin_login_denied", target=user.username,
                  detail=f"等级{user.level}尝试管理登录")
            flash("该账号无管理权限", "error")
            return None
        prev = user.last_login_at
        login_user(user, remember=bool(form.remember.data))
        user.last_login_at = datetime.now(timezone.utc)
        db.session.commit()
        clear_failure(username)
        session["prev_login"] = prev.strftime("%Y-%m-%d %H:%M") if prev else ""
        if user.pref_theme:
            session["theme"] = user.pref_theme
        audit("login" if not admin_mode else "admin_login", target=user.username)
        return user
    register_failure(username)
    flash("账号或密码错误", "error")
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("bp.mkt"))
    form = LoginForm()
    if form.validate_on_submit():
        user = _do_login(form, admin_mode=False)
        if user:
            nxt = request.args.get("next")
            # 仅允许站内相对路径, 防开放重定向
            if not nxt or not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("bp.mkt")
            return redirect(nxt)
    return render_template("login.html", form=form, admin_mode=False)


@bp.route("/register", methods=["GET", "POST"])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        u = User(username=form.username.data.strip(),
                 nickname=form.nickname.data.strip() or form.username.data.strip(),
                 level=2)
        u.set_password(form.password.data)
        db.session.add(u)
        db.session.commit()
        login_user(u)
        u.last_login_at = datetime.now(timezone.utc)
        db.session.commit()
        session["prev_login"] = ""
        audit("register", target=u.username, detail="自助注册")
        flash("注册成功, 欢迎使用", "ok")
        return redirect(url_for("bp.mkt"))
    return render_template("register.html", form=form)


@bp.get("/logout")
def logout():
    if current_user.is_authenticated:
        audit("logout", target=current_user.username)
    logout_user()
    return redirect(url_for("bp.index"))


# ---------------------------------------------------------------- 用户侧页面

@bp.route("/me", methods=["GET", "POST"])
@require_level(2)
def me():
    nick_form = NicknameForm(prefix="nick")
    pw_form = PasswordChangeForm(prefix="pw")
    if nick_form.submit.data and nick_form.validate():
        current_user.nickname = nick_form.nickname.data.strip()
        db.session.commit()
        audit("nickname_change", target=current_user.username)
        flash("昵称已更新", "ok")
        return redirect(url_for("bp.me"))
    if pw_form.submit.data and pw_form.validate():
        if not current_user.check_password(pw_form.old.data):
            flash("当前密码不正确", "error")
        else:
            current_user.set_password(pw_form.new.data)
            db.session.commit()
            audit("password_change", target=current_user.username)
            flash("密码已修改", "ok")
        return redirect(url_for("bp.me"))
    return render_template("me.html", nick_form=nick_form, pw_form=pw_form,
                           prev_login=session.get("prev_login", ""))


@bp.route("/settings", methods=["GET", "POST"])
@require_level(1)
def settings():
    form = ThemeForm(theme=session.get("theme") or "light")
    if form.validate_on_submit():
        session["theme"] = form.theme.data
        if current_user.is_authenticated:
            current_user.pref_theme = form.theme.data
            db.session.commit()
        flash("设置已保存", "ok")
        return redirect(url_for("bp.settings"))
    return render_template("settings.html", form=form)


@bp.get("/mkt")
@require_level(1)
def mkt():
    """行情数据: 默认最新已发布产线情绪视图, ?rid= 指定, 空态引导"""
    return render_template("mkt.html")


@bp.get("/mkt/api/latest")
@require_level(1)
def mkt_latest():
    from server.models import ViewRun
    vtype = request.args.get("type", "sentiment")
    q = (ViewRun.query.filter_by(published=1, env="prod", status="done")
         .filter(ViewRun.vtype == vtype)
         .filter(ViewRun.file_path.isnot(None)))
    rid = request.args.get("rid", type=int)
    r = q.filter_by(id=rid).first() if rid else q.order_by(ViewRun.id.desc()).first()
    if not r:
        return jsonify({"view": None})
    return jsonify({"view": {"rid": r.id, "def_name": r.def_name or "",
                             "data_date": r.data_date or "",
                             "published_at": r.published_at.strftime("%Y-%m-%d %H:%M") if r.published_at else ""}})


@bp.get("/mkt/api/views")
@require_level(2)
def mkt_views():
    """历史视图列表(≥2): 已发布产线视图"""
    from server.models import ViewRun
    vtype = request.args.get("type", "sentiment")
    runs = (ViewRun.query.filter_by(published=1, env="prod", status="done")
            .filter(ViewRun.vtype == vtype, ViewRun.file_path.isnot(None))
            .order_by(ViewRun.id.desc()).limit(60).all())
    return jsonify({"views": [{"rid": r.id, "def_name": r.def_name or "",
                               "data_date": r.data_date or "",
                               "published_at": r.published_at.strftime("%Y-%m-%d %H:%M") if r.published_at else ""}
                              for r in runs]})


@bp.get("/mkt/view/<int:rid>")
@require_level(1)
def mkt_view(rid):
    """用户侧视图文件: 仅已发布的产线产物"""
    from flask import send_file as _send
    from server.routes_admin import _safe_run_file
    from server.models import ViewRun
    r = db.session.get(ViewRun, rid)
    # 防御性双检: 发布语义仅限prod(API层已强制), 此处兜底
    if not r or not r.published or r.env != "prod" or not r.file_path:
        abort(404)
    p = _safe_run_file(r)
    if not p:
        abort(404)
    return _send(p, mimetype="text/html")
