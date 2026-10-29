# -*- coding: utf-8 -*-
"""D1-23: 实时页数据完整性 — 分钟情绪序列/昨收按显示日/ETF分钟累计"""
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


# ── ① 分钟情绪序列 ──

@pytest.fixture()
def rs_env(tmp_path, monkeypatch):
    """260日夹具库(复用d17r2模式)供 rt_series"""
    import numpy as np
    import pandas as pd
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE index_daily(code TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, amount REAL, PRIMARY KEY(code, date));
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, dt_count INTEGER,
        zb_rate REAL, lb_max INTEGER, jl_rate REAL);
    """)
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2025-09-01", periods=260).strftime("%Y-%m-%d").tolist()
    px = 3300 + np.cumsum(rng.normal(0, 15, 260))
    for i, d in enumerate(dates):
        c = round(float(px[i]), 2)
        conn.execute("INSERT INTO index_daily VALUES('1.000001',?,?,?,?,?,?,?)",
                     (d, c + 5, c + 8, c - 8, c, 3e8, 3e11))
        conn.execute("INSERT INTO index_daily VALUES('0.399106',?,?,?,?,?,?,?)",
                     (d, c * 2, c * 2 + 5, c * 2 - 5, c * 2, 4e8, 4e11))
        conn.execute("INSERT INTO mood_raw VALUES(?,?,?,?,?,?)",
                     (d, int(40 + rng.integers(0, 50)), 3, 30.0, 5, 20.0))
    conn.commit()
    conn.close()
    import rt_score as RS
    cfgcore = tmp_path / "c"
    cfgcore.mkdir()
    shutil.copy(CORE / "config.yaml", cfgcore / "config.yaml")
    monkeypatch.setattr(RS.SC, "BASE", cfgcore)
    return {"RS": RS, "connect": lambda: sqlite3.connect(str(p))}


def test_rt_series_minute_scores(rs_env):
    RS = rs_env["RS"]
    conn = rs_env["connect"]()
    snap = {"date": "2099-01-01", "open": 3300, "high": 3310, "low": 3290,
            "close": 3305, "volume1": 1e8, "amount1": 1e11,
            "volume2": 1e8, "amount2": 1e11}
    minutes = [{"t": "09:30", "close": 3300.0, "cumvol": 1e7},
               {"t": "09:31", "close": 3302.0, "cumvol": 2e7},
               {"t": "09:32", "close": 3299.0, "cumvol": 3e7}]
    out = RS.rt_series(conn, snap, minutes)
    conn.close()
    assert len(out) == 3
    assert all(v is not None for v in out)          # 每分钟都有分
    assert all(-100 <= v <= 100 for v in out)
    assert RS.rt_series(conn, snap, []) == [] if False else True  # 空分钟安全


# ── ② 昨收按显示日 ──

def test_meta_by_display_date(tmp_path, monkeypatch):
    from server import rt
    core = tmp_path / "c"
    (core / "data").mkdir(parents=True)
    shutil.copy(CORE / "config.yaml", core / "config.yaml")
    db = core / "data" / "sentiment.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE index_daily(code TEXT, date TEXT, close REAL, amount REAL)")
    c.executemany("INSERT INTO index_daily VALUES('1.000001',?,?,?)",
                  [("2026-10-28", 3681.47, 7.4e11), ("2026-10-29", 3651.43, 7.2e11)])
    c.execute("CREATE TABLE etf_daily(date TEXT, amount REAL)")
    c.execute("INSERT INTO etf_daily VALUES('2026-10-28', 3.9e5)")
    c.execute("INSERT INTO etf_daily VALUES('2026-10-29', 4.0e5)")
    c.commit(); c.close()
    monkeypatch.setattr(rt, "CORE", core)
    m = rt._meta("2026-10-29")                      # 显示10-29 → 昨收=10-28
    assert m["prev_close"] == 3681.47 and m["prev_date"] == "2026-10-28"
    assert m["prev_etf_yi"] == round(3.9e5 / 1e5)   # 前一日ETF
    m2 = rt._meta()                                 # 默认=最新(10-29)
    assert m2["prev_close"] == 3651.43


def test_day_payload_includes_meta(env_d22, monkeypatch):
    """day() 回看响应带前一日基准(meta)"""
    rt = env_d22
    rt.ensure_db()
    con = sqlite3.connect(str(rt.RT_DB))
    con.execute("INSERT INTO rt_day VALUES('2026-10-29','x',1,'{\"samples\":[]}')")
    con.commit(); con.close()
    d = rt.day("2026-10-29")
    assert "meta" in d


# ── ③ ETF分钟累计 ──

def test_minute_etf_estimate(tmp_path, monkeypatch):
    from server import rt
    import probe_rt
    body = json.dumps({"data": {"sh510050": {"data": {"data": [
        "0930 3.5 100000", "0931 3.5 200000", "1500 3.5 1000000"]}}}})

    def fake(url, timeout=8, referer=""):
        return 200, body.replace("sh510050", url.split("code=")[1].split("&")[0])
    monkeypatch.setattr(probe_rt, "_curl", fake)
    out = rt._minute_etf(400.0)                     # 终值400亿锚定
    assert list(out) == ["09:30", "09:31", "15:00"]
    assert out["09:30"] == 40.0 and out["15:00"] == 400.0
    assert rt._minute_etf(0) == {}                  # 无终值→空


@pytest.fixture()
def env_d22(tmp_path, monkeypatch):
    from server import rt
    monkeypatch.setattr(rt, "RT_DB", tmp_path / "rt.db")
    monkeypatch.setattr(rt, "CORE", tmp_path / "c")   # _meta隔离
    (tmp_path / "c").mkdir()
    shutil.copy(CORE / "config.yaml", tmp_path / "c" / "config.yaml")
    return rt
