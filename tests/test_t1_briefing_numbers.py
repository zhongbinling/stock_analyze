# -*- coding: utf-8 -*-
"""测试T1: 数据简报数值正确性 — 夹具库已知值 → HTML渲染数字逐一断言

锁定D1-30r修复: viewer小数分位×100转%、_pct_bar配色阈值(≥80红/≤20蓝)、
环比/5日变化/ETF聚合的计算口径, 以及L2+事件过滤与24h/7天窗口切分。
"""
import datetime as dt
import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import briefing as BR  # noqa: E402

SCHEMA = """
CREATE TABLE sentiment(date TEXT PRIMARY KEY, score REAL, stage TEXT);
CREATE TABLE sector_scores(date TEXT, code TEXT, score REAL);
CREATE TABLE sectors(code TEXT, name TEXT);
CREATE TABLE mkt_val_daily(date TEXT, alla_pe REAL, alla_pb REAL, pe300 REAL);
CREATE TABLE margin_daily(date TEXT, rzrqye REAL);
CREATE TABLE etf_daily(date TEXT, amount REAL);
"""


def _today(offset=0):
    return (dt.date.today() - dt.timedelta(days=offset)).isoformat()


@pytest.fixture()
def benv(tmp_path, monkeypatch):
    """夹具库(已知值) + server.macro与viewer打桩 → generate_html全链路可控"""
    db = tmp_path / "sentiment.db"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA)
    # 情绪: 今+12.5偏热, 昨+8.0 → 环比+4.5
    con.execute("INSERT INTO sentiment VALUES(?,?,?)", (_today(1), 8.0, "中性"))
    con.execute("INSERT INTO sentiment VALUES(?,?,?)", (_today(), 12.5, "偏热"))
    # 板块: 半导体最热+66.6, 白酒最冷-40.0 (最新日)
    for d in (_today(1), _today()):
        con.execute("INSERT INTO sector_scores VALUES(?,?,?)", (d, "BK0001", 66.6 if d == _today() else 60.0))
        con.execute("INSERT INTO sector_scores VALUES(?,?,?)", (d, "BK0002", -40.0 if d == _today() else -35.0))
        con.execute("INSERT INTO sector_scores VALUES(?,?,?)", (d, "BK0003", 3.2))
    con.execute("INSERT INTO sectors VALUES('BK0001','半导体')")
    con.execute("INSERT INTO sectors VALUES('BK0002','白酒')")
    con.execute("INSERT INTO sectors VALUES('BK0003','证券')")
    # 估值: 全A PE 18.5 / PB 1.85, 沪深300 PE 12.3
    con.execute("INSERT INTO mkt_val_daily VALUES(?,?,?,?)",
                (_today(), 18.5, 1.85, 12.3))
    # 两融: 今2.35万亿, 5日前2.30万亿 → +2.2%
    for off, v in [(5, 2.30e12), (4, 2.31e12), (3, 2.32e12), (2, 2.33e12), (1, 2.34e12), (0, 2.35e12)]:
        con.execute("INSERT INTO margin_daily VALUES(?,?)", (_today(off), v))
    # ETF: 近5日, 每日2只合计50(口径: SUM(amount)/1e5)
    for off in range(5):
        con.execute("INSERT INTO etf_daily VALUES(?,?)", (_today(4 - off), 25.0e5))
        con.execute("INSERT INTO etf_daily VALUES(?,?)", (_today(4 - off), 25.0e5))
    con.commit()
    con.close()
    monkeypatch.setattr(BR, "SENT_DB", db)
    # 巴菲特: 78.5%, 全史分位85(红), 5年分位15(蓝)
    monkeypatch.setattr("server.macro.buffett",
                        lambda: {"latest": {"ratio": 78.5, "pct_all": 85, "pct_5y": 15}})
    # 事件: L3今天/ L2三天前 入选; L1今天、L2十天前 出局
    monkeypatch.setattr("server.macro.list_events", lambda **kw: {"events": [
        {"date": _today(), "level": 3, "title": "央行降准0.5个百分点"},
        {"date": _today(3), "level": 2, "title": "证监会座谈会"},
        {"date": _today(), "level": 1, "title": "某公司发布新品公告"},
        {"date": _today(10), "level": 2, "title": "十天前的旧事件"},
    ]})
    # viewer payload打桩: 分位小数→%(D1-30r口径); 5年条用的是alla_pe_pct5
    stub_v = types.ModuleType("viewer")
    stub_v.build_payload = lambda cfg: {
        "dates": [_today(1), _today()],
        "mval": {"alla_pe_pct": [0.5, 0.807],       # →81% 红
                 "alla_pe_pct5": [0.5, 0.19],       # →19% 蓝
                 "pe300_pct": [0.5, 0.50]}}         # →50% 灰
    stub_s = types.ModuleType("score")
    stub_s.load_cfg = lambda: {}
    monkeypatch.setitem(sys.modules, "viewer", stub_v)
    monkeypatch.setitem(sys.modules, "score", stub_s)
    return tmp_path


def _gen(benv):
    out = benv / "b.html"
    BR.generate_html(str(out))
    return out.read_text(encoding="utf-8")


def test_mood_score_and_delta(benv):
    html = _gen(benv)
    assert "+12.5" in html and "偏热" in html
    assert "环比 +4.5" in html


def test_sector_hot_cold_with_cn_names(benv):
    html = _gen(benv)
    assert "半导体" in html and "+66.6" in html      # BK代码→中文名
    assert "白酒" in html and "-40.0" in html


def test_valuation_numbers_and_pct_bars(benv):
    """原始值直显 + viewer小数分位×100转%(81%红/19%蓝/50%灰)"""
    html = _gen(benv)
    assert "18.5 / 1.85" in html and "12.3" in html
    assert "81%" in html and "#e34448" in html       # ≥80红
    assert "19%" in html and "#2f5cd6" in html       # ≤20蓝
    assert "50%" in html                             # 中间灰


def test_buffett_ratio_and_pct(benv):
    html = _gen(benv)
    assert "78.5%" in html and "85%" in html and "15%" in html


def test_margin_level_and_change(benv):
    """万亿口径(/1e12两位小数) + 5日变化百分比"""
    html = _gen(benv)
    assert "2.35" in html and "万亿" in html
    assert "+2.2%" in html


def test_etf_daily_totals(benv):
    """SUM(amount)/1e5口径: 每日2×25e5→50, 近5日全列"""
    html = _gen(benv)
    assert html.count("<b>50</b>") >= 5


def test_events_l2_filter_and_windows(benv):
    """L2+过滤(L1出局) + 24h/7天窗口切分(10天前出局)"""
    html = _gen(benv)
    assert "央行降准" in html            # 今天L3 → 近24小时
    assert "证监会座谈会" in html        # 3天前L2 → 24h~7天
    assert "新品公告" not in html        # L1出局
    assert "十天前" not in html          # 超窗口出局


def test_empty_db_renders_gracefully(tmp_path, monkeypatch):
    """空库不炸: 缺失模块显示占位(--/无)"""
    db = tmp_path / "empty.db"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA)            # 仅建表无数据
    con.close()
    monkeypatch.setattr(BR, "SENT_DB", db)
    monkeypatch.setattr("server.macro.buffett", lambda: {"latest": None})
    monkeypatch.setattr("server.macro.list_events", lambda **kw: {"events": []})
    out = tmp_path / "e.html"
    BR.generate_html(str(out))
    html = out.read_text(encoding="utf-8")
    assert "数据简报" in html and "无" in html
