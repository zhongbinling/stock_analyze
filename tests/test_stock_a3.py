# -*- coding: utf-8 -*-
"""避雷针A3: 个股解禁/大事件归一/风险分级/顶部风险提示/API/UI接线"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import fetch_stock as FS  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "stock.db"
    monkeypatch.setattr(FS, "STOCK_DB", db)
    return db


# ── 风险分级 ──

def test_float_risk_tiers():
    assert FS._float_risk(6.0) == "high"      # ≥5%大额
    assert FS._float_risk(5) == "high"
    assert FS._float_risk(2.0) == "mid"       # ≥1%
    assert FS._float_risk(0.3) == "low"
    assert FS._float_risk(None) == "low"


def test_forecast_risk_map():
    assert FS.FORECAST_RISK["首亏"] == "high"
    assert FS.FORECAST_RISK["预增"] == "low"
    assert FS.FORECAST_RISK["略减"] == "mid"


# ── 解禁: 入库+事件生成+载荷分段 ──

def test_refresh_float_and_payload(env, monkeypatch):
    import datetime as dt
    today = dt.date.today()
    df = pd.DataFrame([
        {"ts_code": "600519.SH", "ann_date": "20261001", "float_date": "20250601",
         "float_share": 1e8, "float_ratio": 8.0, "holder_name": "首发股东甲"},
        {"ts_code": "600519.SH", "ann_date": "20261101",
         "float_date": (today + dt.timedelta(days=30)).strftime("%Y%m%d"),
         "float_share": 5e7, "float_ratio": 6.0, "holder_name": "定增股东乙"},
        {"ts_code": "600519.SH", "ann_date": "20261101",
         "float_date": (today + dt.timedelta(days=200)).strftime("%Y%m%d"),
         "float_share": 2e7, "float_ratio": 0.5, "holder_name": "股权激励丙"},
    ])
    monkeypatch.setattr(FS.T, "call", lambda *a, **kw: df)
    n = FS.refresh_float("600519", env)
    assert n == 3
    p = FS.float_payload("600519", env)
    # 分段: 1已解禁(蓝), 2未来90天(大额红), 3未来200天(不在soon)
    assert p["n"] == 3
    assert [x["future"] for x in p["data"]] == [False, True, True]
    assert [x["big"] for x in p["data"]] == [True, True, False]
    assert p["soon"]["n"] == 1 and p["soon"]["max_ratio"] == 6.0
    # 事件同步生成(高风险2条: 8%与6%)
    ev = FS.events_payload("600519", env)
    high = [e for e in ev["events"] if e["risk"] == "high"]
    assert len(high) == 2 and all("解禁" in e["title"] for e in high)


# ── 事件归一: 预告/分红/增减持/停牌 ──

def test_refresh_events_risk_grading(env, monkeypatch):
    def fake(api, fields="", **kw):
        if api == "forecast":
            return pd.DataFrame([
                {"ts_code": "X", "ann_date": "20260115", "end_date": "20251231",
                 "type": "首亏", "p_change_min": -80, "p_change_max": -70},
                {"ts_code": "X", "ann_date": "20250410", "end_date": "20250331",
                 "type": "预增", "p_change_min": 30, "p_change_max": 40}])
        if api == "dividend":
            return pd.DataFrame([
                {"ts_code": "X", "end_date": "20251231", "ann_date": "20260401",
                 "cash_div": 21.5, "stk_div": 0.0}])
        if api == "stk_holdertrade":
            return pd.DataFrame([
                {"ts_code": "X", "ann_date": "20261020", "holder_name": "大股东A公司",
                 "holder_type": "G", "change_vol": -800.0, "change_ratio": -2.5},
                {"ts_code": "X", "ann_date": "20260901", "holder_name": "小股东",
                 "holder_type": "P", "change_vol": -3.0, "change_ratio": -0.01}])  # 噪音
        if api == "suspend":
            return pd.DataFrame([{"ts_code": "X", "suspend_date": "20200301",
                                  "resume_date": "20200310"}])
        if api == "pledge_stat":
            return pd.DataFrame([{"ts_code": "X", "end_date": "20261031",
                                  "pledge_ratio": 38.5}])
        return pd.DataFrame()
    monkeypatch.setattr(FS.T, "call", fake)
    n = FS.refresh_events("600519", env)
    ev = FS.events_payload("600519", env)["events"]
    titles = [e["title"] for e in ev]
    assert any("业绩预告: 首亏" in t for t in titles)
    fk = next(e for e in ev if "首亏" in e["title"])
    assert fk["risk"] == "high" and fk["date"] == "2026-01-15"
    assert any("减持800" in t for t in titles)
    dj = next(e for e in ev if "减持" in e["title"])
    assert dj["risk"] == "high"                       # 减持+|ratio|>=1
    assert not any("小股东" in t for t in titles)      # <50万股噪音滤除
    assert any("停牌" in t for t in titles)
    assert any("每10股派21.5" in t for t in titles)
    # 质押入独立表
    con = FS._conn(env)
    pr = con.execute("SELECT pledge_ratio FROM stock_pledge").fetchone()
    con.close()
    assert pr == (38.5,)


# ── 顶部风险提示 ──

def test_risk_payload(env, monkeypatch):
    import datetime as dt
    con = FS._conn(env)
    today = dt.date.today()
    con.execute("INSERT INTO stock_float VALUES(?,?,?,?,?,?)",
                ("600519.SH", "20261101", (today + dt.timedelta(days=30)).isoformat(),
                 5e7, 6.0, "定增股东"))
    con.execute("INSERT INTO stock_events VALUES(?,?,?,?,?,?)",
                ("600519.SH", (dt.date.today() - dt.timedelta(days=30)).isoformat(),
                 "forecast", "业绩预告: 首亏", "", "high"))
    con.execute("INSERT INTO stock_events VALUES(?,?,?,?,?,?)",
                ("600519.SH", "2020-01-01", "forecast", "业绩预告: 预增", "", "low"))
    con.execute("INSERT INTO stock_pledge VALUES('600519.SH','2026-10-31',38.5)")
    con.commit()
    con.close()
    r = FS.risk_payload("600519", env)
    texts = [i["text"] for i in r["items"]]
    assert any("解禁1批" in t and "6.0%" in t for t in texts)      # 未来90天大额
    assert any("质押比例38.5%" in t for t in texts)                 # ≥30%警示
    assert any("首亏" in t for t in texts)                          # 近180天高危
    assert not any("预增" in t for t in texts)                      # 2020年低危不入选
    assert r["checked"] is True


# ── API + UI ──

def test_a3_module_autofetch(server_app, monkeypatch):
    import fetch_stock as FS
    calls = []
    monkeypatch.setattr(FS, "float_payload",
                        lambda c, path=None: {"ok": True, "code": c, "n": 0, "data": [],
                                              "soon": {"n": 0}})
    monkeypatch.setattr(FS, "events_payload",
                        lambda c, path=None: {"ok": True, "code": c, "n": 0, "events": []})
    monkeypatch.setattr(FS, "risk_payload",
                        lambda c, path=None: {"ok": True, "code": c, "items": [], "checked": True})
    for fn in ("refresh_float", "refresh_events"):
        monkeypatch.setattr(FS, fn, lambda c, path=None: calls.append(c) or 0)
    c = server_app["client"]
    c.get("/admin/api/stock/float?code=600519")       # 空→拉取(归一化code)
    assert calls == ["600519.SH"]
    c.get("/admin/api/stock/events?code=600519")
    assert calls == ["600519.SH", "600519.SH"]
    # risk桩返回checked=True → 空items不触发重复拉取
    c.get("/admin/api/stock/risk?code=600519")
    assert len(calls) == 2
    assert c.get("/admin/api/stock/hack").status_code == 400


def test_a3_ui_markers():
    import re
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    for mk in ("riskBanner", "chartFloat", "evTable", "curEvents", "evByDate",
               "风险事件", "findIndex(x => x >= e.date)", "loadA3", "evCount"):
        assert mk in html, f"缺: {mk}"
    if shutil.which("node"):
        blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
        main = max(blocks, key=len)
        r = subprocess.run([shutil.which("node"), "-e",
                            "new Function(process.argv[1])", main],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f"JS语法错误: {r.stderr[:300]}"
