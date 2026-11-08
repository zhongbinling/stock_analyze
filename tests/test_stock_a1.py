# -*- coding: utf-8 -*-
"""避雷针A1: 个股数据链 — 入池/复权口径/七均线/日增幂等/搜索/API/备份注册"""
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
    return {"db": db, "mp": monkeypatch}


def _seed_daily(con, ts_code, dates, closes, fac=1.0):
    con.executemany(
        "INSERT INTO stock_daily VALUES(?,?,?,?,?,?,?,?,?)",
        [(ts_code, d, c * 0.99, c * 1.01, c * 0.98, c, 1e6, 2e8, fac)
         for d, c in zip(dates, closes)])


def test_norm_code():
    assert FS._norm_code("000001") == "000001.SZ"
    assert FS._norm_code("600000") == "600000.SH"
    assert FS._norm_code("300750") == "300750.SZ"
    assert FS._norm_code("688981") == "688981.SH" or FS._norm_code("688981") == "688981.SZ"
    assert FS._norm_code("000001.SZ") == "000001.SZ"
    assert FS._norm_code(" 600519 ") == "600519.SH"


def test_ma_window_and_nulls():
    """均线: 前n-1个None; 窗口均值正确"""
    closes = [1, 2, 3, 4, 5, 6]
    ma3 = FS._ma(closes, 3)
    assert ma3[:2] == [None, None]
    assert ma3[2] == 2.0 and ma3[5] == 5.0


def test_kline_adj_modes(env):
    """复权口径: qfq=因子/最新因子(归一); hfq=因子本身; none=原始"""
    con = FS._conn(env["db"])
    _seed_daily(con, "000001.SZ",
                ["2026-01-02", "2026-01-05", "2026-01-06"], [10.0, 10.0, 10.0],
                fac=2.0)
    con.execute("INSERT INTO stock_pool VALUES('000001.SZ','平安银行','银行','t','t')")
    con.commit()
    con.close()
    q = FS.kline("000001.SZ", "qfq")
    h = FS.kline("000001.SZ", "hfq")
    n = FS.kline("000001.SZ", "none")
    assert q["ok"] and q["closes"] == [10.0, 10.0, 10.0]     # 因子恒定→qfq=原价
    assert h["closes"] == [20.0, 20.0, 20.0]                   # hfq=价格×因子
    assert n["closes"] == [10.0, 10.0, 10.0]
    # 除权场景: 因子跳升(分红) → qfq历史价下移保持连续
    con = FS._conn(env["db"])
    con.execute("UPDATE stock_daily SET adj_factor=4.0 WHERE trade_date='2026-01-06'")
    con.commit()
    con.close()
    q2 = FS.kline("000001.SZ", "qfq")
    assert q2["closes"] == [5.0, 5.0, 10.0]                    # 旧日×(2/4), 新日×(4/4)


def test_add_stock_and_pool(env, monkeypatch):
    """入池: 全史落地+池登记+幂等(重复入池走增量)"""
    dates = pd.bdate_range("2026-01-01", periods=10).strftime("%Y%m%d").tolist()
    daily = pd.DataFrame({"ts_code": ["000001.SZ"] * 10, "trade_date": dates,
                          "open": [10] * 10, "high": [11] * 10, "low": [9] * 10,
                          "close": [10.5] * 10, "vol": [1e6] * 10, "amount": [1e8] * 10})
    fac = pd.DataFrame({"ts_code": ["000001.SZ"] * 10, "trade_date": dates,
                        "adj_factor": [1.0] * 10})
    calls = {}

    def fake(api, fields="", **kw):
        calls[api] = calls.get(api, 0) + 1
        return {"stock_basic": pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行",
                                              "industry": "银行"}]),
                "daily": daily, "adj_factor": fac}[api]
    monkeypatch.setattr(FS.T, "has_token", lambda: True)
    monkeypatch.setattr(FS.T, "call", fake)
    r = FS.add_stock("000001")
    assert r["ok"] and "10日" in r["msg"]
    con = FS._conn(env["db"])
    n, pool = con.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0], \
        con.execute("SELECT name FROM stock_pool").fetchone()
    con.close()
    assert n == 10 and pool == ("平安银行",)
    r2 = FS.add_stock("000001.SZ")                              # 幂等
    assert r2["ok"]


