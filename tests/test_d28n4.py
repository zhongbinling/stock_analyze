# -*- coding: utf-8 -*-
"""D1-28 N4: AI链总成 — 等级计数/自动分析/一键全量/summary API/UI门禁"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import analyze_news as AN  # noqa: E402


def test_level_counts(tmp_path, monkeypatch):
    """等级计数(AI/手工分列)"""
    monkeypatch.setattr(AN, "BASE", tmp_path)
    db = tmp_path / "data" / "macro.db"
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(str(db))
    AN.ensure_tables(c)
    c.executemany("INSERT INTO macro_events(title, level, date, source) VALUES(?,?,?,?)", [
        ("a", 3, "2026-11-01", "ai_local:m"), ("b", 3, "2026-11-01", "ai_cloud:m"),
        ("c", 5, "2026-11-01", "模拟样例"), ("d", 1, "2026-11-01", "ai_local:m")])
    c.commit(); c.close()
    r = AN.level_counts()
    assert r["ai"]["3"] == 2 and r["ai"]["1"] == 1
    assert r["manual"]["5"] == 1


def test_level_counts_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(AN, "BASE", tmp_path)
    r = AN.level_counts()
    assert r == {"ai": {}, "manual": {}}


def test_auto_analyze_calls_both(tmp_path, monkeypatch):
    """自动分析 = 先本地后云端"""
    monkeypatch.setattr(AN, "BASE", tmp_path)
    calls = []
    monkeypatch.setattr(AN, "analyze_batch", lambda n: calls.append(("local", n)) or {"analyzed": 0})
    monkeypatch.setattr(AN, "analyze_cloud_batch",
                        lambda n: calls.append(("cloud", n)) or {"analyzed": 0})
    r = AN.auto_analyze(20, 10)
    assert calls == [("local", 20), ("cloud", 10)]


def test_summary_api(server_app):
    c = server_app["client"]
    r = c.get("/admin/api/ai/summary")
    assert r.status_code == 200
    j = r.get_json()
    assert "pending" in j and "levels" in j and "total_raw" in j


def test_analyze_all_api(server_app, monkeypatch):
    from server import jobs
    c = server_app["client"]
    # 未启用 → 400
    assert c.post("/admin/api/ai/analyze-all").status_code == 400
    # 启用 → 入队
    with server_app["app"].app_context():
        jobs.set_setting("ai_local_enabled", "1")
    r = c.post("/admin/api/ai/analyze-all")
    assert r.status_code == 200 and r.get_json()["ok"]
    item = jobs._q.get_nowait()
    assert item["kind"] == "analyze_all"
    with server_app["app"].app_context():
        jobs.set_setting("ai_local_enabled", "0")


def test_nightly_chain_wires_auto_analyze():
    src = (CORE / "fetch_daily.py").read_text(encoding="utf-8")
    assert "analyze_news.auto_analyze()" in src


def test_page_gates():
    html = (REPO / "server" / "templates" / "admin" / "macro.html").read_text(encoding="utf-8")
    for mk in ("btnAiAll", "aiLevels", "/admin/api/ai/summary", "一键全量",
               "L${lv} ${ai + mn}", "startsWith('ai_')"):
        assert mk in html, f"缺: {mk}"
