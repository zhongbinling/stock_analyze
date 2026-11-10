# -*- coding: utf-8 -*-
"""任务运行中心: 排队登记簿/计划视图/暂停/心跳/API/页面接线"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture()
def jc(env_none=None):
    from server import jobs as J
    # 清登记簿与暂停态
    J._queue_log.clear()
    J.set_paused(False)
    return J


# ── 排队登记簿 ──

def test_queue_log_register_and_snapshot(jc):
    """enqueue登记(seq/kind/target/时间), 出队执行后从快照消失"""
    jc.enqueue("test_kind", lambda append: 0, target="测试目标", trigger="manual")
    snap = jc.queue_snapshot()
    assert len(snap) == 1 and snap[0]["kind"] == "test_kind"
    assert snap[0]["seq"] >= 1 and snap[0]["state"] == "queued"
    # 模拟执行: Job表running态 → 快照排除
    # (无app上下文时running_kinds为空集, 快照保留 — 直接清登记簿模拟消费)
    jc._queue_log.clear()
    assert jc.queue_snapshot() == []


def test_queue_cap_still_enforced(jc):
    """容量上限不回归"""
    for i in range(25):
        ok, _ = jc.enqueue("k", lambda append: 0)
        if not ok:
            break
    assert jc.queue_size() <= 20


# ── 暂停 ──

def test_pause_toggle(jc):
    assert jc.is_paused() is False
    assert jc.set_paused(True) is True and jc.is_paused() is True
    assert jc.set_paused(False) is False


# ── 心跳 ──

def test_heartbeat_roundtrip(jc, tmp_path, monkeypatch):
    monkeypatch.setattr(jc, "HEARTBEAT_FILE", tmp_path / "hb.json")
    jc.heartbeat("news_driver")
    assert jc.heartbeat_alive("news_driver") is True
    assert jc.heartbeat_alive("ghost") is False
    # TTL过期
    import time
    beats = {t: time.time() - 99999 for t in ("stale",)}
    (tmp_path / "hb.json").write_text(json.dumps(beats), encoding="utf-8")
    assert jc.heartbeat_alive("stale") is False


def test_driver_heartbeat_writes(tmp_path, monkeypatch):
    """新闻驱动侧心跳: 写同一文件可被jobs侧读到(生产=repo/data/task_heartbeat.json)"""
    import fetch_news as FN
    import server.jobs as J
    # FN写BASE.parent/data/, J读HEARTBEAT_FILE — 指向同一文件
    monkeypatch.setattr(FN, "BASE", tmp_path / "core" / "x")
    monkeypatch.setattr(J, "HEARTBEAT_FILE", tmp_path / "core" / "data" / "task_heartbeat.json")
    FN._driver_heartbeat(True)
    assert J.heartbeat_alive("news_driver") is True
    FN._driver_heartbeat(False)
    assert J.heartbeat_alive("news_driver") is False


# ── 计划视图 ──

def test_scheduled_view_no_scheduler(jc):
    """调度器未启动 → 空列表不炸"""
    orig = jc._scheduler
    jc._scheduler = None
    try:
        assert jc.scheduled_jobs_view() == []
    finally:
        jc._scheduler = orig


# ── API ──

def test_center_api(server_app, monkeypatch):
    from server import jobs as J
    c = server_app["client"]
    monkeypatch.setattr(J, "queue_snapshot", lambda: [
        {"seq": 3, "kind": "backfill", "target": "x", "trigger": "manual",
         "at": "t", "state": "queued"}])
    monkeypatch.setattr(J, "scheduled_jobs_view", lambda: [
        {"id": "daily_update", "cron": "cron[...]", "next": "2026-11-11 16:35:00",
         "next_rel": "1天后"}])
    r = c.get("/admin/api/jobs/center").get_json()
    assert r["queued"][0]["seq"] == 3
    assert r["scheduled"][0]["id"] == "daily_update"
    assert r["paused"] is False
    # 暂停切换
    r2 = c.post("/admin/api/jobs/pause", json={"paused": True}).get_json()
    assert r2["paused"] is True
    assert J.is_paused() is True
    c.post("/admin/api/jobs/pause", json={"paused": False})
    assert J.is_paused() is False


def test_jobs_list_api(server_app):
    c = server_app["client"]
    r = c.get("/admin/api/jobs?limit=5")
    assert r.status_code == 200 and "jobs" in r.get_json()


# ── 页面接线 ──

def test_center_page_renders(server_app):
    assert server_app["client"].get("/admin/jobs").status_code == 200


def test_ui_wiring():
    import re
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "jobs_center.html").read_text(encoding="utf-8")
    for mk in ("jcRun", "jcQueue", "jcSched", "jcHist", "btnJcPause",
               "/admin/api/jobs/center", "/admin/api/jobs/pause", "排队中", "计划任务"):
        assert mk in html, f"缺: {mk}"
    dash = (REPO / "server" / "templates" / "admin" / "dashboard.html").read_text(encoding="utf-8")
    assert "jobs_center_page" in dash            # url_for跳转
    data = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "/admin/jobs" in data                 # 数据管理页跳转
    if shutil.which("node"):
        blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
        main = max(blocks, key=len)
        r = subprocess.run([shutil.which("node"), "-e",
                            "new Function(process.argv[1])", main],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        assert r.returncode == 0, f"JS语法错误: {(r.stderr or '')[:300]}"


def test_sched_registration_includes_new_ids():
    """清理列表含stock_*且新增sync_crons不受影响"""
    src = (REPO / "server" / "jobs.py").read_text(encoding="utf-8")
    assert '"stock_daily", "stock_weekly"' in src
    assert "def queue_snapshot" in src and "def heartbeat" in src
    assert "def scheduled_jobs_view" in src and "def set_paused" in src


def test_queue_log_lifecycle(jc, server_app):
    """review修复回归: 登记簿条目 queue→running(_execute开题)→done(收尾),
    40条窗口内跑完的任务不再滞留排队视图"""
    jc._queue_log.clear()
    jc.enqueue("probe_kind", lambda append: 0, target="探针", trigger="manual")
    assert len(jc.queue_snapshot()) == 1
    # 直接执行该条(worker同路径: _execute)
    from server import jobs as J
    item = J._q.get_nowait()
    with server_app["app"].app_context():
        J._execute(item)
    snap = jc.queue_snapshot()
    assert snap == []                          # 完结后不再显示排队
    assert any(e["state"] == "done" and e["kind"] == "probe_kind"
               for e in jc._queue_log)


def test_fmt_rel_timezone_aware(jc):
    """review修复回归: APScheduler的next_run_time是aware(tz) —
    naive datetime.now()相减抛TypeError(实测计划视图全表error行)"""
    from datetime import datetime, timezone, timedelta
    aware = datetime.now(timezone.utc) + timedelta(hours=2)
    assert "2小时" in jc._fmt_rel(aware) or "1小时" in jc._fmt_rel(aware)  # 转naive后正常
    naive = datetime.now() + timedelta(minutes=5)
    assert "分钟" in jc._fmt_rel(naive)


def test_center_running_has_log_and_elapsed(server_app):
    """运行中任务带实时日志流(尾部4k)+已运行时长"""
    from server.models import Job
    from datetime import datetime
    with server_app["app"].app_context():
        db = server_app["app"].extensions["sqlalchemy"]
        Job.query.filter_by(kind="probe_run").delete()
        db.session.commit()
        j = Job(kind="probe_run", target="探针", status="running",
                started_at=datetime.now(), log="第1行输出")
        db.session.add(j); db.session.commit()
        jid = j.id
    c = server_app["client"]
    r = c.get("/admin/api/jobs/center").get_json()
    run = next(x for x in r["running"] if x["id"] == jid)
    assert "第1行输出" in run["log"]
    assert run["elapsed_s"] is not None and run["elapsed_s"] >= 0
    with server_app["app"].app_context():
        db = server_app["app"].extensions["sqlalchemy"]
        Job.query.filter_by(id=jid).delete()
        db.session.commit()
