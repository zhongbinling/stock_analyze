# -*- coding: utf-8 -*-
"""D1-23b: 同花顺式量额副图 — mkline解析/昨日曲线(官方额锚定)/半日重建/页面门禁"""
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
    monkeypatch.setattr(rt, "RT_DB", tmp_path / "rt.db")
    monkeypatch.setattr(rt, "_state", {"date": None, "samples": [], "updated_at": None})
    monkeypatch.setattr(rt, "_day_cache", {})
    monkeypatch.setattr(rt, "_yday_curve_cache", {})
    monkeypatch.setattr(rt, "_yday_anchor_cache", {})
    monkeypatch.setattr(rt, "_mkline_cache", {"day": None, "bars": None})
    return {"rt": rt, "mp": monkeypatch, "db": tmp_path / "rt.db"}


# ── mkline 解析与昨日曲线 ──

def test_yday_curve_official_anchor(env):
    """曲线形态=分钟量比, 绝对额=tushare官方额(指数点数×量非金额, 实测2.3×偏差)"""
    rt, mp = env["rt"], env["mp"]
    bars = [
        ["202610291500", "3690", "3691", "3692", "3689", "100"],
        ["202610300930", "3691", "3700", "3705", "3690", "200"],
        ["202610300931", "3700", "3710", "3712", "3699", "300"],
        ["202610301500", "3710", "3715", "3716", "3709", "500"],
    ]
    mp.setattr(rt, "_mkline", lambda n=640: bars)
    mp.setattr(rt, "_yday_amt_anchor", lambda d: 7156.0)      # 10-29官方7156亿
    r = rt._yday_curve("2026-10-30")
    assert r["date"] == "2026-10-29" and r["total_yi"] == 7156.0
    # 昨日仅1根bar → 唯一时刻=全额
    assert r["points"] == {"15:00": 7156.0}
    # 多bar形态: 两根 → 按量比累计
    bars2 = [b[:] for b in bars]
    bars2.insert(0, ["202610291459", "3690", "3691", "3692", "3689", "300"])
    mp.setattr(rt, "_mkline", lambda n=640: bars2)
    rt._yday_curve_cache.clear()
    r2 = rt._yday_curve("2026-10-30")
    assert r2["points"]["15:00"] == 7156.0 and r2["points"]["14:59"] == round(7156 * 300 / 400, 1)


def test_yday_curve_no_anchor(env):
    """官方额不可得(无token) → None(前端graceful无昨日线)"""
    rt, mp = env["rt"], env["mp"]
    mp.setattr(rt, "_mkline", lambda n=640: [["202610291500", "1", "1", "1", "1", "1"]])
    mp.setattr(rt, "_yday_amt_anchor", lambda d: None)
    assert rt._yday_curve("2026-10-30") is None


# ── 半日样本盘后重建 ──

def test_partial_day_rebuilt_after_close(env):
    """盘中重启丢上午(首样本12:55) → 盘后即使n≥100也重建全日"""
    import types
    rt, mp = env["rt"], env["mp"]
    mp.setitem(sys.modules, "rt_score", types.SimpleNamespace(
        fetch_snapshot=lambda conn: {"date": "2026-10-30", "close": 3596.6,
                                     "volume1": 4.9e8, "amount1": 7.5e11,
                                     "zt_count": 47, "jl_rate": 23.1},
        compute_rt=lambda conn, snap: {"score": -40.2, "v1": -38.0, "stage": "偏冷/退潮"},
        rt_series=lambda conn, snap, mins: [None] * len(mins)))
    mp.setitem(sys.modules, "db", types.SimpleNamespace(
        connect=lambda: sqlite3.connect(":memory:")))
    mp.setattr(rt, "_fetch_etf_amount_yi", lambda: 384.6)
    mp.setattr(rt, "_minute_samples", lambda a: [
        {"t": "09:30", "close": 3600, "cumvol": 1e6, "cumamt": 15.0, "score": None,
         "v1": None, "stage": None, "zt": None, "jl": None, "etf": None},
        {"t": "15:00", "close": 3596.6, "cumvol": 4.9e8, "cumamt": 7534.3, "score": None,
         "v1": None, "stage": None, "zt": None, "jl": None, "etf": None}])
    rt.ensure_db()
    con = sqlite3.connect(str(env["db"]))
    half = [{"t": f"12:55:{i:02d}", "close": 3600, "cumvol": 1e8, "cumamt": 3000.0,
             "score": -30, "v1": -30, "stage": "x", "zt": 40, "jl": 20, "etf": 170.0}
            for i in range(150)]                       # 150点但始于12:55
    con.execute("INSERT INTO rt_day VALUES('2026-10-30','x',150,?)",
                (json.dumps({"samples": half}),))
    con.commit(); con.close()
    assert rt.backfill_today(datetime(2026, 10, 30, 23, 0)) == "ok"   # 重建
    st = rt.latest()
    assert st["samples"][0]["t"] == "09:30"            # 全日(不再从12:55起)


# ── 页面门禁 ──

def test_page_dual_grid_gates():
    html = (REPO / "server" / "templates" / "admin" / "realtime.html").read_text(encoding="utf-8")
    for mk in ("分钟量(万手)", "今日累计额(亿)", "昨日累计额(亿)",
               "meta.yday", "gridIndex: 1", "chartMain", "520px"):
        assert mk in html, f"页面缺: {mk}"
    assert "barMaxWidth: 2" in html                    # 分钟量细柱
    assert "'#e34448' : '#1baf7a'" in html             # 红涨绿跌
