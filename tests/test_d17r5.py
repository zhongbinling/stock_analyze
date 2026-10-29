# -*- coding: utf-8 -*-
"""D1-17 R5: 盘后可见性 — 重启水合/盘后补采/分钟重建/守卫链/页面适配"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
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


def _mock_final(mp, snap_date, score=-30.3):
    """终值链mock: rt_score快照/评分 + db连接(注入sys.modules)"""
    import types
    mp.setitem(sys.modules, "rt_score", types.SimpleNamespace(
        fetch_snapshot=lambda conn: {"date": snap_date, "close": 3681.5,
                                     "volume1": 4.3e8, "amount1": 7.5e11,
                                     "amount2": 4.2e11,
                                     "zt_count": 72, "jl_rate": 19.5},
        compute_rt=lambda conn, snap: {"score": score, "v1": score - 5,
                                       "stage": "偏冷/退潮"}))
    mp.setitem(sys.modules, "db", types.SimpleNamespace(
        connect=lambda: sqlite3.connect(":memory:")))


def _mock_minute(mp, rows1, rows2):
    """ifzq 双指数分钟线罐头(调用顺序 sh000001 → sz399106)"""
    body = [json.dumps({"data": {"sh000001": {"data": {"data": rows1}}}}),
            json.dumps({"data": {"sz399106": {"data": {"data": rows2}}}})]
    state = {"n": 0}

    def fake_curl(url, timeout=10, referer=""):
        b = body[state["n"]] if state["n"] < len(body) else "{}"
        state["n"] += 1
        return 200, b
    import probe_rt
    mp.setattr(probe_rt, "_curl", fake_curl)


def _after_close_today(hour=15, minute=30):
    """今天的收盘后时刻; 若撞周末则顺延到下周一(守卫测试需交易日)"""
    d = datetime.now().replace(hour=hour, minute=minute, second=0)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


# ── R5a 重启水合 ──

def test_hydrate_restores_today_from_db(env):
    rt = env["rt"]
    rt.ensure_db()
    samples = [{"t": "10:00:00", "close": 3680, "cumamt": 4000, "score": -20,
                "v1": -25, "stage": "偏冷/退潮", "zt": 60, "jl": 18, "etf": 300,
                "cumvol": 1e8}]
    con = sqlite3.connect(str(env["db"]))
    con.execute("INSERT INTO rt_day VALUES(?,?,?,?)",
                (datetime.now().strftime("%Y-%m-%d"), "x", 1,
                 json.dumps({"samples": samples})))
    con.commit()
    con.close()
    st = rt.latest()                          # 空态 → 水合
    assert st["samples"] and st["samples"][0]["score"] == -20


# ── R5b 盘后补采 ──

def test_backfill_builds_and_fuses(env):
    rt, mp = env["rt"], env["mp"]
    _mock_final(mp, datetime.now().strftime("%Y-%m-%d"))
    _mock_minute(mp, ["0930 3676.92 100000", "0931 3678.0 200000"],
                 ["0930 7700.0 120000", "0931 7710.0 240000"])
    mp.setattr(rt, "_fetch_etf_amount_yi", lambda: 384.0)
    when = _after_close_today()
    assert rt.backfill_today(when) == "ok"
    st = rt.latest()
    assert len(st["samples"]) == 2
    assert st["samples"][0]["t"] == "09:30" and st["samples"][0]["score"] is None
    last = st["samples"][-1]
    assert last["score"] == -30.3 and last["etf"] == 384.0    # 终值融合末点
    assert last["cumamt"] > st["samples"][0]["cumamt"]   # 额度单调
    assert rt.backfill_today(when) == "have"                  # 幂等不重复


def test_backfill_target_semantics(env):
    """D1-22目标制: 盘中当日= None; 周末服务周五 → 重建周五(瘦行自愈窗口)"""
    rt, mp = env["rt"], env["mp"]
    _mock_final(mp, "2026-10-30")            # 周六qt仍服务周五
    _mock_minute(mp, ["0930 3650.0 100", "1500 3651.0 440000"], [])
    mp.setattr(rt, "_fetch_etf_amount_yi", lambda: 1.0)
    # 盘中当日(qt=今天): None
    assert rt.backfill_today(datetime(2026, 10, 30, 10, 0)) is None
    # 周六16:00(qt服务周五10-30): 目标=周五 → 重建
    r = rt.backfill_today(datetime(2026, 10, 31, 16, 0))
    assert r in ("ok", "have")
    if r == "ok":
        st = rt.latest()
        assert st["date"] == "2026-10-30"


def test_minute_samples_estimate(env):
    """R5r: 额度=上证口径量比线性估算(与实时tick的cumamt一致), 单调到终值真值"""
    rt, mp = env["rt"], env["mp"]
    _mock_minute(mp, ["0930 10.0 100", "0931 10.1 200", "0932 10.2 300"], [])
    out = rt._minute_samples(7.5e11)          # 上证终值7500亿
    assert [o["t"] for o in out] == ["09:30", "09:31", "09:32"]
    amts = [o["cumamt"] for o in out]
    assert amts[0] == 2500.0 and amts[1] == 5000.0 and amts[-1] == 7500.0
    assert out[-1]["close"] == 10.2 and out[0]["score"] is None
    # 残行(字段不足)过滤 + 无效JSON → 空
    _mock_minute(mp, ["0930 10.0 100", "bad-row"], [])
    assert len(rt._minute_samples(1e11)) == 1
    _mock_minute(mp, ["not json"], [])
    assert rt._minute_samples(1e11) == []


# ── 页面与接线门禁 ──

def test_template_symbol_for_backfill():
    html = (REPO / "server" / "templates" / "admin" / "realtime.html").read_text(encoding="utf-8")
    assert "showSymbol: true, symbolSize: 3" in html
    assert "盘后补采仅收盘点" in html


def test_loop_wires_backfill():
    src = (REPO / "server" / "rt.py").read_text(encoding="utf-8")
    assert 'if tick() == "idle":' in src and "backfill_today()" in src
    assert "_hydrate_locked()" in src
