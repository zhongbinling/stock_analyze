# -*- coding: utf-8 -*-
"""简报增强: 板块冷热5日趋势 + ETF近5日均值环比 + 事件AI概要 + 打印导出PDF"""
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


def _d(offset=0):
    return (dt.date.today() - dt.timedelta(days=offset)).isoformat()


@pytest.fixture()
def benv(tmp_path, monkeypatch):
    """6日板块史(带已知Δ5) + 10日ETF(已知环比) + 事件AI概要桩"""
    db = tmp_path / "sentiment.db"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA)
    con.execute("INSERT INTO sentiment VALUES(?,?,?)", (_d(), 5.0, "中性"))
    # 6个交易日: BK热 60→66.6(Δ+6.6), BK冷 -35→-40(Δ-5.0), BK平 恒30
    for i, off in enumerate(range(5, -1, -1)):      # 升序: 5日前→今日
        d = _d(off * 2)                              # 隔日出6个distinct日期
        hot = 66.6 - 1.32 * (5 - i)                  # i=0:60.0 → i=5:66.6
        con.execute("INSERT INTO sector_scores VALUES(?,?,?)", (d, "BK热", hot))
        con.execute("INSERT INTO sector_scores VALUES(?,?,?)", (d, "BK冷", -35.0 - i))
        con.execute("INSERT INTO sector_scores VALUES(?,?,?)", (d, "BK平", 30.0))
    con.execute("INSERT INTO sectors VALUES('BK热','半导体')")
    con.execute("INSERT INTO sectors VALUES('BK冷','白酒')")
    con.execute("INSERT INTO sectors VALUES('BK平','证券')")
    # ETF 10日: 旧5日各20, 近5日各25 → 均值20→25, 环比+25.0%
    for i in range(10):
        con.execute("INSERT INTO etf_daily VALUES(?,?)",
                    (_d((9 - i) * 2), (20.0 if i < 5 else 25.0) * 1e5))
    con.execute("INSERT INTO mkt_val_daily VALUES(?,?,?,?)", (_d(), 18.5, 1.85, 12.3))
    con.commit()
    con.close()
    monkeypatch.setattr(BR, "SENT_DB", db)
    monkeypatch.setattr("server.macro.buffett",
                        lambda: {"latest": {"ratio": 78.5, "pct_all": 85, "pct_5y": 15}})
    monkeypatch.setattr("server.macro.list_events", lambda **kw: {"events": [
        {"date": _d(), "level": 3, "title": "央行降准0.5个百分点",
         "ai_impact": "释放万亿流动性利好"},
        {"date": _d(), "level": 2, "title": "无AI分析事件"},
    ]})
    stub_v = types.ModuleType("viewer")
    stub_v.build_payload = lambda cfg: {"dates": [_d(), _d()], "mval": {}}
    stub_s = types.ModuleType("score")
    stub_s.load_cfg = lambda: {}
    monkeypatch.setitem(sys.modules, "viewer", stub_v)
    monkeypatch.setitem(sys.modules, "score", stub_s)
    return tmp_path


def _gen(benv):
    out = benv / "b.html"
    BR.generate_html(str(out))
    return out.read_text(encoding="utf-8")


def test_sector_trend_arrows(benv):
    """Δ5箭头: 升温红↑/降温蓝↓/持平灰→"""
    html = _gen(benv)
    assert "半导体" in html and "↑6.6" in html       # 60→66.6, 红
    assert "#e34448\">↑6.6" in html
    assert "白酒" in html and "↓5.0" in html         # -35→-40, 蓝
    assert "#2f5cd6\">↓5.0" in html
    assert "证券" in html and "→持平" in html        # 恒30


def test_sector_trend_short_history_no_crash(benv, tmp_path, monkeypatch):
    """不足6个交易日 → 无箭头但不炸(名字照常)"""
    db = tmp_path / "s.db"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA)
    con.execute("INSERT INTO sector_scores VALUES(?,?,?)", (_d(1), "BK1", 10.0))
    con.execute("INSERT INTO sector_scores VALUES(?,?,?)", (_d(), "BK1", 12.0))
    con.commit()
    con.close()
    monkeypatch.setattr(BR, "SENT_DB", db)
    monkeypatch.setattr("server.macro.buffett", lambda: {"latest": None})
    monkeypatch.setattr("server.macro.list_events", lambda **kw: {"events": []})
    out = tmp_path / "s.html"
    BR.generate_html(str(out))
    html = out.read_text(encoding="utf-8")
    assert "BK1" in html and "↑" not in html and "↓" not in html


def test_etf_5day_wow(benv):
    """近5日均值环比前5日: 20→25亿 → +25.0%红; 标签用<small>(简报无.hint类)"""
    html = _gen(benv)
    assert "近5日均值" in html
    assert "<small>近5日均值</small>" in html     # review: class='hint'在独立HTML无定义
    assert "<b>25</b> 亿" in html
    assert "+25.0%" in html and "#e34448" in html


def test_etf_short_history_no_wow(benv, tmp_path, monkeypatch):
    """不足10日 → 无环比行, 5日序列照常"""
    db = tmp_path / "s.db"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA)
    for i in range(5):
        con.execute("INSERT INTO etf_daily VALUES(?,?)", (_d((4 - i) * 2), 20.0e5))
    con.commit()
    con.close()
    monkeypatch.setattr(BR, "SENT_DB", db)
    monkeypatch.setattr("server.macro.buffett", lambda: {"latest": None})
    monkeypatch.setattr("server.macro.list_events", lambda **kw: {"events": []})
    out = tmp_path / "s.html"
    BR.generate_html(str(out))
    html = out.read_text(encoding="utf-8")
    assert "环比" not in html and "<b>20</b>" in html


def test_event_ai_impact_rendered(benv):
    """L2+事件附AI概要(灰字small); 无AI的事件不加"""
    html = _gen(benv)
    assert "释放万亿流动性利好" in html
    assert '<small style="color:#898781">释放万亿流动性利好</small>' in html
    assert "<small" not in html.split("无AI分析事件")[1].split("</td>")[0]


def test_event_window_queries_by_range(benv, monkeypatch):
    """review修复回归: 事件窗口按level_min=2+d_from直查(摆脱list_events
    默认最新500条截断 — 总量2526+时7天窗口丢日), 不再客户端过滤等级"""
    import datetime as dt2
    cap = {}

    def _le(**kw):
        cap.update(kw)
        return {"events": []}
    monkeypatch.setattr("server.macro.list_events", _le)
    _gen(benv)
    assert cap.get("level_min") == 2
    d_from = cap.get("d_from")
    assert d_from == (dt2.date.today() - dt2.timedelta(days=7)).isoformat()
    assert cap.get("limit", 0) >= 2000


def test_print_pdf_button(benv):
    """导出PDF/打印按钮 + @media print样式(打印时隐藏按钮/去阴影)"""
    html = _gen(benv)
    assert "导出PDF/打印" in html and "window.print()" in html
    assert "@media print" in html and ".noprint" in html
    assert 'class="noprint"' in html
