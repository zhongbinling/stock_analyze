# -*- coding: utf-8 -*-
"""D1-22 R6: 实时行情可见性与管理 — 目标日制补采/瘦行自愈/回退显示/stats/管理卡"""
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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import server.rt as rt
    monkeypatch.setattr(rt, "RT_DB", tmp_path / "realtime.db")
    monkeypatch.setattr(rt, "_state", {"date": None, "samples": [], "updated_at": None})
    monkeypatch.setattr(rt, "_day_cache", {})
    return {"rt": rt, "mp": monkeypatch, "db": tmp_path / "realtime.db"}


def _mock_final(mp, snap_date, score=-32.1):
    import types
    mp.setitem(sys.modules, "rt_score", types.SimpleNamespace(
        fetch_snapshot=lambda conn: {"date": snap_date, "close": 3651.4,
                                     "volume1": 4.4e8, "amount1": 7.2e11,
                                     "zt_count": 39, "jl_rate": 13.9},
        compute_rt=lambda conn, snap: {"score": score, "v1": score,
                                       "stage": "偏冷/退潮"}))
    mp.setitem(sys.modules, "db", types.SimpleNamespace(
        connect=lambda: sqlite3.connect(":memory:")))


def _mock_minute(mp, rows=("0930 3650.0 100", "0931 3651.0 200", "1500 3651.4 440000")):
    import probe_rt
    body = json.dumps({"data": {"sh000001": {"data": {"data": list(rows)}}}})

    def fake(url, timeout=10, referer=""):
        return 200, body
    mp.setattr(probe_rt, "_curl", fake)


# ── R6a 目标日制 ──

def test_backfill_preopen_rebuilds_thin_yesterday(env):
    """盘前(qt服务昨日)+昨日瘦行(1点降级) → 分钟重建自愈"""
    rt, mp = env["rt"], env["mp"]
    _mock_final(mp, "2026-10-29")
    _mock_minute(mp)
    mp.setattr(rt, "_fetch_etf_amount_yi", lambda: 403.7)
    rt.ensure_db()
    con = sqlite3.connect(str(env["db"]))
    con.execute("INSERT INTO rt_day VALUES(?,?,?,?)",
                ("2026-10-29", "x", 1, json.dumps({"samples": [{"t": "15:00:00"}]})))
    con.commit(); con.close()
    now = datetime(2026, 10, 30, 1, 8)               # 盘前
    assert rt.backfill_today(now) == "ok"
    st = rt.latest()
    assert st["date"] == "2026-10-29" and len(st["samples"]) == 3   # 回退+重建
    assert st["fallback"] is True                     # R6b 非当日回退
    assert st["samples"][-1]["score"] == -32.1        # 终值融合


def test_backfill_healthy_day_skipped(env):
    """健康行(n≥100)不再重建; 幂等缓存"""
    rt, mp = env["rt"], env["mp"]
    _mock_final(mp, "2026-10-29")
    _mock_minute(mp)
    mp.setattr(rt, "_fetch_etf_amount_yi", lambda: 1.0)
    rt.ensure_db()
    con = sqlite3.connect(str(env["db"]))
    con.execute("INSERT INTO rt_day VALUES(?,?,?,?)",
                ("2026-10-29", "x", 150, json.dumps({"samples": [{"t": f"09:{i:02d}:00"} for i in range(150)]})))
    con.commit(); con.close()
    assert rt.backfill_today(datetime(2026, 10, 29, 16, 0)) == "have"
    assert rt.backfill_today(datetime(2026, 10, 30, 1, 0)) == "have"  # 缓存


def test_backfill_intraday_today_deferred(env):
    """目标=今天且未收盘 → None(盘中由tick负责)"""
    rt, mp = env["rt"], env["mp"]
    _mock_final(mp, "2026-10-30")                    # 盘中qt=今天
    _mock_minute(mp)
    mp.setattr(rt, "_fetch_etf_amount_yi", lambda: 1.0)
    assert rt.backfill_today(datetime(2026, 10, 30, 10, 0)) is None


# ── R6c stats / API / 页面 ──

def test_stats(env):
    rt = env["rt"]
    rt.ensure_db()
    con = sqlite3.connect(str(env["db"]))
    con.execute("INSERT INTO rt_day VALUES('2026-10-29','x',242,'{}')")
    con.execute("INSERT INTO rt_day VALUES('2026-10-28','x',240,'{}')")
    con.commit(); con.close()
    st = rt.stats()
    assert st["days"] == 2 and st["latest"] == "2026-10-29" and st["latest_n"] == 242
    assert st["today_n"] == 0 and st["size_kb"] > 0


def test_rt_stats_and_backfill_api(server_app, monkeypatch, tmp_path):
    from server import rt
    monkeypatch.setattr(rt, "RT_DB", tmp_path / "rt.db")
    c = server_app["client"]
    r = c.get("/admin/api/rt/stats")
    assert r.status_code == 200 and "days" in r.get_json()
    monkeypatch.setattr(rt, "backfill_now", lambda: "ok")
    r = c.post("/admin/api/rt/backfill")
    assert r.status_code == 200 and r.get_json()["msg"] == "已补采/重建"
    monkeypatch.setattr(rt, "backfill_now", lambda: "idle")
    assert "盘中" in c.post("/admin/api/rt/backfill").get_json()["msg"]


def test_mgmt_card_and_fallback_gates():
    data_html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    for mk in ("实时行情库", "btnRtBackfill", "loadRtStat", "/admin/api/rt/stats"):
        assert mk in data_html, f"数据管理卡缺: {mk}"
    rt_html = (REPO / "server" / "templates" / "admin" / "realtime.html").read_text(encoding="utf-8")
    assert "data.fallback" in rt_html and "非采集时段" in rt_html
