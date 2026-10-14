# -*- coding: utf-8 -*-
"""app.db 数据模型 (Flask-SQLAlchemy)

S0: users 雏形(首版迁移); S1 补认证相关, S2/S3 补 view_defs/view_runs/jobs/data_sources 等。
"""
from datetime import datetime, timezone

from flask_login import UserMixin

from server import bcrypt, db


def _now():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    nickname = db.Column(db.String(64))
    pwd_hash = db.Column(db.String(128))          # Flask-Bcrypt; 匿名预留 NULL
    level = db.Column(db.Integer, default=2, nullable=False)   # 权限等级 1~9
    enabled = db.Column(db.Integer, default=1, nullable=False)
    pref_theme = db.Column(db.String(8))          # light|dark, 账号级主题偏好
    created_at = db.Column(db.DateTime, default=_now)
    last_login_at = db.Column(db.DateTime)
    deleted_at = db.Column(db.DateTime)           # 软删除

    def set_password(self, raw: str):
        self.pwd_hash = bcrypt.generate_password_hash(raw).decode("utf-8")

    def check_password(self, raw: str) -> bool:
        return bool(self.pwd_hash) and bcrypt.check_password_hash(self.pwd_hash, raw)


class AdminLog(db.Model):
    """敏感操作审计: 管理登录/注册/账号增删启停/重置密码/改密"""
    __tablename__ = "admin_log"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer)
    actor = db.Column(db.String(64))              # 冗余用户名, 便于直读
    action = db.Column(db.String(32), nullable=False)
    target = db.Column(db.String(64))
    detail = db.Column(db.String(255))
    at = db.Column(db.DateTime, default=_now, index=True)
