# -*- coding: utf-8 -*-
"""新闻内容富化: fetch_day(with_content)薄行回填 + sync_event_content事件内容对齐"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import fetch_news as FN  # noqa: E402
import analyze_news as AN  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "macro.db"
    monkeypatch.setattr(FN, "MACRO_DB", db)
    return db


def _df(rows, with_content):
    cols = ["title", "content", "pub_time", "src", "url"] if with_content \
        else ["title", "pub_time", "src", "url"]
    if len(cols) == len(rows[0]):
        return pd.DataFrame(rows, columns=cols)
    return pd.DataFrame([r[:1] + (None,) + r[1:] for r in rows], columns=cols)


def test_fetch_day_with_content_backfills_thin_row(env, monkeypatch):
    """with_content重拉同日: 已有薄行(content空)回填富文本, 不重复插入"""
    FN.ensure_db(env)
    con = sqlite3.connect(str(env))
    con.execute("INSERT INTO news_raw(src,pub_time,title,content) "
                "VALUES('major_news','2026-11-07 09:00:00','稀土出口管制新闻',NULL)")
    con.commit()
    con.close()
    rich = "商务部宣布对日稀土出口许可大幅收紧, 887吨降至18吨, 德国韩国不受影响, 行业供给格局重塑。"
    monkeypatch.setattr(FN.T, "call", lambda api, fields="", **kw: _df(
        [("稀土出口管制新闻", rich, "2026-11-07 09:00:00", "新华网", None)], "content" in fields))
    n = FN.fetch_day("20261107", env, with_content=True)
    con = sqlite3.connect(str(env))
    total, ct = con.execute("SELECT COUNT(*), MAX(COALESCE(content,'')) FROM news_raw").fetchone()
    con.close()
    assert total == 1 and n == 1                 # 未重复插入, 回填计1
    assert ct == rich


def test_fetch_day_with_content_no_override_rich(env, monkeypatch):
    """已有富文本(em摘要版) → 不覆盖(跨源去重语义不变)"""
    FN.ensure_db(env)
    con = sqlite3.connect(str(env))
    con.execute("INSERT INTO news_raw(src,pub_time,title,content) "
                "VALUES('em_flash','2026-11-07 09:00:00','某事件','东财摘要原文')")
    con.commit()
    con.close()
    monkeypatch.setattr(FN.T, "call", lambda api, fields="", **kw: _df(
        [("某事件", "官方全文", "2026-11-07 09:00:00", "新华网", None)], "content" in fields))
    FN.fetch_day("20261107", env, with_content=True)
    con = sqlite3.connect(str(env))
    ct, total = con.execute("SELECT COALESCE(content,''), COUNT(*) FROM news_raw").fetchone()
    con.close()
    assert ct == "东财摘要原文" and total == 1


def test_fetch_day_lightweight_unchanged(env, monkeypatch):
    """默认(深史回补)仍轻量: 不拉content字段, 薄行不回填"""
    FN.ensure_db(env)
    monkeypatch.setattr(FN.T, "call", lambda api, fields="", **kw: _df(
        [("历史新闻", "2026-11-07 09:00:00", "新华网", None)], "content" in fields))
    FN.fetch_day("20261107", env)
    con = sqlite3.connect(str(env))
    ct = con.execute("SELECT COALESCE(content,'') FROM news_raw").fetchone()[0]
    con.close()
    assert ct == ""


def test_sync_event_content(env):
    """事件内容对齐: content==title且news_raw有富文本 → 更新; 幂等"""
    FN.ensure_db(env)
    con = sqlite3.connect(str(env))
    AN.ensure_tables(con)
    con.execute("INSERT INTO news_raw(src,pub_time,title,content) VALUES(?,?,?,?)",
                ("major_news", "2026-11-07 09:00", "稀土事件标题", "稀土出口管制的详细全文内容"))
    con.execute("INSERT INTO macro_events(title,level,content,date) VALUES(?,?,?,?)",
                ("稀土事件标题", 3, "稀土事件标题", "2026-11-07"))       # 薄回退
    con.execute("INSERT INTO news_raw(src,pub_time,title,content) VALUES(?,?,?,NULL)",
                ("major_news", "2026-11-07 10:00", "无富文本事件"))
    con.execute("INSERT INTO macro_events(title,level,content,date) VALUES(?,?,?,?)",
                ("无富文本事件", 2, "无富文本事件", "2026-11-07"))       # 无源可富 → 不动
    con.commit()
    con.close()
    assert AN.sync_event_content(env) == 1
    con = sqlite3.connect(str(env))
    c1 = con.execute("SELECT content FROM macro_events WHERE title='稀土事件标题'").fetchone()[0]
    c2 = con.execute("SELECT content FROM macro_events WHERE title='无富文本事件'").fetchone()[0]
    con.close()
    assert c1 == "稀土出口管制的详细全文内容"
    assert c2 == "无富文本事件"                     # 无源保持
    assert AN.sync_event_content(env) == 0         # 幂等


def test_dedup_insert_upgrades_thin_content(env):
    """跨源内容升级: em富文本遇已有薄行(major_news先入) → 回填content(跨源回流)"""
    FN.ensure_db(env)
    con = sqlite3.connect(str(env))
    now = "2026-11-08 22:00:00"
    FN._dedup_insert(con, "major_news", "2026-11-08 09:00:00", "央行MLF操作", None, None, now)
    n = FN._dedup_insert(con, "em_flash", "2026-11-08 09:05:00", "央行MLF操作",
                         None, "央行开展3000亿元MLF操作中标利率不变维稳意图明显", now)
    ct = con.execute("SELECT content FROM news_raw").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM news_raw").fetchone()[0]
    con.commit()
    con.close()
    assert n == 0 and total == 1                     # 仍去重不重复插
    assert ct == "央行开展3000亿元MLF操作中标利率不变维稳意图明显"
    # 富文本行不被薄源降级(已有content不为空 → UPDATE不命中)
    n2 = FN._dedup_insert(con2 := sqlite3.connect(str(env)), "sina_flash",
                          "2026-11-08 10:00:00", "央行MLF操作", None, None, now)
    ct2 = con2.execute("SELECT content FROM news_raw").fetchone()[0]
    con2.close()
    assert n2 == 0 and ct2 == "央行开展3000亿元MLF操作中标利率不变维稳意图明显"


def test_auto_analyze_wires_sync(monkeypatch):
    """夜间链auto_analyze先内容对齐再分析"""
    called = []
    monkeypatch.setattr(AN, "sync_event_content", lambda *a, **kw: called.append(1))
    monkeypatch.setattr(AN, "analyze_batch", lambda *a, **kw: {})
    monkeypatch.setattr(AN, "analyze_cloud_batch", lambda *a, **kw: {})
    AN.auto_analyze()
    assert called == [1]


def test_dedup_insert_strips_html(env):
    """2026-11-10修复回归: content为完整网页HTML(含script广告)时剥离标签入库"""
    import sqlite3
    FN.ensure_db(env)
    con = sqlite3.connect(str(env))
    html = '<div class="ad"><script>(sinaads=[]).push({})</script></div>太阳能装机首超煤电。'
    FN._dedup_insert(con, "em_flash", "2026-11-07 09:00:00", "稀土出口收紧", None, html, "t")
    ct = con.execute("SELECT content FROM news_raw").fetchone()[0]
    con.commit(); con.close()
    assert "<" not in ct and "script" not in ct and "太阳能装机首超煤电" in ct
