# -*- coding: utf-8 -*-
"""D1-25: 实时页五问修复 — 快照守卫/object-dtype根杀/单调累计/渲染窗/日chips"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


# ── A1/A2: object-dtype 崩溃类根杀 + 无价格快照守卫 ──

def test_rt_frame_none_row_survives(tmp_path, monkeypatch):
    """合成行含None(列转object)不再抛 NoneType-float — 10-31实测qt失败日
    tick全天崩零采样的根因"""
    import shutil
    import numpy as np
    p = tmp_path / "s.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
      CREATE TABLE index_daily(code TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, amount REAL);
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY);
    """)
    import pandas as pd
    dates = pd.bdate_range("2026-08-01", periods=60).strftime("%Y-%m-%d").tolist()
    px = np.linspace(3300, 3400, 60)
    for i, d in enumerate(dates):
        c.execute("INSERT INTO index_daily VALUES('1.000001',?,?,?,?,?,?,?)",
                  (d, px[i], px[i] + 2, px[i] - 2, px[i], 1e8, 1e11))
        c.execute("INSERT INTO index_daily VALUES('0.399106',?,?,?,?,?,?,?)",
                  (d, px[i] * 2, px[i] * 2, px[i] * 2, px[i] * 2, 1e8, 1e11))
    c.commit(); c.close()
    import rt_score as RS
    cfgcore = tmp_path / "c"
    cfgcore.mkdir()
    shutil.copy(CORE / "config.yaml", cfgcore / "config.yaml")
    monkeypatch.setattr(RS.SC, "BASE", cfgcore)
    conn = sqlite3.connect(str(p))
    # 半截快照(无open/high/low/量额None) — 修复前这里抛 TypeError
    snap = {"date": dates[-1], "close": 3400.0, "open": None, "high": None,
            "low": None, "volume1": None, "amount1": None}
    r = RS.compute_rt(conn, snap)
    assert r["score"] is not None
    conn.close()


def test_fetch_snapshot_no_price_returns_empty(monkeypatch):
    """qt失败 → {} 半截快照不再产出(date都不带, tick判stale)"""
    import rt_score as RS
    import probe_rt
    monkeypatch.setattr(probe_rt, "_curl", lambda url, timeout=8: (0, ""))
    monkeypatch.setattr(RS, "_pool_snapshot", lambda conn: {"zt_count": 10})
    assert RS.fetch_snapshot(None) == {}


def test_tick_skips_empty_snapshot(tmp_path, monkeypatch):
    """tick: 空快照→stale不采样(不再整天崩)"""
    import types
    import server.rt as rt
    monkeypatch.setattr(rt, "RT_DB", tmp_path / "rt.db")
    monkeypatch.setattr(rt, "_state", {"date": None, "samples": [], "updated_at": None})
    fake_rs = types.SimpleNamespace(fetch_snapshot=lambda conn: {},
                                    compute_rt=lambda c, s: (_ for _ in ()).throw(AssertionError("不应到达")))
    monkeypatch.setitem(sys.modules, "rt_score", fake_rs)
    monkeypatch.setitem(sys.modules, "db",
                        types.SimpleNamespace(connect=lambda: sqlite3.connect(":memory:")))
    monkeypatch.setattr(rt, "_fetch_etf_amount_yi", lambda: 1.0)
    assert rt.tick(datetime(2026, 11, 3, 10, 0)) == "stale"
    assert rt._state["samples"] == []


# ── C: 累计单调 ──

def test_minute_etf_monotonic(tmp_path, monkeypatch):
    from server import rt
    import probe_rt
    rows = ["0930 1 100", "0931 1 90", "0932 1 200", "1500 1 300"]  # 0931量回退(源毛刺)
    body = json.dumps({"data": {"sh510050": {"data": {"data": rows}}}})

    def fake(url, timeout=8, referer=""):
        return 200, body.replace("sh510050", url.split("code=")[1])
    monkeypatch.setattr(probe_rt, "_curl", fake)
    out = rt._minute_etf(300.0)
    vals = list(out.values())
    assert vals == sorted(vals), f"累计必须单调: {vals}"
    assert out["15:00"] == 300.0


# ── D/B: 页面门禁 ──

def test_page_gates():
    html = (REPO / "server" / "templates" / "admin" / "realtime.html").read_text(encoding="utf-8")
    assert "cummax" in html                        # 累计显示单调化
    assert "x.t >= '09:30:00' && x.t <= '15:00:59'" in html   # 渲染窗
    assert "data-day" in html and "rtDaysList" in html        # 日chips可点
    assert "viewDate = a.dataset.day" in html
