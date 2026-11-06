# -*- coding: utf-8 -*-
"""AI分析批量控制: 一键limit参数(批次精确到余量/上限封顶) + 夜间增量100 + 最新优先回归"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import analyze_news as AN  # noqa: E402


# ── 最新优先回归(核心语义: 历史积压不饿死新新闻) ──

def test_analyze_batch_newest_first(tmp_path, monkeypatch):
    """pending中2018历史与2026新新闻混杂 → pub_time DESC, 新的先分析"""
    import ai_client
    db = tmp_path / "macro.db"
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE news_raw(
      id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT, pub_time TEXT,
      title TEXT, url TEXT, content TEXT, fetched_at TEXT)""")
    AN.ensure_tables(con)
    con.executemany("INSERT INTO news_raw(src,pub_time,title) VALUES(?,?,?)", [
        ("major_news", "2018-06-01 09:00:00", "2018历史新闻甲"),
        ("major_news", "2018-06-02 09:00:00", "2018历史新闻乙"),
        ("em_flash", "2026-11-06 10:00:00", "今日新新闻A"),
        ("em_flash", "2026-11-06 11:00:00", "今日新新闻B"),
    ])
    con.commit()
    con.close()
    done = []
    monkeypatch.setattr(ai_client, "analyze_news",
                        lambda *a, **kw: done.append(a[3]) or
                        {"level": 2, "type": "政策", "impact": "t", "direction": "中性"})
    cfg = {"base_url": "http://x", "model": "m", "api_key": "", "enabled": True}
    monkeypatch.setattr(AN, "_is_paused", lambda: False)
    r = AN.analyze_batch(2, db_path=db, cfg=cfg)
    assert r["analyzed"] == 2 and set(done) == {"今日新新闻A", "今日新新闻B"}
    con = sqlite3.connect(str(db))
    left = {t for (t,) in con.execute("SELECT title FROM news_raw "
                                      "WHERE id NOT IN (SELECT news_id FROM news_local)")}
    con.close()
    assert left == {"2018历史新闻甲", "2018历史新闻乙"}   # 历史留待后续批次


# ── API: limit参数 ──

def _post_and_pull(server_app, body):
    from server import jobs
    c = server_app["client"]
    with server_app["app"].app_context():
        jobs.set_setting("ai_local_enabled", "1")
    assert c.post("/admin/api/ai/analyze-all", json=body).status_code == 200
    item = jobs._q.get_nowait()
    with server_app["app"].app_context():
        jobs.set_setting("ai_local_enabled", "0")
    return item


def test_api_limit_exact_batches(server_app, monkeypatch):
    """limit=50 → 批次[20,20,10]精确到余量; 结束提示达上限"""
    item = _post_and_pull(server_app, {"limit": 50})
    calls, logs = [], []

    def fake_batch(n):
        calls.append(n)
        return {"analyzed": 20, "failed": 0, "pending": 999}
    monkeypatch.setattr(AN, "analyze_batch", fake_batch)
    monkeypatch.setattr(AN, "analyze_cloud_batch",
                        lambda *a, **kw: {"analyzed": 0, "pending": 0})
    rc = item["fn"](logs.append)
    assert rc == 0
    assert calls == [20, 20, 10]
    assert any("达本次上限50条" in l for l in logs)


def test_api_limit_capped_and_break(server_app, monkeypatch):
    """limit>1000封顶1000; analyzed=0(无待/暂停)提前止"""
    item = _post_and_pull(server_app, {"limit": 99999})
    calls, logs = [], []
    state = {"n": 0}

    def fake_batch(n):
        calls.append(n)
        state["n"] += 1
        return {"analyzed": 20, "pending": 5} if state["n"] <= 1 else {"analyzed": 0, "pending": 0}
    monkeypatch.setattr(AN, "analyze_batch", fake_batch)
    monkeypatch.setattr(AN, "analyze_cloud_batch",
                        lambda *a, **kw: {"analyzed": 0, "pending": 0})
    item["fn"](logs.append)
    assert len(calls) == 2                       # 第2批analyzed=0即止
    assert not any("达本次上限" in l for l in logs)   # 未达上限不误报
    # 封顶: default路径(无body) → 入队消息含1000
    item2 = _post_and_pull(server_app, None)
    assert "1000" in item2["target"]


def test_api_invalid_limit_defaults(server_app):
    """limit非法值 → 默认1000不炸"""
    item = _post_and_pull(server_app, {"limit": "abc"})
    assert "1000" in item["target"]


# ── 夜间增量 + UI ──

def test_auto_analyze_nightly_100():
    """夜间链默认100条/晚(em日增~200追平口径)"""
    import inspect
    sig = inspect.signature(AN.auto_analyze)
    assert sig.parameters["local_batch"].default == 100
    src = (CORE / "fetch_daily.py").read_text(encoding="utf-8")
    assert "analyze_news.auto_analyze()" in src            # 夜间链用默认


def test_ui_limit_select():
    html = (REPO / "server" / "templates" / "admin" / "macro.html").read_text(encoding="utf-8")
    assert 'id="aiLimit"' in html
    for v in ('value="200"', 'value="500"', 'value="1000" selected'):
        assert v in html
    assert "JSON.stringify({limit: +$('aiLimit').value})" in html
    assert "一键分析" in html
