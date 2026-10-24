# -*- coding: utf-8 -*-
"""D1-10 A1/A2/A3 测试: 重试判定/计数上限/定时repair链/生成默认迁移"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


class _FakeSched:
    def __init__(self):
        self.jobs = []

    def add_job(self, fn, trigger, **kw):
        self.jobs.append({"fn": fn, "trigger": trigger, **kw})


def test_post_update_ok_no_retry(server_app, monkeypatch):
    """A1: 数据跟上/非交易日 → 不重试且清零计数"""
    from server import jobs
    with server_app["app"].app_context():
        jobs.set_setting("update_retry_n", "1")
        monkeypatch.setattr(jobs, "_scheduler", _FakeSched())
        monkeypatch.setattr("server.market.is_trading_day_today", lambda: False)
        jobs._post_update_check({"kind": "update"}, 0, None)
        assert jobs.get_setting("update_retry_n") == "0"
        assert jobs._scheduler.jobs == []


def test_post_update_fail_schedules_retry(server_app, monkeypatch):
    """A1: 交易日+数据没跟上 → +30min one-shot重试, 计数+1"""
    from server import jobs
    with server_app["app"].app_context():
        jobs.set_setting("update_retry_n", "0")
        fake = _FakeSched()
        monkeypatch.setattr(jobs, "_scheduler", fake)
        monkeypatch.setattr("server.market.is_trading_day_today", lambda: True)
        monkeypatch.setattr("server.market._mood_latest", lambda: "2026-10-20")  # 落后
        jobs._post_update_check({"kind": "update"}, 0, None)
        assert jobs.get_setting("update_retry_n") == "1"
        assert len(fake.jobs) == 1 and fake.jobs[0]["id"] == "update_retry_1"
        # 再失败一次 → 计数2, 仍可重试
        jobs._post_update_check({"kind": "update"}, 0, None)
        assert jobs.get_setting("update_retry_n") == "2"
        assert len(fake.jobs) == 2
        # 第三次(计数已达2) → 停止并清零
        jobs._post_update_check({"kind": "update"}, 0, None)
        assert jobs.get_setting("update_retry_n") == "0"
        assert len(fake.jobs) == 2


def test_post_update_rc_fail_also_retries(server_app, monkeypatch):
    """A1: rc=1(任务本身失败)同样触发重试"""
    from server import jobs
    with server_app["app"].app_context():
        fake = _FakeSched()
        monkeypatch.setattr(jobs, "_scheduler", fake)
        monkeypatch.setattr("server.market.is_trading_day_today", lambda: False)
        jobs._post_update_check({"kind": "update"}, 1, None)
        assert len(fake.jobs) == 1
        jobs.set_setting("update_retry_n", "0")


def test_scheduled_update_uses_repair(monkeypatch):
    """A2: 定时更新任务为task_repair(近1周)而非task_update"""
    import inspect
    from server import jobs
    src = inspect.getsource(jobs._sync_cron_jobs)
    assert "task_repair(7" in src and "task_update, trigger" not in src


def test_generate_cron_default_1800_and_migrate(server_app):
    """A3: 默认18:00; 旧默认17:00迁移, 自定义值不动"""
    from server import jobs
    assert jobs.DEFAULT_SETTINGS["generate_cron"] == "18:00"
    with server_app["app"].app_context():
        jobs.set_setting("generate_cron", "17:00")
        jobs.migrate_old_defaults()
        assert jobs.get_setting("generate_cron") == "18:00"
        jobs.set_setting("generate_cron", "16:45")       # 用户自定义
        jobs.migrate_old_defaults()
        assert jobs.get_setting("generate_cron") == "16:45"
