# -*- coding: utf-8 -*-
"""D1-30: 宏观策略优化 — ≥N搜索/分页/详情弹窗/L2+AI分析/抓取态/暂停/简报修正"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import server.macro as MC
    monkeypatch.setattr(MC, "MACRO_DB", tmp_path / "macro.db")
    MC.ensure_db()
    return {"MC": MC, "db": tmp_path / "macro.db"}


def _clear(env):
    con = sqlite3.connect(str(env["db"]))
    con.execute("DELETE FROM macro_events")
    con.commit(); con.close()

def _ensure_news_tables(env):
    con = sqlite3.connect(str(env["db"]))
    con.execute("CREATE TABLE IF NOT EXISTS news_raw(id INTEGER PRIMARY KEY, src TEXT, pub_time TEXT, title TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS news_local(id INTEGER PRIMARY KEY AUTOINCREMENT,
      news_id INTEGER, status TEXT, level INTEGER, type TEXT, impact TEXT,
      direction TEXT, analysis_json TEXT, model TEXT, analyzed_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS news_cloud(id INTEGER PRIMARY KEY AUTOINCREMENT,
      news_id INTEGER, status TEXT, analysis_json TEXT)""")
    con.commit(); con.close()


def test_level_min_search(env):
    """≥N等级: 选L3命中3/4/5, 不命中L1/L2 — 用特定标题断言(种子也命中≥3)"""
    MC = env["MC"]
    MC.add_event({"date": "2026-11-01", "title": "测试L1事件XYZ", "level": 1})
    MC.add_event({"date": "2026-11-01", "title": "测试L3事件XYZ", "level": 3})
    MC.add_event({"date": "2026-11-01", "title": "测试L5事件XYZ", "level": 5})
    r = MC.list_events(level_min=3, q="XYZ")
    assert r["total"] == 2
    levels = {e["level"] for e in r["events"]}
    assert levels == {3, 5}


def test_pagination(env):
    """分页: 35条→2页(30+5) — q过滤种子"""
    MC = env["MC"]
    for i in range(35):
        MC.add_event({"date": f"2026-10-{i+1:02d}", "title": f"分页事件P{i}", "level": 1})
    r1 = MC.list_events(limit=30, page=1, q="分页事件P")
    assert r1["total"] == 35 and r1["pages"] == 2 and len(r1["events"]) == 30
    r2 = MC.list_events(limit=30, page=2, q="分页事件P")
    assert len(r2["events"]) == 5


def test_l2plus_ai_analysis_attached(env):
    """L2+事件附加AI分析(本地impact/云端reasoning)"""
    MC = env["MC"]; _clear(env); _ensure_news_tables(env)
    MC.add_event({"date": "2026-11-01", "title": "降准0.5pct", "level": 3, "type": "货币"})
    # 模拟news_raw + news_local(有AI分析)
    con = sqlite3.connect(str(env["db"]))
    con.execute("INSERT INTO news_raw(src,pub_time,title) VALUES('t','2026-11-01 10:00','降准0.5pct')")
    nid = con.execute("SELECT id FROM news_raw WHERE title='降准0.5pct'").fetchone()[0]
    con.execute("INSERT INTO news_local(news_id,status,level,type,impact,direction) VALUES(?,'done',3,'货币','释放万亿流动性','利多')", (nid,))
    con.commit(); con.close()
    r = MC.list_events(level_min=2)
    ev = [e for e in r["events"] if e["title"] == "降准0.5pct"][0]
    assert ev.get("ai_impact") == "释放万亿流动性"
    assert ev.get("ai_level") == 3


def test_briefing_pct_fixed():
    """D1-29r: _fmt_pct乘100(0.493→49%, 修0%假值)"""
    src = (CORE / "briefing.py").read_text(encoding="utf-8")
    assert "v*100:.0f" in src
    assert "_pct_bar" in src                       # 分位进度条


def test_pause_api(server_app):
    from server import jobs
    c = server_app["client"]
    r = c.post("/admin/api/ai/pause", json={"pause": True})
    assert r.status_code == 200 and "暂停" in r.get_json()["msg"]
    with server_app["app"].app_context():
        assert jobs.get_setting("ai_pause") == "1"
    c.post("/admin/api/ai/pause", json={"pause": False})
    with server_app["app"].app_context():
        assert jobs.get_setting("ai_pause") == "0"


def test_running_api(server_app):
    r = server_app["client"].get("/admin/api/macro/news/running")
    assert r.status_code == 200 and "running" in r.get_json()


def test_page_gates():
    html = (REPO / "server" / "templates" / "admin" / "macro.html").read_text(encoding="utf-8")
    for mk in ("fLevelMin", "pageNav", "showDetail", "evDetail", "btnAiPause",
               "level_min", "_defaultDates", "上一页", "下一页",
               "loadNewsRun", "ai_reasoning", "fType"):
        assert mk in html, f"缺: {mk}"
