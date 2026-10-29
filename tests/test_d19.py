# -*- coding: utf-8 -*-
"""D1-19 段1: 体检缺口三级分类(真缺/当日未出数/源端延迟) + 资产说明增强"""
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

DAYS = ["2026-10-26", "2026-10-27", "2026-10-28"]


@pytest.fixture()
def m19(tmp_path, monkeypatch):
    """夹具库: 3交易日(26/27/28), index 仅26有行; margin 全缺;
    sw1_map 含1条未映射; sw_components+sw_daily 供 sw3 止于说明"""
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, up_count INTEGER);
      CREATE TABLE sector_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE sector_pool_stat(date TEXT, code TEXT, zt_count INTEGER);
      CREATE TABLE sector_fundflow(code TEXT, date TEXT, main_net REAL);
      CREATE TABLE margin_daily(date TEXT PRIMARY KEY, rzrqye REAL);
      CREATE TABLE sw_daily(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_components(index_code TEXT, level TEXT);
      CREATE TABLE sw_index_comp(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_breadth(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_pool(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_fflow(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw1_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT);
      CREATE TABLE sw2_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT, sw2_name TEXT);
    """)
    for d in DAYS:
        conn.execute("INSERT INTO calendar VALUES(?,1)", (d.replace("-", ""),))
    conn.execute("INSERT INTO index_daily VALUES('1.000001','2026-10-26',3000)")
    conn.execute("INSERT INTO sw1_map VALUES('BK0001','半导体','信息技术')")
    conn.execute("INSERT INTO sw1_map VALUES('BK0002','白酒',NULL)")   # 未映射
    conn.execute("INSERT INTO sw2_map VALUES('BK0001','半导体','信息技术','半导体')")
    conn.execute("INSERT INTO sw_components VALUES('801010.SI','L3')")
    conn.execute("INSERT INTO sw_daily VALUES('801010.SI','2026-10-19',100)")
    conn.commit()
    conn.close()
    import server.market as M
    monkeypatch.setattr(M, "SENT_DB", p)
    monkeypatch.setattr(M, "_cfg_cache", None)
    cfgcore = tmp_path / "c"
    cfgcore.mkdir()
    shutil.copy(CORE / "config.yaml", cfgcore / "config.yaml")
    monkeypatch.setattr(M, "CORE", cfgcore)          # raw_daily.db 不存在→跳过raw
    return M


def test_scan_classifies_pending_delay_real(m19, monkeypatch):
    """当日=28: margin 26=真缺(lag2)/27=延迟(白名单lag1)/28=待出数;
    非 margin 无延迟豁免 — index 27 为真缺口"""
    monkeypatch.setattr(m19, "_trading_days_since", lambda w: list(DAYS))
    monkeypatch.setattr(m19, "_today", lambda: "2026-10-28")
    by = {a["code"]: a for a in m19.scan_gaps(7)["assets"]}
    m = by["margin"]
    assert m["missing"] == ["2026-10-26"]
    assert m["delay"] == ["2026-10-27"]
    assert m["pending"] == ["2026-10-28"]
    i = by["index"]
    assert i["missing"] == ["2026-10-27"] and i["delay"] == [] and i["pending"] == ["2026-10-28"]


def test_scan_delay_whitelist_only_trailing(m19, monkeypatch):
    """白名单只豁免末端lag≤1 — 更早的 margin 缺日必须归真缺口"""
    days = ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]
    monkeypatch.setattr(m19, "_trading_days_since", lambda w: list(days))
    monkeypatch.setattr(m19, "_today", lambda: "2026-10-28")
    m = next(a for a in m19.scan_gaps(30)["assets"] if a["code"] == "margin")
    assert m["missing"] == ["2026-10-22", "2026-10-23", "2026-10-26"]
    assert m["delay"] == ["2026-10-27"] and m["pending"] == ["2026-10-28"]


def test_scan_non_trading_today_no_pending(m19, monkeypatch):
    """非交易日运行(周末today=31): 末端缺日<today→非待出数;
    index 全真缺, margin 末端两日按白名单延迟"""
    monkeypatch.setattr(m19, "_trading_days_since", lambda w: list(DAYS))
    monkeypatch.setattr(m19, "_today", lambda: "2026-10-31")
    by = {a["code"]: a for a in m19.scan_gaps(7)["assets"]}
    assert by["index"]["missing"] == ["2026-10-27", "2026-10-28"]
    assert by["index"]["pending"] == []
    assert by["margin"]["delay"] == ["2026-10-27", "2026-10-28"]


def test_datasets_notes_enriched(m19):
    """P3: calendar覆盖终点/sw1未映射计数/sw3止于标注 入说明"""
    ds = {d["code"]: d for d in m19.datasets()}
    assert "覆盖至2026-10-28" in ds["calendar"]["note"]
    assert "未映射1" in ds["sw1"]["note"]             # BK0002 sw1_name=NULL
    assert "未映射" not in ds["sw2"]["note"]          # 全命中不添噪
    assert "数据至2026-10-19" in ds["sw3"]["note"]   # D1-21: 源恢复后动态口径(原"止于·官方停更")


def test_data_page_renders_new_semantics(server_app):
    """D1-19r: /admin/data 真渲染(此前仅直读文件, Jinja错误漏网);
    新语义标记随页面送达"""
    r = server_app["client"].get("/admin/data")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    for marker in ("刷新时间", "待出数", "源端延迟", "真缺口", "新增${(L.days"):
        assert marker in body, f"页面缺新语义标记: {marker}"
