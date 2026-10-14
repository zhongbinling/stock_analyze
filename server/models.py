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
    created_at = db.Column(db.DateTime, default=_now)
    last_login_at = db.Column(db.DateTime)
    deleted_at = db.Column(db.DateTime)           # 软删除

    def set_password(self, raw: str):
        self.pwd_hash = bcrypt.generate_password_hash(raw).decode("utf-8")

    def check_password(self, raw: str) -> bool:
        return bool(self.pwd_hash) and bcrypt.check_password_hash(self.pwd_hash, raw)
