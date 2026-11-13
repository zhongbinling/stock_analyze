# -*- coding: utf-8 -*-
"""D1-28 N1: 宏观新闻抓取链 — 幂等upsert/分页/同步图/注册/API/管理卡"""
import json
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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "macro.db"
    monkeypatch.setattr(FN, "MACRO_DB", db)
    return {"db": db, "mp": monkeypatch}


def _mn_df(rows):
    return pd.DataFrame(rows, columns=["title", "pub_time", "src", "url"])


def test_fetch_day_paginates_and_upserts(env, monkeypatch):
    """分页拉全(800满页翻页) + (src,pub_time,title)幂等去重"""
    pages = [
        _mn_df([(f"标题{i}", "2026-10-28 10:00:00", "新华网", None) for i in range(FN.PAGE)]),
        _mn_df([("尾部", "2026-10-28 15:00:00", "央广", "http://x")]),
    ]
    calls = []
    monkeypatch.setattr("time.sleep", lambda s: None)   # 翻页限速旁路(测试提速)

    def fake(api, fields="", **kw):
        calls.append(kw)
        return pages[kw.get("offset", 0) // FN.PAGE]
    monkeypatch.setattr(FN.T, "call", fake)
    n = FN.fetch_day("20261028")
    assert n == FN.PAGE + 1
    assert len(calls) == 2 and calls[0]["limit"] == FN.PAGE
    # 幂等二拉(同数据0新)
    monkeypatch.setattr(FN.T, "call", lambda *a, **k: _mn_df([]) if False else pages[0]
                        if k.get("offset", 0) == 0 else pages[1])
    n2 = FN.fetch_day("20261028")
    con = sqlite3.connect(str(env["db"]))
    total = con.execute("SELECT COUNT(*) FROM news_raw").fetchone()[0]
    con.close()
    assert total == FN.PAGE + 1                       # 去重生效


def test_refresh_em_stores_digest(env, monkeypatch):
    class FakeAK:
        @staticmethod
        def stock_info_global_em():
            return pd.DataFrame([
                {"标题": "央行降准", "发布时间": "2026-11-01 10:00:00",
                 "链接": "http://e/1", "摘要": "降准0.5个百分点"},
                {"标题": "", "发布时间": "x", "链接": "", "摘要": ""},   # 脏行跳过
            ])
    import types
    monkeypatch.setitem(sys.modules, "akshare", FakeAK)
    n = FN.refresh_em()
    assert n == 1
    con = sqlite3.connect(str(env["db"]))
    row = con.execute("SELECT src, content FROM news_raw WHERE title='央行降准'").fetchone()
    con.close()
    assert row[0] == FN.SRC_EM and "降准0.5" in row[1]   # 摘要入库


def test_run_skips_done_days(env, monkeypatch):
    """断点续传: 已覆盖日零调用"""
    FN.fetch_day  # noqa: B018
    con = sqlite3.connect(str(env["db"]))
    FN.ensure_db(env["db"])
    con.execute("INSERT INTO news_raw(src,pub_time,title) VALUES(?, '2026-10-28 10:00:00', 'x')",
                (FN.SRC_MN,))
    con.commit(); con.close()
    calls = []
    monkeypatch.setattr(FN.T, "has_token", lambda: True)
    monkeypatch.setattr(FN, "fetch_day",
                        lambda d, path=None: calls.append(d) or 0)
    monkeypatch.setattr(FN, "refresh_em", lambda path=None: 0)
    import datetime as dt
    today = dt.date.today()
    FN.run("20261028", today.strftime("%Y%m%d"), env["db"])
    # 10-28已覆盖 → 只调今日(及之后), 10-28不在calls
    assert all(not c.endswith(("20261028",)) for c in calls)


def test_sync_map(env):
    FN.ensure_db(env["db"])
    con = sqlite3.connect(str(env["db"]))
    con.executemany("INSERT INTO news_raw(src,pub_time,title) VALUES(?,?,?)", [
        (FN.SRC_MN, "2026-10-27 09:00:00", "a"), (FN.SRC_MN, "2026-10-28 10:00:00", "b"),
        (FN.SRC_EM, "2026-11-01 10:00:00", "c")])
    con.commit(); con.close()
    m = FN.sync_map(env["db"])
    assert m[FN.SRC_MN]["days"] == 2 and m[FN.SRC_MN]["first"] == "2026-10-27"
    assert m[FN.SRC_EM]["rows"] == 1


def test_registrations():
    import fetch_daily as FD
    from server import jobs
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "news_fetch" in FD.BACKFILLS
    assert "news_raw" in str(FD.BACKFILLS["news_fetch"][1])
    assert "fetch_news.refresh(backfill=False)" in (CORE / "fetch_daily.py").read_text(encoding="utf-8")  # 修复链不回补(留给驱动)
    assert "news_fetch" in jobs.BACKFILL_DATASETS
    assert jobs.BACKFILL_DEFAULT_START["news_fetch"] == "20180601"
    for mk in ("宏观新闻抓取", "btnNewsBackfill", "btnNewsInc", "loadNewsStat"):
        assert mk in html, f"缺: {mk}"


def test_news_api(server_app, monkeypatch, tmp_path):
    import fetch_news as FN
    c = server_app["client"]
    r = c.get("/admin/api/macro/news/stats")
    assert r.status_code == 200 and "sources" in r.get_json()
    assert c.post("/admin/api/macro/news/fetch", json={"mode": "hack"}).status_code == 400
    monkeypatch.setattr(FN, "refresh", lambda path=None: True)
    r = c.post("/admin/api/macro/news/fetch", json={"mode": "incremental"})
    assert r.status_code == 200 and "增量完成" in r.get_json()["msg"]
    from server import jobs
    r = c.post("/admin/api/macro/news/fetch", json={"mode": "backfill"})
    assert r.status_code == 200
    item = jobs._q.get_nowait()
    assert item["kind"] == "backfill"
