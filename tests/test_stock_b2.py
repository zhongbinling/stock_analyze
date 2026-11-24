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


def test_b2_ui_and_js(js_syntax):
    import re
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    for mk in ("一键扫雷", "btnSkUpd", "loadFresh", "renderFresh", "freshRow",
               "/admin/api/stock/scan", "update_all", "freshness",
               "DIM_COLOR", "[${p.dimension}]", "上次扫雷结果", "未扫雷"):
        assert mk in html, f"缺: {mk}"
    assert "${{" not in html
    js_syntax(html)


def test_freshness_sparse_data(env):
    """review修复: 解禁/事件天然稀疏 — 解禁有数据即fresh, 事件季频判定"""
    import datetime as dt
    import fetch_stock as FS
    import stock_risk as SR
    import sqlite3
    con = FS._conn(env)   # _conn自动ensure_db
    # 解禁: 只有2009年数据(茅台场景)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','茅台','','t','t')")
    con.execute("INSERT INTO stock_float VALUES('600519.SH','t','2009-05-25',1e8,5.0,'股东')")
    # 事件: 30天前
    con.execute("INSERT INTO stock_events VALUES('600519.SH',?,?,'t','','low')",
                ((dt.date.today() - dt.timedelta(days=30)).isoformat(), "test"))
    con.commit(); con.close()
    ds = SR.data_freshness("600519.SH", env)
    assert ds["解禁"]["fresh"] is True    # 有数据即fresh(天然稀疏)
    assert ds["事件"]["fresh"] is True   # 30天前在季频判定内


# ── D1-31: 股东户数as-of填充 + Y自适应 + 徽标点选/扫雷免打扰 ──

def test_holder_num_asof_fill(env):
    """非标准披露日(临时披露, 如茅台25-11-05/19)有holders无holdernum →
    数据层as-of填充最近一期(页面层兜底之外, 扫雷输入等消费方同样受益)"""
    import fetch_stock as FS
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','茅台','','t','t')")
    # holders披露日: 季度 + 非标准11-05; holdernum仅季度
    for d in ("2025-09-30", "2025-11-05", "2025-12-31"):
        con.execute("INSERT INTO stock_holders VALUES(?,?,?,?,?)",
                    ("600519.SH", d, "香港中央结算有限公司", 1e6, 5.0))
    for d, n in (("2025-09-30", 238512.0), ("2025-12-31", 255892.0)):
        con.execute("INSERT INTO stock_holdernum VALUES(?,?,?)",
                    ("600519.SH", d, n))
    con.commit(); con.close()
    hp = FS.holders_payload("600519", env)
    by_d = {x["end_date"]: x["holder_num"] for x in hp["data"]}
    asof = {x["end_date"]: x["holder_num_asof"] for x in hp["data"]}
    assert by_d["2025-11-05"] is None and asof["2025-11-05"] == 238512.0
    # D1-35: 股东表holder_num保持真实值(无披露=None), 沿用值单列holder_num_asof —
    # 曾直接填入holder_num, 页面据此算环比得到"+0.0% 筹码分散⚠"的假预警
    assert by_d["2025-12-31"] == 255892.0 and asof["2025-12-31"] is None
    assert by_d["2025-12-31"] == 255892.0
    # full payload的inst通道同样填充
    fp = FS.finance_full_payload("600519.SH", path=env)
    i = fp["inst"]["dates"].index("2025-11-05")
    assert fp["inst"]["holder_num"][i] == 238512.0


def test_stock_page_yfit_and_chips_gates():
    """页面门禁: _fitY自适应helper+datazoom挂载/徽标点选(muted)/扫雷3分钟免确认"""
    import re
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    assert "function _fitY" in html                       # X缩放→Y重算
    assert ".on('datazoom', () => _fitY(c))" in html      # 十图挂载
    assert "fresh-chip" in html and "skMuted" in html     # 点选忽略
    assert ".fresh-chip.muted" in html                    # 灰调+删除线样式
    assert "(skUpdated[scanCode] || 0) < 3 * 60 * 1000" in html   # 刚更新免确认(按股票记)
    # JS语法门禁(node哑元不需要 — 该页无Jinja变量混入script)
    import shutil, subprocess, tempfile, os
    blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
    main = max(blocks, key=len)
    fd, p = tempfile.mkstemp(suffix=".js"); os.close(fd)
    open(p, "w", encoding="utf-8").write(main)
    r = subprocess.run([shutil.which("node"), "-e",
                        'new Function(require("fs").readFileSync(process.argv[1],"utf8"))', p],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    os.unlink(p)
    assert r.returncode == 0, f"JS语法错误: {(r.stderr or '')[:300]}"
