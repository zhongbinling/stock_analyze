# -*- coding: utf-8 -*-
"""D1-4 测试: 中文target / logs.running进度解析 / interrupted三表标记 / 模板断言 / 回补快速失败"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


# ── server 侧 ──

def test_backfill_enqueue_uses_readable_target(server_app):
    c = server_app["client"]
    r = c.post("/admin/api/backfill/run", json={"dataset": "zt_ts", "start": "20200102"})
    assert r.status_code == 200
    from server import jobs
    item = jobs._q.get_nowait()
    assert item["kind"] == "backfill"
    assert item["target"] == "涨停结构·tushare回补(2020起)"      # 可读名, 非zt_ts


def test_logs_running_progress_parsed(server_app):
    import datetime as _dt
    from server.models import DataBackfillLog, Job
    with server_app["app"].app_context():
        s = server_app["app"].extensions["sqlalchemy"].session
        s.add(DataBackfillLog(dataset="breadth", label="涨跌家数", status="running",
                              started_at=_dt.datetime.now()))
        s.add(Job(kind="backfill", target="涨跌家数·tushare回补(2018起)", status="running",
                  log="[breadth-ts] 487 天\n  [300/487] 348s\n  [350/487] 404s",
                  started_at=_dt.datetime.now()))
        s.commit()
    j = server_app["client"].get("/admin/api/backfill/logs").get_json()
    assert j["running"] == {"target": "涨跌家数·tushare回补(2018起)",
                            "n": 350, "total": 487, "pct": 72}
    assert j["logs"][0]["status"] == "running"


def test_mark_interrupted_marks_all_three_tables(server_app):
    import datetime as _dt
    from server import jobs
    from server.models import BackupRun, DataBackfillLog, Job
    with server_app["app"].app_context():
        s = server_app["app"].extensions["sqlalchemy"].session
        s.add(Job(kind="update", status="running", log="x", started_at=_dt.datetime.now()))
        s.add(DataBackfillLog(dataset="breadth", status="running",
                              started_at=_dt.datetime.now()))
        s.add(BackupRun(btype="daily", status="running", started_at=_dt.datetime.now()))
        s.commit()
        jobs._mark_interrupted()
        assert Job.query.filter_by(status="running").count() == 0
        assert DataBackfillLog.query.filter_by(status="running").count() == 0
        assert BackupRun.query.filter_by(status="running").count() == 0
        j = Job.query.filter_by(kind="update").first()
        assert j.status == "fail" and "服务重启" in j.log
        d = DataBackfillLog.query.first()
        assert d.error == "服务重启, 任务中断"


def test_template_has_running_branch_and_cn_map():
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "JOB_TARGET_CN" in html and "zt_ts:" in html          # 存量代码→可读名映射
    assert "… 运行中" in html                                     # running三分支(不再是✗)
    assert "rg.pct" in html                                       # 进度百分比展示
    assert "bfLive" in html                                       # 运行态独立元素(不覆盖按钮反馈)


# ── core 侧: 回补循环快速失败 ──

@pytest.fixture()
def fastfail_env(tmp_path, monkeypatch):
    """临时库 + tushare.call 恒抛连接异常 + sleep置空"""
    import sqlite3
    import fetch_daily as FD
    import tushare_api

    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, up_count INTEGER,
        dt_count INTEGER, zb_rate REAL, lb_max INTEGER, lb2_count INTEGER, jl_rate REAL, zt_source TEXT);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
    """)
    for i, d in enumerate([f"2026-09-{dd:02d}" for dd in range(1, 11)]):   # 10个交易日待补
        conn.execute("INSERT INTO index_daily VALUES('1.000001',?,3000)", (d,))
    conn.commit()
    conn.close()

    calls = {"n": 0}

    class _ConnErr(Exception):
        pass

    def boom(api_name, fields="", **kw):
        calls["n"] += 1
        raise _ConnErr("NameResolution failed")

    monkeypatch.setattr(tushare_api, "has_token", lambda: True)
    monkeypatch.setattr(tushare_api, "call", boom)
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(p)))
    monkeypatch.setattr(FD.time, "sleep", lambda s: None)
    return {"calls": calls}


def test_backfill_zt_est_fastfail(fastfail_env, capsys):
    import fetch_daily as FD
    FD.backfill_zt_est("20260901")
    out = capsys.readouterr().out
    assert "连续5次异常" in out
    assert fastfill_done(fastfail_env) <= 6            # 第5次即断, 不再逐天重试


def fastfill_done(env):
    return env["calls"]["n"]
