# -*- coding: utf-8 -*-
"""跨源去重: 同日同标题不同源不重复入库"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import fetch_news as FN  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "macro.db"
    monkeypatch.setattr(FN, "MACRO_DB", db)
    return {"db": db, "mp": monkeypatch}


def test_dedup_insert_skips_cross_source(env):
    """同日同标题已从em入库 → sina/ths跳过"""
    con = sqlite3.connect(str(env["db"]))
    FN.ensure_db(env["db"])
    c = con
    c.execute("INSERT INTO news_raw(src,pub_time,title) VALUES('em_flash','2026-11-05 10:00','测试新闻')")
    c.commit()
    now = "2026-11-05 12:00:00"
    # 同标题同日 → sina跳过
    r1 = FN._dedup_insert(c, "sina_flash", "2026-11-05 10:05", "测试新闻", None, None, now)
    # 同标题不同日 → 不跳过
    r2 = FN._dedup_insert(c, "sina_flash", "2026-11-04 10:00", "测试新闻", None, None, now)
    # 不同标题同日 → 不跳过
    r3 = FN._dedup_insert(c, "ths_flash", "2026-11-05 11:00", "另一新闻", None, None, now)
    c.commit()
    total = c.execute("SELECT COUNT(*) FROM news_raw").fetchone()[0]
    c.close()
    assert r1 == 0  # 跨源重复跳过
    assert r2 == 1 and r3 == 1
    assert total == 3  # 原始1 + 不同日1 + 不同标题1


def test_refresh_em_dedup(env, monkeypatch):
    """em快讯增量时自动去重(已有ths同标题的不重复入)"""
    import pandas as pd
    con = sqlite3.connect(str(env["db"]))
    FN.ensure_db(env["db"])
    # 预置一条ths同标题
    con.execute("INSERT INTO news_raw(src,pub_time,title) VALUES('ths_flash','2026-11-05 09:00','已有新闻')")
    con.commit(); con.close()

    class FakeAK:
        @staticmethod
        def stock_info_global_em():
            return pd.DataFrame([
                {"标题": "已有新闻", "发布时间": "2026-11-05 10:00", "链接": "", "摘要": "t"},  # 重复
                {"标题": "新新闻", "发布时间": "2026-11-05 10:01", "链接": "", "摘要": "n"},  # 新
            ])
    import types
    monkeypatch.setitem(sys.modules, "akshare", FakeAK)
    n = FN.refresh_em(env["db"])
    con = sqlite3.connect(str(env["db"]))
    total = con.execute("SELECT COUNT(*) FROM news_raw").fetchone()[0]
    con.close()
    assert n == 1  # 只入库新新闻
    assert total == 2  # 预置1 + 新1
