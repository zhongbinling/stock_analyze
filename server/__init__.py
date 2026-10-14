# -*- coding: utf-8 -*-
"""stock_analyze Web 服务 (第一阶段)

应用工厂: Flask + SQLAlchemy + Login + Bcrypt + WTF(CSRF) + Migrate

启动: python -m server           (waitress)
开发: flask --app "server:create_app()" run
迁移: flask --app "server:create_app()" db upgrade
"""
import secrets
import sys
from pathlib import Path

import yaml
from flask import Flask, render_template
from flask_bcrypt import Bcrypt
from flask_login import AnonymousUserMixin, LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
SERVER_CFG = ROOT / "config" / "server.yaml"

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
bcrypt = Bcrypt()
csrf = CSRFProtect()


class AnonymousUser(AnonymousUserMixin):
    """匿名访客: 权限等级1(可看已发布行情数据)"""
    id = None
    username = "anonymous"
    nickname = "游客"
    level = 1


def load_server_cfg() -> dict:
    with open(SERVER_CFG, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _secret_key(cfg) -> str:
    """首启生成随机密钥并落盘, 重启后会话不失效"""
    kf = ROOT / cfg["data"]["secret_key_file"]
    kf.parent.mkdir(exist_ok=True)
    if kf.exists():
        return kf.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    kf.write_text(key, encoding="utf-8")
    return key


def create_app() -> Flask:
    cfg = load_server_cfg()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_secret_key(cfg),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{(ROOT / cfg['data']['app_db']).as_posix()}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        THEME_DEFAULT=cfg["app"].get("theme_default", "light"),
        SERVER_CFG=cfg,
    )
    app.json.ensure_ascii = False

    db.init_app(app)
    migrate.init_app(app, db)
    bcrypt.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "bp.login"          # S1 实现路由
    login_manager.anonymous_user = AnonymousUser

    from server.models import User  # noqa: F401  (确保模型在迁移前已注册)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from server.routes import bp
    from server.routes_admin import bp_admin
    app.register_blueprint(bp)
    app.register_blueprint(bp_admin)

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.context_processor
    def inject_globals():
        from flask import session
        from server.auth import level_name
        return {"theme": session.get("theme", app.config["THEME_DEFAULT"]),
                "app_name": "stock_analyze",
                "level_name": level_name}

    return app
