# -*- coding: utf-8 -*-
"""实时行情历史管理: 东财trends2源解析/缺口映射/历史回补幂等/API/UI接线"""
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
    monkeypatch.setattr(rt, "_gap_cooldown", {"until": None})
    return rt


TRENDS_BODY = json.dumps({"data": {"trends": [
    "2026-11-05 09:30,3624.58,5974886,3619.254",
    "2026-11-05 09:31,3625.00,6100000,3630.0",
    "2026-11-06 09:30,3600.00,5000000,3000.0",
    "2026-11-06 09:31,3601.00,5200000,3050.0",
    "2026-11-06 bad,ver,y,x",                       # 脏行跳过
]}})


def _mock_source(env, monkeypatch, body=TRENDS_BODY):
    import server.rt as rt
    calls = []
    monkeypatch.setattr(rt, "_em_trends_days",
                        lambda: calls.append(1) or json.loads(
                            json.dumps(_parse_fake(body))))
    return calls

def _parse_fake(body):
    """与_em_trends_days同构解析(mock替身)"""
    trends = json.loads(body)["data"]["trends"]
    out = {}
    for x in trends:
        p = x.split(",")
        if len(p) < 4:
            continue
        dt2 = p[0].split()
        if len(dt2) < 2:
            continue
        try:
            rec = (dt2[1][:5], float(p[1]), float(p[2]), float(p[3]))
        except ValueError:
            continue
        out.setdefault(dt2[0], []).append(rec)
    return out


def test_em_trends_parse(env, monkeypatch):
    """源解析: 日期分组/脏行跳过/字段映射"""
    import server.rt as rt
    import probe_rt
    monkeypatch.setattr(probe_rt, "_curl",
                        lambda url, timeout=10, referer="": (200, TRENDS_BODY))
    d = rt._em_trends_days()
    assert set(d) == {"2026-11-05", "2026-11-06"}
    assert d["2026-11-05"][0] == ("09:30", 3624.58, 5974886.0, 3619.254)
    assert d["2026-11-06"][0][1] == 3600.00
    assert len(d["2026-11-06"]) == 2                            # 脏行被跳过


def test_hist_gap_map(env, monkeypatch):
    """缺口映射: 库有11-05 → 11-06为缺口"""
    import server.rt as rt
    rt.ensure_db()
    con = sqlite3.connect(str(rt.RT_DB))
    con.execute("INSERT INTO rt_day(date,updated_at,n,payload) VALUES('2026-11-05','t',1,'{}')")
    con.commit(); con.close()
    _mock_source(env, monkeypatch)
    g = rt.hist_gap_map()
    assert g["src_days"] == ["2026-11-05", "2026-11-06"]
    assert g["missing"] == ["2026-11-06"]


def test_backfill_hist_idempotent(env, monkeypatch):
    """一键回补: 缺口日落库(带样本) → 幂等(二跑0新增); 已有日不动"""
    import server.rt as rt
    rt.ensure_db()
    con = sqlite3.connect(str(rt.RT_DB))
    con.execute("INSERT INTO rt_day(date,updated_at,n,payload) VALUES("
                "'2026-11-05','t',1,'{\"samples\":[{\"t\":\"09:30\"}]}')")
    con.commit(); con.close()
    calls = _mock_source(env, monkeypatch)
    # 情绪框架打桩(不连sentiment.db)
    import types
    fake_rs = types.ModuleType("rt_score")
    fake_rs.rt_series = lambda conn, snap, ms: [-10.0] * len(ms)
    monkeypatch.setitem(sys.modules, "rt_score", fake_rs)
    fake_db = types.ModuleType("db")
    fake_db.connect = lambda: sqlite3.connect(":memory:")
    monkeypatch.setitem(sys.modules, "db", fake_db)
    r = rt.backfill_hist()
    assert r["filled"] == ["2026-11-06"] and r["skipped"] == 1
    con = sqlite3.connect(str(rt.RT_DB))
    n, blob = con.execute("SELECT n, payload FROM rt_day WHERE date='2026-11-06'").fetchone()
    samples = json.loads(blob)["samples"]
    con.close()
    assert n == 2 and samples[0]["score"] == -10.0      # 情绪回算入样
    # 幂等
    r2 = rt.backfill_hist()
    assert r2["filled"] == [] and r2["skipped"] == 2


def test_hist_apis(server_app, monkeypatch):
    import server.rt as rt
    from server import jobs
    c = server_app["client"]
    monkeypatch.setattr(rt, "hist_gap_map",
                        lambda: {"src_days": ["2026-11-06"], "missing": ["2026-11-06"],
                                 "have_recent": []})
    r = c.get("/admin/api/rt/hist_gap").get_json()
    assert r["missing"] == ["2026-11-06"]
    r2 = c.post("/admin/api/rt/backfill_hist").get_json()
    assert r2["ok"]
    item = jobs._q.get_nowait()
    assert item["kind"] == "rt_hist"


def test_hist_ui_markers():
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    for mk in ("btnRtHist", "rtHistDetail", "loadRtHist", "/admin/api/rt/hist_gap",
               "/admin/api/rt/backfill_hist", "回补历史缺口"):
        assert mk in html, f"缺: {mk}"
