# -*- coding: utf-8 -*-
"""情绪图AI标注: L2+默认过滤 + 载荷上限 + AI概要附加(标题匹配复用宏观页口径) + 前端渲染"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import viewer as VW  # noqa: E402


def _mk_db(tmp_path, events, news=None):
    """tmp macro.db: macro_events + 可选news_raw/news_local/news_cloud"""
    db = tmp_path / "data" / "macro.db"
    db.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(db))
    c.execute("""CREATE TABLE macro_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT, level INTEGER, content TEXT, source TEXT, date TEXT, type TEXT,
      created_at TEXT, updated_at TEXT)""")
    c.executemany("INSERT INTO macro_events(title,level,date,type,content,source) "
                  "VALUES(?,?,?,?,?,?)", events)
    if news:
        c.execute("CREATE TABLE news_raw(id INTEGER PRIMARY KEY, src TEXT, pub_time TEXT, title TEXT)")
        c.execute("""CREATE TABLE news_local(id INTEGER PRIMARY KEY AUTOINCREMENT,
          news_id INTEGER, status TEXT, level INTEGER, type TEXT, impact TEXT,
          direction TEXT, analysis_json TEXT, model TEXT, analyzed_at TEXT)""")
        c.execute("""CREATE TABLE news_cloud(id INTEGER PRIMARY KEY AUTOINCREMENT,
          news_id INTEGER, status TEXT, analysis_json TEXT)""")
        for i, (title, lv, impact, reasoning) in enumerate(news):
            c.execute("INSERT INTO news_raw(src,pub_time,title) VALUES('t','2026-11-01 09:00',?)",
                      (title,))
            c.execute("INSERT INTO news_local(news_id,status,level,type,impact,direction)"
                      " VALUES(?,'done',?,'货币',?,'利多')", (i + 1, lv, impact))
            if reasoning:
                c.execute("INSERT INTO news_cloud(news_id,status,analysis_json) "
                          "VALUES(?,'done',?)",
                          (i + 1, json.dumps({"reasoning": reasoning}, ensure_ascii=False)))
    c.commit()
    c.close()
    return db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(VW, "BASE", tmp_path)
    return tmp_path


def test_level_filter_default_l2(env):
    """默认L2+: L1不入载荷(回补后海量日常新闻); level_min参数可覆盖"""
    _mk_db(env, [("L1日常", 1, "2026-10-01", "其他", "c", "ai_local:m"),
                 ("L2关注", 2, "2026-10-02", "政策", "c", "ai_local:m"),
                 ("L5股灾", 5, "2026-10-03", "市场", "c", "手工")])
    evs = VW._read_events()
    assert [e["title"] for e in evs] == ["L2关注", "L5股灾"]      # L1出局, 升序
    assert VW._read_events(level_min=1)[0]["title"] == "L1日常"   # 显式放开


def test_ai_impact_attached(env):
    """AI概要附加: news_local.impact按标题匹配进事件(与宏观页同口径)"""
    _mk_db(env,
           events=[("央行降准0.5个百分点", 3, "2026-11-01", "货币", "原文摘要", "ai_local:qwen")],
           news=[("央行降准0.5个百分点", 3, "释放万亿流动性", "宽松确认")])
    evs = VW._read_events()
    assert evs[0]["ai_impact"] == "释放万亿流动性"
    assert evs[0]["ai_level"] == 3 and evs[0]["ai_reasoning"] == "宽松确认"


def test_ai_attach_missing_tables_graceful(env):
    """news表缺失 → 附加静默跳过, 事件照常返回(不拖累视图生成)"""
    _mk_db(env, events=[("无AI事件", 4, "2026-11-01", "市场", "c", "手工")])
    evs = VW._read_events()
    assert len(evs) == 1 and "ai_impact" not in evs[0]


def test_cap_keeps_newest(env):
    """载荷上限EV_CAP: 超限时保留最新N条(升序返回)"""
    import pandas as pd
    ds = pd.bdate_range("2010-01-01", periods=VW.EV_CAP + 120).strftime("%Y-%m-%d")
    rows = [(f"事件{i:05d}", 2, ds[i], "政策", None, "s")
            for i in range(VW.EV_CAP + 120)]
    _mk_db(env, rows)
    evs = VW._read_events()
    assert len(evs) == VW.EV_CAP
    assert evs[-1]["title"] == f"事件{VW.EV_CAP + 119:05d}"      # 最新在尾
    assert evs[0]["title"] == f"事件{120:05d}"                   # 最旧被截


def test_frontend_ai_rendering():
    """前端接线: 悬停AI概要行(紫色) + 时间区事件AI徽标 + 标题列左对齐 + L1勾选移除"""
    h = VW.HTML
    assert "e.ai_impact" in h and "AI ${e.ai_level" in h          # 悬停AI概要
    assert "startsWith('ai_')" in h and '<b style="color:#6250d6">AI</b> ' in h
    # AI概要优先于content摘要(else if)
    i_if, i_else = h.index("if (e.ai_impact)"), h.index("else if (e.content)")
    assert i_if < i_else
    assert 'value="1"' not in h
    # review: 事件表标题列左对齐(全局td默认right, 仅首列豁免)
    assert '<th style="text-align:left">标题</th>' in h
    assert '<td style="text-align:left">${(o.e.source' in h
