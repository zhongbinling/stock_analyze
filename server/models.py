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


# ================= S2: 任务 / 生成规则 / 数据源 / 设置 =================

class Job(db.Model):
    """任务历史: 数据更新 / 视图生成 (单工作线程串行执行)"""
    __tablename__ = "jobs"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(16), nullable=False)      # update | generate
    target = db.Column(db.String(64))                    # 生成规则名等
    trigger = db.Column(db.String(8), default="manual")  # manual | schedule
    status = db.Column(db.String(8), default="running", nullable=False)  # running|done|fail
    rc = db.Column(db.Integer)
    log = db.Column(db.Text)                             # 截断保留最近400行
    started_at = db.Column(db.DateTime)                  # 本地时间, 便于直读
    finished_at = db.Column(db.DateTime)
    duration_s = db.Column(db.Float)


class ViewDef(db.Model):
    """视图生成规则"""
    __tablename__ = "view_defs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    type = db.Column(db.String(16), default="sentiment")   # 视图类型
    env = db.Column(db.String(8), default="test", nullable=False)  # test | prod
    params_json = db.Column(db.Text)                       # weights/window/pool_min_periods/years
    auto_publish = db.Column(db.Integer, default=0, nullable=False)
    enabled = db.Column(db.Integer, default=1, nullable=False)
    cron = db.Column(db.String(32))                        # 预留: 规则级独立调度
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)


class ViewRun(db.Model):
    """视图生成记录 (规则删除后仍保留, 冗余规则名/环境/类型快照)"""
    __tablename__ = "view_runs"

    id = db.Column(db.Integer, primary_key=True)
    def_id = db.Column(db.Integer)
    def_name = db.Column(db.String(64))
    env = db.Column(db.String(8))                         # test | prod (快照)
    vtype = db.Column(db.String(16))                      # 视图类型快照(当前sentiment)
    status = db.Column(db.String(8), default="running", nullable=False)  # running|done|fail
    data_date = db.Column(db.String(10))                   # 行情数据日期
    file_path = db.Column(db.Text)                         # 相对仓库根, views_store/...
    size_kb = db.Column(db.Integer)
    generated_at = db.Column(db.DateTime)
    duration_s = db.Column(db.Float)
    published = db.Column(db.Integer, default=0, nullable=False)
    published_at = db.Column(db.DateTime)


class DataSource(db.Model):
    """数据源注册: 启停与 api_key(tushare 预留)"""
    __tablename__ = "data_sources"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), unique=True, nullable=False)
    name = db.Column(db.String(32), nullable=False)
    kind = db.Column(db.String(16))                        # pool | kline | breadth | reserve
    enabled = db.Column(db.Integer, default=1, nullable=False)
    api_key = db.Column(db.String(128))
    note = db.Column(db.String(128))


class AppSetting(db.Model):
    """调度策略等键值设置"""
    __tablename__ = "app_settings"

    skey = db.Column(db.String(32), primary_key=True)
    value = db.Column(db.String(64), nullable=False)


class BackupRun(db.Model):
    """备份执行记录 (B1): 服务内备份能力的历史/审计, 供数据管理页与运维系统查询"""
    __tablename__ = "backup_runs"

    id = db.Column(db.Integer, primary_key=True)
    btype = db.Column(db.String(12), nullable=False)       # daily | weekly | manual | pre-*
    trigger = db.Column(db.String(8), default="manual")    # manual | schedule
    status = db.Column(db.String(8), default="running", nullable=False)  # running|done|fail
    files_n = db.Column(db.Integer, default=0)
    total_mb = db.Column(db.Float, default=0)
    duration_s = db.Column(db.Float)
    error = db.Column(db.String(255))
    files_json = db.Column(db.Text)                        # [{file,asset,size}]
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)


class DataBackfillLog(db.Model):
    """数据补全日志 (D1-3): 按(数据集×日期)可反查——数据异常定位与清理重补的依据"""
    __tablename__ = "data_backfill_log"

    id = db.Column(db.Integer, primary_key=True)
    dataset = db.Column(db.String(16), nullable=False, index=True)   # zt_est|zt_ts|breadth|calendar
    label = db.Column(db.String(48))
    date_from = db.Column(db.String(10))
    date_to = db.Column(db.String(10))
    rows_written = db.Column(db.Integer, default=0)
    status = db.Column(db.String(8), default="running", nullable=False)  # running|done|fail
    trigger = db.Column(db.String(8), default="manual")
    days_json = db.Column(db.Text)                         # [{date,rows}…] 逐日写入明细
    duration_s = db.Column(db.Float)
    error = db.Column(db.String(255))
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
