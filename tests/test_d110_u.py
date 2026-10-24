# -*- coding: utf-8 -*-
"""D1-10 U1/U2 测试: scan_gaps交易日基线/窗口/API校验/repair任务/progress解析"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


@pytest.fixture()
def mkt(tmp_path, monkeypatch):
    """夹具库: 近5个交易日, index/pool/sw_breadth全有, sw_pool缺2日, mood缺1日"""
    import datetime as dt
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    days = ["2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-23"]
    ymd = [d.replace("-", "") for d in days]
    conn.executescript("""
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, up_count INTEGER);
      CREATE TABLE sector_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE sector_pool_stat(date TEXT, code TEXT, zt_count INTEGER);
      CREATE TABLE sector_fundflow(code TEXT, date TEXT, main_net REAL);
      CREATE TABLE margin_daily(date TEXT PRIMARY KEY, rzrqye REAL);
      CREATE TABLE sw_daily(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_index_comp(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_breadth(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_pool(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_fflow(index_code TEXT, date TEXT, x REAL);
    """)
    for d in ymd:
        conn.execute("INSERT INTO calendar VALUES(?,1)", (d,))
    for d in days:                                    # index 全有
        conn.execute("INSERT INTO index_daily VALUES('1.000001',?,3000)", (d,))
    # mood(=pool) 缺最后一天; breadth列有 → breadth资产基于up_count缺同日
    for d in days[:-1]:
        conn.execute("INSERT INTO mood_raw VALUES(?,50,2300)", (d,))
    for d in days[:-2]:                              # sw_pool 缺2日
        conn.execute("INSERT INTO sw_pool VALUES('801010.SI',?,1)", (d,))
    for d in days:                                   # 其余sw全有
        conn.execute("INSERT INTO sw_breadth VALUES('801010.SI',?,10)", (d,))
        conn.execute("INSERT INTO sw_fflow VALUES('801010.SI',?,1e6)", (d,))
        conn.execute("INSERT INTO sw_daily VALUES('801010.SI',?,100)", (d,))
        conn.execute("INSERT INTO sw_index_comp VALUES('801010.SI',?,100)", (d,))
    conn.commit()
    conn.close()
    import server.market as M
    monkeypatch.setattr(M, "SENT_DB", p)
    monkeypatch.setattr(M, "_cfg_cache", None)
    import shutil
    cfgcore = tmp_path / "c"
    cfgcore.mkdir()
    shutil.copy(CORE / "config.yaml", cfgcore / "config.yaml")
    monkeypatch.setattr(M, "CORE", cfgcore)          # raw_daily.db不存在→跳过raw
    return M


def test_scan_gaps_trading_day_baseline(mkt):
    r = mkt.scan_gaps(9)                            # 窗口含10-16(今日10-24, 9自然日)
    assert r["trading_days"] == 5                    # 5交易日(周末不计)
    by = {a["code"]: a["missing"] for a in r["assets"]}
    assert by["sw_pool"] == ["2026-10-21", "2026-10-23"]
    assert by["pool"] == ["2026-10-23"]
    assert by["index"] == []                         # 全有
    assert by["sw_breadth"] == []


def test_scan_api_validation(server_app):
    r = server_app["client"].post("/admin/api/scan", json={"window": 14})
    assert r.status_code == 400
    r = server_app["client"].post("/admin/api/repair", json={"window": 99})
    assert r.status_code == 400


def test_repair_enqueues(server_app):
    from server import jobs
    r = server_app["client"].post("/admin/api/repair", json={"window": 30})
    assert r.status_code == 200
    item = jobs._q.get_nowait()
    assert item["kind"] == "repair" and item["target"] == "近30天"


def test_progress_parsed_from_log(server_app, monkeypatch):
    """U2a: _execute从日志[n/N]更新job.progress"""
    from server import jobs
    from server.models import Job
    runs = []

    def fake_run(cmd, append, cwd=None, env_extra=None):
        runs.append([str(c) for c in cmd])
        append("$ " + " ".join(runs[-1]))
        append("  [5/10] 30s")
        return 0
    monkeypatch.setattr(jobs, "_run", fake_run)
    # task_repair单趟全链: scan(真实库有缺口)→fetch+score+sector各一次
    fn = jobs.task_repair(7)
    with server_app["app"].app_context():
        rc = fn(lambda s: None)
    assert rc == 0
    fetch_n = sum(1 for r in runs if "fetch_daily.py" in " ".join(r))
    score_n = sum(1 for r in runs if "score.py" in " ".join(r))
    assert fetch_n == 1 and score_n == 1        # 单趟(旧版重复跑2-3次)
    with server_app["app"].app_context():
        def fn2(append):
            append("[3/10] x")
            return 0
        jobs._execute({"kind": "repair", "fn": fn2, "target": "t", "trigger": "manual"})
        j = Job.query.order_by(Job.id.desc()).first()
        assert j.progress == 30.0 and j.status == "done"
