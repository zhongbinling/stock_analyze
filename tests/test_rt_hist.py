# -*- coding: utf-8 -*-
"""实时行情历史管理: 双源解析(东财累计口径/新浪)/全日健康判定/缺口+半日档
回补幂等/当日守卫/残日守卫/每日自动回补/API/UI接线"""
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


# f51时间, f53收, f56每分钟量增量(手), f57每分钟额增量(元) — 与真实响应同构
TRENDS_BODY = json.dumps({"data": {"trends": [
    "2026-11-05 09:30,3624.58,1000000,4837178880",
    "2026-11-05 15:00,3625.00,3312000,7312821120",
    "2026-11-06 09:30,3600.00,900000,2000000000",
    "2026-11-06 15:00,3601.00,3412000,5313000000",
    "2026-11-06 bad,ver,y,x",                       # 脏行跳过
]}})

SINA_BODY = json.dumps([
    {"day": "2026-11-04 14:59:00", "close": "3600.0", "volume": "300000000",
     "amount": "3000000000"},
    {"day": "2026-11-04 15:00:00", "close": "3601.0", "volume": "100000000",
     "amount": "1000000000"},
    {"day": "2026-11-05 09:31:00", "close": "3610.0", "volume": "50000000",
     "amount": "500000000"},
    {"day": "bad"},                                  # 脏行跳过(缺字段)
    {"day": "2026-11-05 09:32:00", "close": "3611.0", "volume": "150000000",
     "amount": "1500000000"},
])


def _parse_trends(body):
    """与_em_trends_days同构的解析替身(增量→累计)"""
    out = {}
    for x in json.loads(body)["data"]["trends"]:
        p = x.split(",")
        if len(p) < 4:
            continue
        dt2 = p[0].split()
        if len(dt2) < 2:
            continue
        try:
            c, v, a = float(p[1]), float(p[2]), float(p[3])
        except ValueError:
            continue
        arr = out.setdefault(dt2[0], [])
        prev = arr[-1] if arr else None
        arr.append((dt2[1][:5], c, (prev[2] if prev else 0.0) + v,
                    round((prev[3] if prev else 0.0) + a / 1e8, 1)))
    return out


def _mock_source(env, monkeypatch, body=TRENDS_BODY):
    """源mock: 直接替换_hist_src_days(解析口径由test_em_trends_parse独立覆盖)"""
    import server.rt as rt
    parsed = _parse_trends(body)
    calls = []
    monkeypatch.setattr(rt, "_hist_src_days",
                        lambda: calls.append(1) or json.loads(json.dumps(parsed)))
    return calls


def _stub_framework(monkeypatch, score=-10.0):
    """情绪框架打桩(不连sentiment.db)"""
    import types
    fake_rs = types.ModuleType("rt_score")
    fake_rs.rt_series = lambda conn, snap, ms: [score] * len(ms)
    monkeypatch.setitem(sys.modules, "rt_score", fake_rs)
    fake_db = types.ModuleType("db")
    fake_db.connect = lambda: sqlite3.connect(":memory:")
    monkeypatch.setitem(sys.modules, "db", fake_db)


def _read_day(rt, d):
    con = sqlite3.connect(str(rt.RT_DB))
    n, blob = con.execute("SELECT n, payload FROM rt_day WHERE date=?", (d,)).fetchone()
    con.close()
    return n, json.loads(blob)["samples"]


# ── 东财源解析(累计口径) ──

def test_em_trends_parse(env, monkeypatch):
    """增量→累计; 日界清零; 脏行跳过(2026-11实测: f56/f57为每分钟增量)"""
    import server.rt as rt
    import probe_rt
    monkeypatch.setattr(probe_rt, "_curl",
                        lambda url, timeout=10, referer="": (200, TRENDS_BODY))
    d = rt._em_trends_days()
    assert set(d) == {"2026-11-05", "2026-11-06"}
    assert d["2026-11-05"][0] == ("09:30", 3624.58, 1000000.0, 48.4)
    assert d["2026-11-05"][1][2] == 4312000.0 and d["2026-11-05"][1][3] == 121.5
    assert len(d["2026-11-06"]) == 2                            # 脏行被跳过
    assert d["2026-11-06"][0][2] == 900000.0                    # 日界累计清零
    assert d["2026-11-06"][1][3] == 73.1


# ── 新浪备源解析 ──

def test_sina_minute_days(env, monkeypatch):
    """volume股→手(/100), amount元→亿累计; 日界清零; 脏行/null容错"""
    import server.rt as rt
    import probe_rt
    monkeypatch.setattr(probe_rt, "_curl",
                        lambda url, timeout=10, referer="": (200, SINA_BODY))
    d = rt._sina_minute_days()
    assert set(d) == {"2026-11-04", "2026-11-05"}
    assert d["2026-11-04"][0] == ("14:59", 3600.0, 3000000.0, 30.0)
    assert d["2026-11-04"][1] == ("15:00", 3601.0, 4000000.0, 40.0)
    assert d["2026-11-05"][0] == ("09:31", 3610.0, 500000.0, 5.0)   # 日界清零
    assert d["2026-11-05"][1] == ("09:32", 3611.0, 2000000.0, 20.0)
    monkeypatch.setattr(probe_rt, "_curl", lambda url, timeout=10, referer="": (200, "null"))
    assert rt._sina_minute_days() == {}
    monkeypatch.setattr(probe_rt, "_curl", lambda url, timeout=10, referer="": (500, "x"))
    assert rt._sina_minute_days() == {}


