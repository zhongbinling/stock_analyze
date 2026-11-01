# -*- coding: utf-8 -*-
"""D1-28 N2: 本地AI分析链 — ai_client/analyze_batch/news_local+成品upsert/进度/API/配置UI"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import ai_client  # noqa: E402
import analyze_news as AN  # noqa: E402


# ── ai_client ──

def test_analyze_news_parses_json():
    """正常JSON响应解析"""
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content":
                '```json\n{"level": 3, "type": "货币", "impact": "降息利好", "direction": "利多"}\n```'}}]}
    import requests
    monkey_target = "requests.post"
    orig = requests.post
    requests.post = lambda *a, **kw: FakeResp()
    try:
        r = ai_client.analyze_news("http://x", "m", "", "央行降息", "降准0.5pct")
        assert r["level"] == 3 and r["type"] == "货币"
    finally:
        requests.post = orig


def test_analyze_news_graceful_degradation():
    """非法JSON → level=1 + _parse_error"""
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "这不是JSON"}}]}
    import requests
    orig = requests.post
    requests.post = lambda *a, **kw: FakeResp()
    try:
        r = ai_client.analyze_news("http://x", "m", "", "test", "")
        assert r["level"] == 1 and "_parse_error" in r
    finally:
        requests.post = orig


def test_analyze_news_level_clamp():
    """level越界(99) → 归1"""
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": '{"level": 99, "type": "x"}'}}]}
    import requests
    orig = requests.post
    requests.post = lambda *a, **kw: FakeResp()
    try:
        r = ai_client.analyze_news("http://x", "m", "", "t", "")
        assert r["level"] == 1
    finally:
        requests.post = orig


# ── analyze_news ──

def test_progress_empty(tmp_path, monkeypatch):
    """空库/无macro.db → 全零"""
    monkeypatch.setattr(AN, "BASE", tmp_path)
    r = AN.progress()
    assert r == {"pending": 0, "done": 0, "fail": 0, "cloud_done": 0, "total_raw": 0}


def test_progress_with_data(tmp_path, monkeypatch):
    db = tmp_path / "data" / "macro.db"
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE news_raw(id INTEGER PRIMARY KEY, title TEXT, pub_time TEXT)")
    c.execute("CREATE TABLE macro_events(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, date TEXT, level INTEGER, type TEXT, source TEXT, content TEXT)")
    for i in range(5):
        c.execute("INSERT INTO news_raw VALUES(?,?,?)", (i + 1, f"新闻{i}", f"2026-10-{i+1:02d} 10:00:00"))
    c.commit(); c.close()
    monkeypatch.setattr(AN, "BASE", tmp_path)
    r = AN.progress()
    assert r["pending"] == 5 and r["total_raw"] == 5


# ── API/页面 ──

def test_ai_config_api(server_app):
    c = server_app["client"]
    r = c.get("/admin/api/ai/config")
    assert r.status_code == 200 and "local" in r.get_json()
    r2 = c.post("/admin/api/ai/config", json={
        "local": {"enabled": True, "base_url": "http://localhost:11434/v1",
                  "model": "qwen3.8:27b", "api_key": "test123"}})
    assert r2.status_code == 200
    r3 = c.get("/admin/api/ai/config").get_json()
    assert r3["local"]["enabled"] is True
    assert r3["local"]["model"] == "qwen3.8:27b"
    # 清理
    from server import jobs
    with server_app["app"].app_context():
        jobs.set_setting("ai_local_enabled", "0")


def test_ai_analyze_api(server_app, monkeypatch, tmp_path):
    monkeypatch.setattr(AN, "BASE", tmp_path)
    c = server_app["client"]
    r = c.get("/admin/api/ai/progress")
    assert r.status_code == 200 and "pending" in r.get_json()
    from server import jobs
    r2 = c.post("/admin/api/ai/analyze", json={"batch": 5})
    assert r2.status_code == 200
    item = jobs._q.get_nowait()
    assert item["kind"] == "analyze"


def test_page_ai_gates():
    html = (REPO / "server" / "templates" / "admin" / "macro.html").read_text(encoding="utf-8")
    for mk in ("aiLocalEnabled", "aiCloudEnabled", "btnAiSave", "btnAiAnalyze",
               "loadAiConfig", "loadAiProg", "aiProg", "/admin/api/ai/config"):
        assert mk in html, f"缺: {mk}"