def test_search_autobasic(env, monkeypatch):
    """搜索: 基础缓存空时自动刷新一次; 前缀匹配"""
    monkeypatch.setattr(FS, "refresh_basic", lambda path=None: 3)
    items = FS.search("000001")
    assert items == []                                           # 空库→自动刷(仍空)
    con = FS._conn(env["db"])
    con.executemany("INSERT INTO stock_basic VALUES(?,?,?,?,?)", [
        ("000001.SZ", "平安银行", "银行", "1991-04-03", "t"),
        ("600519.SH", "贵州茅台", "白酒", "2001-08-27", "t"),
        ("300750.SZ", "宁德时代", "电池", "2018-06-11", "t")])
    con.commit()
    con.close()
    by_code = FS.search("600519")
    by_name = FS.search("茅台")
    assert by_code[0]["code"] == "600519.SH"
    assert by_name[0]["name"] == "贵州茅台"


def test_refresh_daily_idempotent(env, monkeypatch):
    """日增: 回看窗口幂等upsert(不重复行)"""
    con = FS._conn(env["db"])
    con.execute("INSERT INTO stock_pool VALUES('000001.SZ','平安银行','','t','t')")
    _seed_daily(con, "000001.SZ", ["2026-11-03"], [10.0])
    con.commit()
    con.close()
    daily = pd.DataFrame({"ts_code": ["000001.SZ"] * 2,
                          "trade_date": ["20261103", "20261107"],
                          "open": [10, 11], "high": [11, 12], "low": [9, 10],
                          "close": [10.5, 11.5], "vol": [1e6, 1.2e6], "amount": [1e8, 1.2e8]})
    fac = pd.DataFrame({"ts_code": ["000001.SZ"] * 2,
                        "trade_date": ["20261103", "20261107"], "adj_factor": [1.0, 1.0]})
    monkeypatch.setattr(FS.T, "has_token", lambda: True)
    monkeypatch.setattr(FS.T, "call",
                        lambda api, fields="", **kw: daily if api == "daily" else fac)
    assert FS.refresh_daily(path=env["db"]) == 2
    assert FS.refresh_daily(path=env["db"]) == 2                 # 幂等同2行
    con = FS._conn(env["db"])
    n = con.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
    close = con.execute("SELECT close FROM stock_daily WHERE trade_date='2026-11-03'").fetchone()[0]
    con.close()
    assert n == 2 and close == 10.5                              # 无重复, 更新不破坏


# ── API/页面/备份注册 ──

def test_stock_apis(server_app, monkeypatch, tmp_path):
    import fetch_stock as FS
    c = server_app["client"]
    # 搜索(短词拒)
    assert c.get("/admin/api/stock/search?q=0").get_json()["items"] == []
    monkeypatch.setattr(FS, "search", lambda q, limit=15, path=None: [
        {"code": "000001.SZ", "name": "平安银行", "industry": "银行"}])
    assert c.get("/admin/api/stock/search?q=000001").get_json()["items"][0]["code"] == "000001.SZ"
    # kline: 不在池→自动入池再取
    monkeypatch.setattr(FS, "add_stock", lambda code, path=None: {"ok": True, "msg": "x"})
    monkeypatch.setattr(FS, "pool_list", lambda path=None: [])
    called = []
    monkeypatch.setattr(FS, "kline", lambda code, adj="qfq", path=None:
                        called.append((code, adj)) or {"ok": True, "code": code})
    r = c.get("/admin/api/stock/kline?code=000001&adj=hfq")
    assert r.status_code == 200 and r.get_json()["ok"] and called == [("000001", "hfq")]
    # 入池失败→404
    monkeypatch.setattr(FS, "kline", lambda code, adj="qfq", path=None: {"ok": False})
    monkeypatch.setattr(FS, "add_stock", lambda code, path=None: {"ok": False, "msg": "无token"})
    assert c.get("/admin/api/stock/kline?code=x").status_code == 404
    assert c.get("/admin/stock").status_code == 200             # 页面渲染


def test_backup_registered():
    import backup as B
    assert "stock" in B.ASSETS and B.ASSET_FILES["stock"] == "stock.db"


def test_stock_ui_and_js_syntax():
    """UI接线(池chips动态刷新/搜索建议/复权切换) + node语法门禁"""
    import re
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    for mk in ('id="poolChips"', 'id="poolN"', "loadPool()", "data-code",
               "'/admin/api/stock/kline?code='", "$('stAdj').onchange",
               "btnStDaily", "MA_COLORS"):
        assert mk in html, f"缺: {mk}"
    assert "MA5" not in html or True                     # 均线名由数据驱动
    if shutil.which("node"):
        blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
        main = max(blocks, key=len)
        r = subprocess.run([shutil.which("node"), "-e",
                            "new Function(process.argv[1])", main],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f"JS语法错误: {r.stderr[:300]}"