def test_hist_src_union(env, monkeypatch):
    """东财主源残缺(<5日)→新浪补缺日, 同日东财优先; 东财满5日不拉备源"""
    import server.rt as rt
    em = {"2026-11-05": [("09:30", 1.0, 10.0, 10.0)],
          "2026-11-06": [("09:30", 2.0, 20.0, 20.0)]}
    sina = {"2026-11-06": [("09:31", 9.9, 99.0, 99.0)],
            "2026-11-07": [("09:31", 3.0, 30.0, 30.0)]}
    monkeypatch.setattr(rt, "_em_trends_days", lambda: em)
    monkeypatch.setattr(rt, "_sina_minute_days", lambda: sina)
    u = rt._hist_src_days()
    assert set(u) == {"2026-11-05", "2026-11-06", "2026-11-07"}
    assert u["2026-11-06"] == em["2026-11-06"]          # 同日东财优先
    calls = []
    monkeypatch.setattr(rt, "_em_trends_days",
                        lambda: {f"2026-11-0{i}": [("09:30", 1.0, 1.0, 1.0)]
                                 for i in range(1, 6)})
    monkeypatch.setattr(rt, "_sina_minute_days", lambda: calls.append(1) or {})
    assert len(rt._hist_src_days()) == 5 and not calls  # 满窗口不拉备源


# ── 全日健康判定 ──

def test_day_complete(env):
    """首≤09:35 且 末≥14:55 — 截尾半日档(11-11止于14:14)/缺早盘都不健康"""
    import server.rt as rt
    assert rt._day_complete([]) is False
    assert rt._day_complete([{"t": "09:30"}, {"t": "15:00"}]) is True
    assert rt._day_complete([{"t": "09:15:14"}, {"t": "14:55:00"}]) is True   # 边界
    assert rt._day_complete([{"t": "09:30"}, {"t": "14:14:05"}]) is False     # 截尾
    assert rt._day_complete([{"t": "12:55:00"}, {"t": "15:05:00"}]) is False  # 缺早盘


# ── 缺口映射 ──

def test_hist_gap_map(env, monkeypatch):
    """缺口+半日档: 库有11-05半日档/11-06缺 → missing=11-06, partial=11-05"""
    import server.rt as rt
    rt.ensure_db()
    con = sqlite3.connect(str(rt.RT_DB))
    con.execute("INSERT INTO rt_day(date,updated_at,n,payload) "
                "VALUES('2026-11-05','t',1,'{\"samples\":[{\"t\":\"09:30\"}]}')")
    con.commit(); con.close()
    _mock_source(env, monkeypatch)
    g = rt.hist_gap_map()
    assert g["src_days"] == ["2026-11-05", "2026-11-06"]
    assert g["missing"] == ["2026-11-06"]
    assert g["partial"] == ["2026-11-05"]


# ── 一键回补: 缺失+半日档+幂等 ──

def test_backfill_hist_fill_repair_idempotent(env, monkeypatch):
    """缺口日落库(带情绪) + 半日档重建 → 二跑全空(幂等)"""
    import server.rt as rt
    rt.ensure_db()
    con = sqlite3.connect(str(rt.RT_DB))
    con.execute("INSERT INTO rt_day(date,updated_at,n,payload) VALUES("
                "'2026-11-05','t',1,'{\"samples\":[{\"t\":\"09:30\"}]}')")
    con.commit(); con.close()
    _mock_source(env, monkeypatch)
    _stub_framework(monkeypatch)
    r = rt.backfill_hist(now=datetime(2026, 11, 7, 16, 0))
    assert r["filled"] == ["2026-11-06"] and r["repaired"] == ["2026-11-05"]
    assert r["skipped"] == 0
    n, samples = _read_day(rt, "2026-11-06")
    assert n == 2 and samples[0]["score"] == -10.0      # 情绪回算入样
    n5, s5 = _read_day(rt, "2026-11-05")
    assert n5 == 2 and s5[-1]["t"] == "15:00"           # 半日档重建为全日
    r2 = rt.backfill_hist(now=datetime(2026, 11, 7, 16, 0))
    assert r2["filled"] == [] and r2["repaired"] == [] and r2["skipped"] == 2


