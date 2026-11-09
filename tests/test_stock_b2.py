# -*- coding: utf-8 -*-
"""避雷针B2: 一键扫雷 — 数据状态/全维度输入/AI扫雷prompt/缓存回看/API/UI"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import fetch_stock as FS  # noqa: E402
import stock_risk as SR   # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "stock.db"
    monkeypatch.setattr(FS, "STOCK_DB", db)
    monkeypatch.setattr(SR, "_conn", lambda path=None: FS._conn(path))
    return db


def _seed_scan(env, with_events=True):
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','茅台','','t','t')")
    con.executemany("INSERT INTO stock_finance(ts_code,end_date,profit_dedt,"
                    "netprofit_yoy,or_yoy,grossprofit_margin,roe,debt_to_assets)"
                    " VALUES(?,?,?,?,?,?,?,?)", [
        ("600519.SH", "2026-06-30", 1.4e10, 8.0, 10.0, 91.0, 15.0, 20.0),
        ("600519.SH", "2026-09-30", 1.5e10, 9.0, 12.0, 90.0, 16.0, 21.0)])
    if with_events:
        con.execute("INSERT INTO stock_events VALUES(?,?,?,?,?,?)",
                    ("600519.SH", "2026-08-01", "holdertrade",
                     "大股东减持500万股", "", "high"))
    con.commit()
    con.close()


# ── 数据状态 ──

def test_data_freshness_tiers(env):
    """季频表看月份级(120日), 日频看7日; 空表不新鲜"""
    _seed_scan(env)
    d = SR.data_freshness("600519", env)
    assert d["财务"]["fresh"] is True          # 2026-09期在120日内
    assert d["日线"]["fresh"] is False          # 无日线数据


def test_update_all_orchestrates(env, monkeypatch):
    """更新全部: add_stock+9数据集"""
    calls = []
    monkeypatch.setattr(FS, "add_stock", lambda c, path=None: {"ok": True})
    for fn in ("refresh_margin", "refresh_val", "refresh_finance", "refresh_bs",
               "refresh_income", "refresh_cashflow", "refresh_holders",
               "refresh_float", "refresh_events"):
        monkeypatch.setattr(FS, fn, lambda c, path=None, _f=fn: calls.append(_f) or 1)
    r = SR.update_all("600519", env)
    assert r["日线"] == 1 and len(calls) == 9 and set(calls) == {
        "refresh_margin", "refresh_val", "refresh_finance", "refresh_bs",
        "refresh_income", "refresh_cashflow", "refresh_holders",
        "refresh_float", "refresh_events"}


# ── 扫雷输入组装 ──

def test_build_scan_input_dimensions(env):
    """全维度: 财务+估值+近一年事件(低危滤除)+股价表现"""
    _seed_scan(env)
    con = FS._conn(env)
    con.execute("INSERT INTO stock_events VALUES(?,?,?,?,?,?)",
                ("600519.SH", "2020-01-01", "dividend", "老分红", "", "low"))
    con.commit()
    con.close()
    inp = SR._build_scan_input("600519", env)
    assert inp["full"]["ok"] is True or len(inp["full"]["dates"]) >= 0
    evs = [e["title"] for e in inp["events"]]
    assert any("减持" in t for t in evs)        # 近一年高危入选
    assert not any("老分红" in t for t in evs)   # 6年前低危出局
    assert "chg_1y" in inp                       # 无K线时None不炸


# ── 扫雷主流程: 缓存/force/incomplete/AI桩 ──

def test_scan_incomplete_and_cache(env, monkeypatch):
    _seed_scan(env)
    import analyze_news as AN
    monkeypatch.setattr(AN, "get_ai_config",
                        lambda: {"enabled": False, "model": "", "api_key": "",
                                 "base_url": ""})
    r = SR.scan_stock("600519", env)
    assert r["ok"] and r["cached"] is False and r["grade"] == "A"  # 仅规则
    r2 = SR.scan_stock("600519", env)                               # 缓存命中
    assert r2["cached"] is True
    r3 = SR.scan_stock("600519", env, force=True)                   # force重扫
    assert r3["cached"] is False
    # 空库incomplete
    con = FS._conn(env)
    con.execute("DELETE FROM stock_finance")
    con.commit()
    con.close()
    r4 = SR.scan_stock("600519", env)
    assert r4["ok"] is False and r4["incomplete"] is True


def test_scan_ai_stub_dimensions(env, monkeypatch):
    """AI桩: SCAN_PROMPT触发, 输出带dimension的点; 旧A4入口兼容"""
    _seed_scan(env)
    import analyze_news as AN
    import ai_client
    monkeypatch.setattr(AN, "get_ai_config",
                        lambda: {"enabled": True, "model": "m", "api_key": "",
                                 "base_url": "http://x"})
    captured = {}

    def fake_chat(url, model, msgs, **kw):
        captured["prompt"] = msgs[0]["content"]
        return json.dumps({"summary": "测试", "points": [
            {"title": "减持压力", "evidence": "减持500万股", "severity": "高",
             "dimension": "事件"},
            {"title": "业绩", "evidence": "净利同比=9.0%", "severity": "低",
             "dimension": "财务"}], "advice": "关注"})
    monkeypatch.setattr(ai_client, "chat", fake_chat)
    r = SR.scan_stock("600519", env, force=True)
    assert r["ok"]
    assert "扫雷" in captured["prompt"] and "减持" in captured["prompt"]
    assert r["ai"]["points"][0]["dimension"] == "事件"
    # 兼容入口
    r5 = SR.analyze_stock_finance("600519", env)
    assert r5["cached"] is True


# ── API + UI ──

def test_b2_apis(server_app, monkeypatch):
    import stock_risk as SR
    c = server_app["client"]
    monkeypatch.setattr(SR, "data_freshness",
                        lambda code: {"日线": {"last": "2026-11-07", "fresh": True}})
    r = c.get("/admin/api/stock/freshness?code=x")
    assert r.status_code == 200 and r.get_json()["datasets"]["日线"]["fresh"]
    monkeypatch.setattr(SR, "update_all", lambda code: {"日线": 1})
    r2 = c.post("/admin/api/stock/update_all", json={"code": "x"})
    assert r2.status_code == 200 and r2.get_json()["ok"]
    monkeypatch.setattr(SR, "scan_stock",
                        lambda code, force=False: {"ok": False, "incomplete": True, "msg": "x"})
    assert c.post("/admin/api/stock/scan", json={"code": "x"}).status_code == 404
    monkeypatch.setattr(SR, "scan_stock",
                        lambda code, force=False: {"ok": True, "cached": True, "grade": "A"})
    assert c.post("/admin/api/stock/scan", json={"code": "x", "force": True}).status_code == 200


def test_b2_ui_and_js():
    import re
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    for mk in ("一键扫雷", "btnSkUpd", "loadFresh", "renderFresh", "freshRow",
               "/admin/api/stock/scan", "update_all", "freshness",
               "DIM_COLOR", "[${p.dimension}]", "上次扫雷结果", "未扫雷"):
        assert mk in html, f"缺: {mk}"
    assert "${{" not in html
    if shutil.which("node"):
        blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
        main = max(blocks, key=len)
        r = subprocess.run([shutil.which("node"), "-e",
                            "new Function(process.argv[1])", main],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        assert r.returncode == 0, f"JS语法错误: {(r.stderr or '')[:300]}"
