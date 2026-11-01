# -*- coding: utf-8 -*-
"""D1-27: 实时页体验 — 同花顺刻度轴/状态角标/日历chips/统计增backup+recent"""
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
    import server.rt as rt
    monkeypatch.setattr(rt, "RT_DB", tmp_path / "rt.db")
    core = tmp_path / "c"
    (core / "data").mkdir(parents=True)
    import shutil
    shutil.copy(CORE / "config.yaml", core / "config.yaml")
    monkeypatch.setattr(rt, "CORE", core)
    return {"rt": rt, "core": core}


def test_recent_days_from_calendar(env):
    rt = env["rt"]
    db = env["core"] / "data" / "sentiment.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER)")
    for d, open_ in (("20261030", 1), ("20261031", 0), ("20261101", 0), ("20261029", 1)):
        c.execute("INSERT INTO calendar VALUES(?,?)", (d, open_))
    c.commit(); c.close()
    assert rt.recent_days(5) == ["2026-10-30", "2026-10-29"]   # 非交易日剔除


def test_stats_backup_and_recent(env):
    rt = env["rt"]
    db = env["core"] / "data" / "sentiment.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER)")
    c.execute("INSERT INTO calendar VALUES('20261030',1)")
    c.commit(); c.close()
    rt.ensure_db()
    con = sqlite3.connect(str(rt.RT_DB))
    con.execute("INSERT INTO rt_day VALUES('2026-10-30','x',100,'{}')")
    con.commit(); con.close()
    mf = rt.ROOT / "backups" / "manifest.json"
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(json.dumps({"entries": [
        {"asset": "app"}, {"asset": "realtime", "file": "daily_20261030_223500.zip/realtime.db"}]}),
        encoding="utf-8")
    st = rt.stats()
    assert st["backup_at"] == "daily_20261030_2235"[:16]
    assert st["recent"] == ["2026-10-30"]
    assert st["days"] == 1 and st["latest_n"] == 100


def test_days_api_recent(server_app, monkeypatch, tmp_path):
    from server import rt
    monkeypatch.setattr(rt, "RT_DB", tmp_path / "rt.db")
    monkeypatch.setattr(rt, "recent_days", lambda n=5: ["2026-10-30", "2026-10-29"])
    r = server_app["client"].get("/admin/api/rt/days")
    j = r.get_json()
    assert j["recent"] == ["2026-10-30", "2026-10-29"] and "days" in j


def test_page_gates():
    html = (REPO / "server" / "templates" / "admin" / "realtime.html").read_text(encoding="utf-8")
    for mk in ("TICKS", "thsIv", "thsX(true)", "thsX(false)",          # 同花顺刻度
               "已就绪", "回看 ${viewDate}", "● 盘中实时",              # 状态角标
               "d.recent", "opacity:.4", "该日无存档",                 # 日历chips
               "fontWeight = 'bold'"):                                 # 选中态
        assert mk in html, f"缺: {mk}"
    data_html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "最近备份" in data_html and "深史无源可补" in data_html
