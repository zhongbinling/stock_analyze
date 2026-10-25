# -*- coding: utf-8 -*-
"""D1-12: tushare dc系列板块数据 — 半截日判定/守卫写入/启用过滤/注册接线"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))


@pytest.fixture()
def dc_env(tmp_path, monkeypatch):
    """迷你库: 3个启用行业板块 + sector_daily(10-16健康/10-19半截/10-20缺) + 日历"""
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE sectors(code TEXT PRIMARY KEY, name TEXT, kind TEXT, enabled INTEGER,
        first_date TEXT, last_date TEXT, updated_at TEXT);
      CREATE TABLE sector_daily(code TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, amount REAL, up_count INTEGER, down_count INTEGER,
        PRIMARY KEY(code, date));
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER);
    """)
    for i in range(3):
        conn.execute("INSERT INTO sectors VALUES(?,?,'industry',1,NULL,NULL,NULL)",
                     (f"BK{i:04d}", f"板块{i}"))
        conn.execute("INSERT INTO sector_daily(code,date,close,up_count) "
                     "VALUES(?, '2026-10-16', 100, 10)", (f"BK{i:04d}",))
    conn.execute("INSERT INTO sector_daily(code,date,close) "
                 "VALUES('BK0000','2026-10-19',99)")        # 半截: 仅1/3板块
    for d in ("2026-10-16", "2026-10-19", "2026-10-20"):
        conn.execute("INSERT INTO calendar VALUES(?,1)", (d.replace("-", ""),))
        conn.execute("INSERT INTO index_daily VALUES('1.000001', ?, 3000)", (d,))
    conn.commit()
    conn.close()
    import fetch_dc as FD
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(p)))
    return {"path": p, "FD": FD}


def _dc_df():
    return pd.DataFrame([
        ("BK0000.DC", 1, 2, 0.5, 1.5, 100.0),
        ("BK0001.DC", 1, 2, 0.5, 1.5, 100.0),
        ("BK0002.DC", 1, 2, 0.5, 1.5, 100.0),
        ("BK9999.DC", 9, 9, 9, 9, 9.0),           # 未启用/概念 → 应过滤
    ], columns=["ts_code", "open", "high", "low", "close", "amount"])


def _ix_df():
    return pd.DataFrame([
        ("BK0000.DC", 10, 5), ("BK0001.DC", 20, 3),
        ("BK0002.DC", 30, 1), ("BK9999.DC", 99, 99),
    ], columns=["ts_code", "up_num", "down_num"])


def test_dc_run_fills_thin_and_missing(dc_env, monkeypatch):
    """只补半截(10-19)与缺失(10-20)交易日; 健康日(10-16)不动; 未启用板块过滤"""
    FD = dc_env["FD"]
    calls = []

    def fake(api, fields="", **kw):
        calls.append((api, kw.get("trade_date")))
        return _dc_df() if api == "dc_daily" else _ix_df()
    monkeypatch.setattr(FD.T, "has_token", lambda: True)
    monkeypatch.setattr(FD.T, "call", fake)
    assert FD.run("20261016") is True
    touched = sorted({d for _, d in calls})
    assert touched == ["20261019", "20261020"]      # 10-16健康日跳过
    conn = sqlite3.connect(str(dc_env["path"]))
    row = conn.execute("SELECT close, up_count FROM sector_daily "
                       "WHERE code='BK0000' AND date='2026-10-20'").fetchone()
    assert row == (1.5, 10)                          # OHLC+涨跌家数齐写
    n19 = conn.execute("SELECT COUNT(*) FROM sector_daily "
                       "WHERE date='2026-10-19'").fetchone()[0]
    assert n19 == 3                                   # 半截日补齐到3板块
    assert conn.execute("SELECT COUNT(*) FROM sector_daily "
                        "WHERE code='BK9999'").fetchone()[0] == 0
    conn.close()


def test_dc_run_guard_rejects_dup_batch(dc_env, monkeypatch):
    """tushare批次主键重复 → 守卫拒绝, 该日失败, 表零污染"""
    FD = dc_env["FD"]
    dup = pd.DataFrame([
        ("BK0000.DC", 1, 2, 0.5, 1.5, 100.0),
        ("BK0000.DC", 1, 2, 0.5, 1.6, 100.0),
    ], columns=["ts_code", "open", "high", "low", "close", "amount"])
    monkeypatch.setattr(FD.T, "has_token", lambda: True)
    monkeypatch.setattr(FD.T, "call",
                        lambda api, fields="", **kw: dup if api == "dc_daily" else _ix_df())
    assert FD.run("20261016") is False
    conn = sqlite3.connect(str(dc_env["path"]))
    assert conn.execute("SELECT COUNT(*) FROM sector_daily "
                        "WHERE date='2026-10-20'").fetchone()[0] == 0
    conn.close()


def test_dc_daily_registered_everywhere():
    """注册接线: BACKFILLS + jobs标签/默认起始 + 资产表行"""
    import fetch_daily as FD2
    assert "dc_daily" in FD2.BACKFILLS
    from server import jobs
    assert "dc_daily" in jobs.BACKFILL_DATASETS
    assert jobs.BACKFILL_DEFAULT_START["dc_daily"] == "20260901"
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "dc_daily" in html and "tushare近端" in html
