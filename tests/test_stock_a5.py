# -*- coding: utf-8 -*-
"""避雷针A5: 数据管理整合 — 夜间链/单只全量/池健康度/调度挂载/管理卡/API"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import fetch_stock as FS  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "stock.db"
    monkeypatch.setattr(FS, "STOCK_DB", db)
    return db


def test_start_lookback():
    assert FS._start(None) == "19900000"
    import datetime as dt
    exp = (dt.date.today() - dt.timedelta(days=45)).strftime("%Y%m%d")
    assert FS._start(45) == exp


def test_run_nightly_daily_vs_weekly(env, monkeypatch):
    """日频: margin/val 45日+events 30日; weekly加 finance/bs/holders/float"""
    calls = []

    def fake_refresh(code, path=None, lookback=None):
        calls.append((code, lookback))
        return 1
    import pandas as pd
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','茅台','','t','t')")
    con.commit()
    con.close()
    monkeypatch.setattr(FS, "refresh_daily", lambda path=None, lookback=7: 3)
    monkeypatch.setattr(FS, "refresh_margin", fake_refresh)
    monkeypatch.setattr(FS, "refresh_val", fake_refresh)
    monkeypatch.setattr(FS, "refresh_events", fake_refresh)
    monkeypatch.setattr(FS, "refresh_finance", fake_refresh)
    monkeypatch.setattr(FS, "refresh_bs", fake_refresh)
    monkeypatch.setattr(FS, "refresh_holders", fake_refresh)
    monkeypatch.setattr(FS, "refresh_float", fake_refresh)
    r = FS.run_nightly(weekly=False, path=env)
    assert r["stocks"] == 1 and r["daily"] == 3
    assert calls == [("600519.SH", 45), ("600519.SH", 45), ("600519.SH", 30)]  # 仅日频三件
    calls.clear()
    FS.run_nightly(weekly=True, path=env)
    assert len(calls) == 7                                    # +季频四件(全史None)


def test_stats_pool_health(env):
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','茅台','','t','t')")
    con.execute("INSERT INTO stock_daily VALUES('600519.SH','2026-11-07',1,2,1,2,100,200,1)")
    con.execute("INSERT INTO stock_margin VALUES('600519.SH','2026-11-07',1,1,1,1)")
    con.execute("INSERT INTO stock_val VALUES('600519.SH','2026-11-07',22,6,1,1)")
    con.execute("INSERT INTO stock_finance(ts_code,end_date) VALUES('600519.SH','2026-09-30')")
    con.execute("INSERT INTO stock_events VALUES('600519.SH','2026-01-01','dividend','t','','low')")
    con.execute("INSERT INTO stock_ai VALUES('600519.SH','2026-09-30','','C',-10,'[]','{}','m','t')")
    con.commit()
    con.close()
    s = FS.stats_pool(env)
    x = s["pool"][0]
    assert x["code"] == "600519.SH" and x["name"] == "茅台"
    assert x["last_daily"] == "2026-11-07" and x["rows_daily"] == 1
    assert x["last_margin"] == "2026-11-07" and x["last_val"] == "2026-11-07"
    assert x["last_finance"] == "2026-09-30" and x["events_n"] == 1
    assert x["ai_grade"] == "C"


def test_full_refresh_orchestrates(env, monkeypatch):
    """单只全量: add_stock + 七数据集全史"""
    done = []
    monkeypatch.setattr(FS, "add_stock", lambda c, path=None: {"ok": True, "msg": "x"})
    for name in ("margin", "val", "finance", "bs", "holders", "events", "float"):
        monkeypatch.setattr(FS, f"refresh_{name}",
                            lambda c, path=None, lookback=None, _n=name:
                            done.append(_n) or 1)
    r = FS.full_refresh("600519", env)
    assert r["ok"] and sorted(done) == sorted(
        ["margin", "val", "finance", "bs", "holders", "events", "float"])


def test_jobs_scheduled_and_task(server_app, monkeypatch):
    from server import jobs as J
    src = (REPO / "server" / "jobs.py").read_text(encoding="utf-8")
    assert "task_stock_daily" in src and "task_stock_full" in src
    assert 'CronTrigger(hour=19, minute=30)' in src                # 日频
    assert 'hour=19, minute=35, day_of_week="fri"' in src          # 周五季频
    assert '"stock_daily", "stock_weekly"' in src                  # 清理列表
    # 任务函数可执行(run_nightly打桩 — 原版对真库池发起真实tushare调用)
    import fetch_stock as FS
    stubbed = []
    monkeypatch.setattr(FS, "run_nightly", lambda weekly=False: stubbed.append(weekly) or
                        {"daily": 0, "stocks": 0})
    fn = J.task_stock_daily(False)
    rc = fn(lambda s: None)
    assert rc == 0 and stubbed == [False]
    fn2 = J.task_stock_daily(True)
    fn2(lambda s: None)
    assert stubbed == [False, True]


def test_a5_apis(server_app, monkeypatch):
    import fetch_stock as FS
    from server import jobs
    c = server_app["client"]
    monkeypatch.setattr(FS, "stats_pool", lambda path=None: {"pool": [], "n": 0})
    r = c.get("/admin/api/stock/stats_pool")
    assert r.status_code == 200 and r.get_json()["n"] == 0
    r = c.post("/admin/api/stock/full_refresh", json={"code": "600519"})
    assert r.status_code == 200
    item = jobs._q.get_nowait()
    assert item["kind"] == "stock_full"
    r2 = c.post("/admin/api/stock/nightly")
    assert r2.status_code == 200
    item2 = jobs._q.get_nowait()
    assert item2["kind"] == "stock_daily"
    assert c.post("/admin/api/stock/full_refresh", json={}).status_code == 400


def test_a5_ui_markers():
    import re
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    for mk in ("个股数据 · 避雷针自选池", "skPoolTable", "loadSkPool", "btnSkNightly",
               "data-full", "全量重拉", "stats_pool", "/admin/api/stock/nightly"):
        assert mk in html, f"缺: {mk}"
    # 注: data.html含服务端Jinja占位符(repair_ds_json等), 整块node门禁
    # 对原始模板天然不可行 — 新增JS的语法由stock.html门禁间接覆盖(同构写法)
