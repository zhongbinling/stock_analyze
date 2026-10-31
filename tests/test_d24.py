# -*- coding: utf-8 -*-
"""D1-24: 数据链治理 — sw_bars增量/链挂载/BF双段/源滞后配置/落后提示/修复诊断"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import sw_bars as SB  # noqa: E402


# ── 24a: sw_bars.refresh ──

def test_sw_bars_refresh_upserts(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""CREATE TABLE sw_daily(index_code TEXT, date TEXT, open REAL,
      high REAL, low REAL, close REAL, volume REAL, amount REAL, pct_chg REAL,
      PRIMARY KEY(index_code, date))""")
    conn.commit(); conn.close()
    monkeypatch.setattr(SB.T, "has_token", lambda: True)
    monkeypatch.setattr(SB, "connect", lambda: sqlite3.connect(str(p)))
    df = pd.DataFrame([
        {"ts_code": "801010.SI", "trade_date": "20261030", "open": 3000,
         "high": 3010, "low": 2990, "close": 3005, "vol": 1e6, "amount": 2e9,
         "pct_change": 0.5},
        {"ts_code": "801010.SI", "trade_date": "20261029", "open": 2990,
         "high": 3000, "low": 2985, "close": 2995, "vol": 9e5, "amount": 1.8e9,
         "pct_change": -0.2},
    ])
    monkeypatch.setattr(SB.T, "call", lambda *a, **k: df)
    assert SB.refresh() is True
    c = sqlite3.connect(str(p))
    rows = c.execute("SELECT index_code, date, close FROM sw_daily ORDER BY date").fetchall()
    c.close()
    assert rows == [("801010.SI", "2026-10-29", 2995), ("801010.SI", "2026-10-30", 3005)]


def test_backfill_sw_daily_dual_pass(monkeypatch):
    """BF入口: 先refresh(近端) 再backfill(深缺口) — 近端缺口不被7日豁免跳过"""
    import fetch_daily as FD
    calls = []
    monkeypatch.setattr("sw_bars.refresh", lambda days=12: calls.append("refresh"))
    monkeypatch.setattr("sw_bars.backfill", lambda start: calls.append(("backfill", start)) or True)
    assert FD.backfill_sw_daily("20200101") is True
    assert calls[0] == "refresh" and calls[1] == ("backfill", "20200101")


def test_chain_wires_bars():
    src = (CORE / "fetch_daily.py").read_text(encoding="utf-8")
    assert '("bars", lambda: __import__("sw_bars").refresh())' in src


# ── 24c: 源滞后配置化 ──

def test_source_lag_config():
    from server import market
    assert market._SOURCE_LAG == {"margin": 1}
    assert market._DELAY_LAG is market._SOURCE_LAG        # 兼容别名同源


# ── 24d: 资产表落后提示 ──

def test_datasets_trailing_note(tmp_path, monkeypatch):
    import shutil
    p = tmp_path / "s.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY);
      CREATE TABLE sector_daily(code TEXT, date TEXT);
      CREATE TABLE sector_pool_stat(date TEXT, code TEXT);
      CREATE TABLE sector_fundflow(code TEXT, date TEXT);
      CREATE TABLE margin_daily(date TEXT PRIMARY KEY);
      CREATE TABLE sw_daily(index_code TEXT, date TEXT);
      CREATE TABLE sw_index_comp(index_code TEXT, date TEXT);
      CREATE TABLE sw_breadth(index_code TEXT, date TEXT);
      CREATE TABLE sw_pool(index_code TEXT, date TEXT);
      CREATE TABLE sw_fflow(index_code TEXT, date TEXT);
    """)
    for d in ("2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"):
        c.execute("INSERT INTO calendar VALUES(?,1)", (d.replace("-", ""),))
        c.execute("INSERT INTO index_daily VALUES('1.000001',?,3000)", (d,))
    c.execute("INSERT INTO sw_daily VALUES('801010.SI','2026-10-27')")  # 落后3日
    c.commit(); c.close()
    import server.market as M
    monkeypatch.setattr(M, "SENT_DB", p)
    monkeypatch.setattr(M, "_cfg_cache", None)
    cfgcore = tmp_path / "c"
    cfgcore.mkdir()
    shutil.copy(CORE / "config.yaml", cfgcore / "config.yaml")
    monkeypatch.setattr(M, "CORE", cfgcore)
    ds = {d["code"]: d for d in M.datasets()}
    assert "落后3个交易日" in ds["sw_daily"]["note"]


# ── 24b: 修复闭环诊断 ──

def test_repair_diagnoses_remaining(server_app, monkeypatch):
    from server import jobs, market

    def fake_scan(win):
        return {"window_days": win, "trading_days": 5, "latest": "2026-10-30",
                "assets": [
                    {"code": "sw_daily", "name": "SW官方", "db": "s.db",
                     "missing": ["2026-10-29", "2026-10-30"], "pending": [],
                     "delay": [], "has_path": True},
                    {"code": "margin", "name": "两融", "db": "s.db",
                     "missing": [], "pending": [], "delay": ["2026-10-30"],
                     "has_path": True},
                    {"code": "无通道集", "name": "X", "db": "s.db",
                     "missing": ["2026-10-30"], "pending": [], "delay": [],
                     "has_path": False}]}
    monkeypatch.setattr(market, "scan_gaps", fake_scan)

    def fake_run(cmd, append, cwd=None, env_extra=None):
        append("$ run")
        return 0
    monkeypatch.setattr(jobs, "_run", fake_run)
    lines = []
    with server_app["app"].app_context():
        assert jobs.task_repair(7)(lines.append) == 0
    tail = "\n".join(lines)
    assert "定向回补零写入" in tail and "SW官方" in tail
    assert "无定向通道" in tail
