# -*- coding: utf-8 -*-
"""宏观打磨: 事件编辑(AI等级修正+news_local同步) / 按等级批量清理 / CSV导出 / 筛选持久化"""
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
    con = sqlite3.connect(str(tmp_path / "macro.db"))
    con.execute("DELETE FROM macro_events")          # 清种子, 测试数据纯净
    # 哨兵行: ensure_db见表空会重灌种子(_conn每次都调), 放一条L5阻断重灌
    con.execute("INSERT INTO macro_events(title,level,date) VALUES('SENTINEL_XYZ',5,'2026-01-01')")
    con.commit()
    con.close()
    return {"MC": MC, "db": tmp_path / "macro.db"}


def _news_tables(env):
    """补news_raw/news_local(模拟AI链已分析的关联表)"""
    con = sqlite3.connect(str(env["db"]))
    con.execute("CREATE TABLE IF NOT EXISTS news_raw(id INTEGER PRIMARY KEY, src TEXT, pub_time TEXT, title TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS news_local(id INTEGER PRIMARY KEY AUTOINCREMENT,
      news_id INTEGER, status TEXT, level INTEGER, type TEXT, impact TEXT,
      direction TEXT, analysis_json TEXT, model TEXT, analyzed_at TEXT)""")
    con.commit()
    con.close()


# ── update_event 单元 ──

def test_update_event_edits_fields(env):
    MC = env["MC"]
    eid = MC.add_event({"date": "2026-11-01", "title": "央行MLF操作",
                        "level": 2, "type": "货币"})
    ok = MC.update_event(eid, {"level": 3, "type": "政策", "content": "新作MLF",
                               "source": "手工修正", "date": "2026-11-02"})
    assert ok
    r = MC.list_events(q="央行MLF操作")["events"][0]
    assert r["level"] == 3 and r["type"] == "政策"
    assert r["content"] == "新作MLF" and r["date"] == "2026-11-02"
    assert r["source"] == "手工修正"


def test_update_event_syncs_news_local(env):
    """AI等级修正 → 同步news_local(列表AI概要列与等级口径一致)"""
    _news_tables(env)
    MC = env["MC"]
    eid = MC.add_event({"date": "2026-11-01", "title": "美联储降息50bp", "level": 2})
    con = sqlite3.connect(str(env["db"]))
    con.execute("INSERT INTO news_raw(src,pub_time,title) VALUES('t','2026-11-01 09:00','美联储降息50bp')")
    nid = con.execute("SELECT id FROM news_raw WHERE title='美联储降息50bp'").fetchone()[0]
    con.execute("INSERT INTO news_local(news_id,status,level,type) VALUES(?,'done',2,'货币')", (nid,))
    con.commit()
    con.close()
    MC.update_event(eid, {"level": 3, "type": "政策"})
    con = sqlite3.connect(str(env["db"]))
    lv, tp = con.execute("SELECT level, type FROM news_local WHERE news_id=?", (nid,)).fetchone()
    con.close()
    assert lv == 3 and tp == "政策"


def test_update_event_validations(env):
    MC = env["MC"]
    eid = MC.add_event({"date": "2026-11-01", "title": "校验事件", "level": 2})
    with pytest.raises(ValueError):
        MC.update_event(eid, {"level": 9})           # 等级越界
    with pytest.raises(ValueError):
        MC.update_event(eid, {"date": "2026/11/01"})  # 日期格式
    with pytest.raises(ValueError):
        MC.update_event(eid, {"title": "改名"})        # title只读 → 无可编辑字段
    with pytest.raises(ValueError):
        MC.update_event(eid, {})                      # 空修改集
    assert MC.update_event(99999, {"level": 3}) is False   # 不存在
    r = MC.list_events(q="校验事件")["events"][0]
    assert r["level"] == 2                            # 上述失败均未改动


# ── delete_events 按等级批量清理 ──

def test_delete_events_by_level_and_range(env):
    MC = env["MC"]
    for d, lv, t in [("2026-10-01", 1, "清理L1旧"), ("2026-11-01", 1, "清理L1新"),
                     ("2026-11-02", 2, "清理L2新"), ("2026-11-03", 3, "清理L3新")]:
        MC.add_event({"date": d, "title": t, "level": lv})
    assert MC.delete_events(1) == 2                   # 全范围≤L1(哨兵L5不受影响)
    assert MC.delete_events(2, d_from="2026-11-10") == 0   # 日期外
    assert {e["level"] for e in MC.list_events(q="清理")["events"]} == {2, 3}
    with pytest.raises(ValueError):
        MC.delete_events(5)                           # L5禁批量
    with pytest.raises(ValueError):
        MC.delete_events("x")                         # 非数字


# ── API层 ──

def test_update_api(server_app, tmp_path, monkeypatch):
    import server.macro as MC
    monkeypatch.setattr(MC, "MACRO_DB", tmp_path / "macro.db")
    MC.ensure_db()
    con = sqlite3.connect(str(tmp_path / "macro.db"))
    con.execute("DELETE FROM macro_events")
    con.execute("INSERT INTO macro_events(title,level,date) VALUES('SENTINEL_XYZ',5,'2026-01-01')")
    con.commit(); con.close()
    eid = MC.add_event({"date": "2026-11-01", "title": "API编辑事件", "level": 2})
    c = server_app["client"]
    assert c.put(f"/admin/api/macro/events/{eid}",
                 json={"level": 4}).status_code == 200
    assert MC.list_events(q="API编辑事件")["events"][0]["level"] == 4
    assert c.put("/admin/api/macro/events/99999",
                 json={"level": 4}).status_code == 404
    assert c.put(f"/admin/api/macro/events/{eid}",
                 json={"level": 99}).status_code == 400
    assert c.put(f"/admin/api/macro/events/{eid}",
                 json={"title": "x"}).status_code == 400  # title只读


def test_bulk_delete_api(server_app, tmp_path, monkeypatch):
    import server.macro as MC
    monkeypatch.setattr(MC, "MACRO_DB", tmp_path / "macro.db")
    MC.ensure_db()
    con = sqlite3.connect(str(tmp_path / "macro.db"))
    con.execute("DELETE FROM macro_events")
    con.execute("INSERT INTO macro_events(title,level,date) VALUES('SENTINEL_XYZ',5,'2026-01-01')")
    con.executemany("INSERT INTO macro_events(title,level,date) VALUES(?,?,?)",
                    [("批量L1a", 1, "2026-11-01"), ("批量L1b", 1, "2026-11-02"),
                     ("批量L2", 2, "2026-11-03")])
    con.commit(); con.close()
    c = server_app["client"]
    r = c.post("/admin/api/macro/events/bulk-delete", json={"level_max": 1})
    assert r.status_code == 200 and r.get_json()["deleted"] == 2
    r2 = c.post("/admin/api/macro/events/bulk-delete", json={"level_max": 5})
    assert r2.status_code == 400                      # L5禁批量
    r3 = c.post("/admin/api/macro/events/bulk-delete",
                json={"level_max": 2, "from": "2026-12-01"})
    assert r3.status_code == 200 and r3.get_json()["deleted"] == 0  # 日期外
    assert MC.list_events(q="批量")["total"] == 1      # 仅剩L2(哨兵除外)


def test_export_csv_api(server_app, tmp_path, monkeypatch):
    """CSV导出: 同筛选条件 + BOM头 + AI列 + Content-Disposition"""
    import server.macro as MC
    monkeypatch.setattr(MC, "MACRO_DB", tmp_path / "macro.db")
    MC.ensure_db()
    con = sqlite3.connect(str(tmp_path / "macro.db"))
    con.execute("DELETE FROM macro_events")
    con.execute("INSERT INTO macro_events(title,level,date) VALUES('SENTINEL_XYZ',5,'2026-01-01')")
    con.executemany("INSERT INTO macro_events(title,level,date,type,content) VALUES(?,?,?,?,?)",
                    [("导出L2事件", 2, "2026-11-01", "货币", "降准"),
                     ("导出L1事件", 1, "2026-11-02", "产业", "x")])
    con.commit(); con.close()
    c = server_app["client"]
    r = c.get("/admin/api/macro/events/export?level=2")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    assert "attachment" in r.headers.get("Content-Disposition", "")
    data = r.get_data(as_text=True)
    assert data.startswith("﻿")                     # BOM(Excel中文)
    assert "日期" in data and "导出L2事件" in data and "降准" in data
    assert "导出L1事件" not in data                    # 筛选条件生效
    # 全量导出含L1
    data2 = c.get("/admin/api/macro/events/export").get_data(as_text=True)
    assert "导出L1事件" in data2


# ── UI接线 ──

def test_ui_markers():
    html = (REPO / "server" / "templates" / "admin" / "macro.html").read_text(encoding="utf-8")
    for mk in ("macroFilters", "saveFilters", "restoreFilters",       # 筛选持久化
               "btnEvCsv", "/admin/api/macro/events/export",          # 导出CSV
               "btnEvPurge", "purgeLv", "bulk-delete",                # 批量清理
               "btnEvSave", "edLevel", "edDate", "edContent",         # 详情编辑
               "/admin/api/macro/events/' + e.id",                    # PUT编辑
               "localStorage.removeItem(FKEY)"):                      # 重置清持久化
        assert mk in html, f"缺: {mk}"
    # 初始自动加载(原版表格停在'加载中…'直到手动检索)
    assert "\nload(1);" in html