def test_backfill_hist_repairs_truncated_day(env, monkeypatch):
    """11-11场景: 首样本早但tick止于14:14 → 重建全日; 真值字段(zt/jl/etf)
    按时间前值保留, 且不外推出旧档视野"""
    import server.rt as rt
    rt.ensure_db()
    old = [{"t": "09:15:14", "close": 3650, "zt": 40, "jl": 18.0, "etf": 170.0},
           {"t": "11:00:00", "close": 3660, "zt": 55, "jl": 19.0, "etf": 200.0},
           {"t": "14:14:05", "close": 3670, "zt": 60, "jl": 20.0, "etf": 250.0}]
    con = sqlite3.connect(str(rt.RT_DB))
    con.execute("INSERT INTO rt_day VALUES('2026-11-11','x',3,?)",
                (json.dumps({"samples": old}),))
    con.commit(); con.close()
    _mock_source(env, monkeypatch, body=json.dumps({"data": {"trends": [
        "2026-11-11 09:30,3655.66,3581398,4837178880",
        "2026-11-11 11:00,3660.00,100000000,5000000000",
        "2026-11-11 14:14,3668.00,200000000,1500000000",
        "2026-11-11 15:00,3680.64,300000000,980000000",
    ]}}))
    _stub_framework(monkeypatch)
    r = rt.backfill_hist(now=datetime(2026, 11, 12, 16, 0))
    assert r["repaired"] == ["2026-11-11"] and r["filled"] == []
    _, samples = _read_day(rt, "2026-11-11")
    assert samples[-1]["t"] == "15:00" and samples[-1]["cumamt"] == 123.2
    assert samples[0]["zt"] == 40                          # 09:15档前值
    assert next(s for s in samples if s["t"] == "11:00")["zt"] == 55
    mid = next(s for s in samples if s["t"] == "14:14")
    assert mid["zt"] == 60 and mid["etf"] == 250.0         # 14:14:05档前值
    assert samples[-1]["zt"] is None                       # 超旧档视野不外推


def test_backfill_hist_guards(env, monkeypatch):
    """当日盘中不动(tick负责); 源残日(头部半日)不造半日档"""
    import server.rt as rt
    rt.ensure_db()
    _mock_source(env, monkeypatch, body=json.dumps({"data": {"trends": [
        "2026-11-11 09:30,3655.66,3581398,4837178880",      # 完整日
        "2026-11-11 15:00,3680.64,300000000,980000000",
        "2026-11-12 14:00,3660.00,1000,1000",               # 残日(首14:00)
        "2026-11-12 15:00,3661.00,1000,1000",
    ]}}))
    _stub_framework(monkeypatch)
    r = rt.backfill_hist(now=datetime(2026, 11, 11, 10, 0))   # 当日盘中
    assert r["filled"] == [] and r["repaired"] == [] and r["skipped"] == 2
    r2 = rt.backfill_hist(now=datetime(2026, 11, 13, 16, 0))  # 盘后: 只补完整源日
    assert r2["filled"] == ["2026-11-11"] and "2026-11-12" not in r2["repaired"]


# ── 每日自动回补 ──

def test_hist_auto_once_daily_gate(env, monkeypatch):
    """15:35前不跑; 之后当日一次; 次日再跑"""
    import server.rt as rt
    calls = []
    monkeypatch.setattr(rt, "backfill_hist",
                        lambda now=None: calls.append(now)
                        or {"filled": [], "repaired": []})
    monkeypatch.setattr(rt, "_hist_auto", {"date": None})
    rt._hist_auto_once(datetime(2026, 11, 16, 14, 0))
    assert not calls
    rt._hist_auto_once(datetime(2026, 11, 16, 15, 36))
    assert len(calls) == 1
    rt._hist_auto_once(datetime(2026, 11, 16, 20, 0))       # 当日去重
    assert len(calls) == 1
    rt._hist_auto_once(datetime(2026, 11, 17, 15, 36))      # 次日再跑
    assert len(calls) == 2


def test_loop_wires_hist_auto():
    src = (REPO / "server" / "rt.py").read_text(encoding="utf-8")
    assert "_hist_auto_once()" in src


# ── API + UI ──

def test_hist_apis(server_app, monkeypatch):
    import server.rt as rt
    from server import jobs
    c = server_app["client"]
    monkeypatch.setattr(rt, "hist_gap_map",
                        lambda: {"src_days": ["2026-11-06"], "missing": ["2026-11-06"],
                                 "partial": ["2026-11-04"], "have_recent": []})
    r = c.get("/admin/api/rt/hist_gap").get_json()
    assert r["missing"] == ["2026-11-06"] and r["partial"] == ["2026-11-04"]
    r2 = c.post("/admin/api/rt/backfill_hist").get_json()
    assert r2["ok"]
    item = jobs._q.get_nowait()
    assert item["kind"] == "rt_hist"


def test_hist_ui_markers():
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    for mk in ("btnRtHist", "rtHistDetail", "loadRtHist", "/admin/api/rt/hist_gap",
               "/admin/api/rt/backfill_hist", "回补历史缺口", "半日档",
               "d.partial", "15:35自动回补"):
        assert mk in html, f"缺: {mk}"


def test_rt_conns_have_timeout():
    """review修复回归: rt.py全部rt_day直连带timeout=30(与tick并发让锁)"""
    import re
    src = (REPO / "server" / "rt.py").read_text(encoding="utf-8")
    bare = re.findall(r"sqlite3\.connect\(str\((?:RT_DB|path)\)\)(?!.*timeout)", src)
    assert not bare, f"仍有裸连接: {bare[:3]}"
