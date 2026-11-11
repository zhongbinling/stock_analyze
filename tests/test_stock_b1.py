# -*- coding: utf-8 -*-
"""避雷针B1: 财务指标全景 — 四表拉取/扩列迁移/四组载荷/机构聚合/API/UI"""
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


# ── 扩列迁移: 旧库(A4建的窄列stock_bs)升级到B1宽列 ──

def test_schema_migration_adds_columns(env):
    """旧库(stock_bs五列)经_conn自动扩列到B1宽列"""
    con = sqlite3.connect(str(env))
    con.execute("CREATE TABLE stock_bs(ts_code TEXT, end_date TEXT, goodwill REAL,"
                " total_assets REAL, total_liab REAL, PRIMARY KEY(ts_code,end_date))")
    con.execute("CREATE TABLE stock_finance(ts_code TEXT, end_date TEXT,"
                " profit_dedt REAL, PRIMARY KEY(ts_code,end_date))")
    con.execute("CREATE TABLE stock_val(ts_code TEXT, trade_date TEXT, pe_ttm REAL,"
                " PRIMARY KEY(ts_code,trade_date))")
    con.commit()
    con.close()
    con = FS._conn(env)                     # 触发迁移
    bs_cols = {r[1] for r in con.execute("PRAGMA table_info(stock_bs)")}
    fin_cols = {r[1] for r in con.execute("PRAGMA table_info(stock_finance)")}
    val_cols = {r[1] for r in con.execute("PRAGMA table_info(stock_val)")}
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"money_cap", "accounts_receiv", "acct_payable", "inventories"} <= bs_cols
    assert {"netprofit_margin", "inv_turn", "ar_turn"} <= fin_cols
    assert "dv_ttm" in val_cols
    assert {"stock_income", "stock_cashflow"} <= tables


def test_refresh_income_cashflow_bs(env, monkeypatch):
    dates = ["20260331", "20260630", "20260930"]
    dfs = {
        "income": pd.DataFrame({"ts_code": ["600519.SH"]*3, "end_date": dates,
                                "revenue": [3e10]*3, "total_revenue": [3.1e10]*3,
                                "n_income": [1.5e10]*3}),
        "cashflow": pd.DataFrame({"ts_code": ["600519.SH"]*3, "end_date": dates,
                                  "n_cashflow_act": [1.2e10, 1.4e10, 1.1e10]}),
        "balancesheet": pd.DataFrame({"ts_code": ["600519.SH"]*3, "end_date": dates,
                                      "money_cap": [5e10]*3, "accounts_receiv": [1e9]*3,
                                      "acct_payable": [2e9]*3, "inventories": [4e10]*3,
                                      "goodwill": [None]*3, "total_assets": [3e11]*3,
                                      "total_liab": [5e10]*3, "total_cur_assets": [None]*3,
                                      "total_cur_liab": [None]*3}),
    }
    monkeypatch.setattr(FS.T, "call", lambda api, fields="", **kw: dfs[api])
    assert FS.refresh_income("600519", env) == 3
    assert FS.refresh_cashflow("600519", env) == 3
    assert FS.refresh_bs("600519", env) == 3
    con = FS._conn(env)
    n1 = con.execute("SELECT COUNT(*) FROM stock_income").fetchone()[0]
    mc = con.execute("SELECT money_cap FROM stock_bs WHERE end_date='2026-03-31'").fetchone()[0]
    con.close()
    assert n1 == 3 and mc == 5e10


# ── 全景载荷: 四组指标 ──

def _seed_full(env):
    con = FS._conn(env)
    con.executemany("INSERT INTO stock_income VALUES(?,?,?,?,?)", [
        ("600519.SH", "2026-06-30", 3e10, 3.1e10, 1.5e10),
        ("600519.SH", "2026-09-30", 3.2e10, 3.3e10, 1.6e10)])
    con.executemany("INSERT INTO stock_cashflow VALUES(?,?,?)", [
        ("600519.SH", "2026-06-30", 1.2e10), ("600519.SH", "2026-09-30", 1.4e10)])
    con.executemany("INSERT INTO stock_finance(ts_code,end_date,profit_dedt,or_yoy,"
                    "netprofit_yoy,grossprofit_margin,netprofit_margin,roe,"
                    "debt_to_assets,inv_turn,ar_turn) VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
        ("600519.SH", "2026-06-30", 1.4e10, 10.0, 8.0, 91.0, 50.0, 15.0, 20.0, 0.8, 55.0),
        ("600519.SH", "2026-09-30", 1.5e10, 12.0, 9.0, 90.0, 49.0, 16.0, 21.0, 0.9, 52.0)])
    con.executemany("INSERT INTO stock_bs(ts_code,end_date,money_cap,"
                    "accounts_receiv,acct_payable,inventories,goodwill,"
                    "total_assets,total_liab) VALUES(?,?,?,?,?,?,?,?,?)", [
        ("600519.SH", "2026-06-30", 5e10, 1e9, 2e9, 4e10, 3e8, 3e11, 5e10),
        ("600519.SH", "2026-09-30", 5.2e10, 1.1e9, 2.1e9, 4.2e10, 3e8, 3.1e11, 5.2e10)])
    con.execute("INSERT INTO stock_val(ts_code,trade_date,pe_ttm,dv_ttm) "
                "VALUES('600519.SH','2026-11-07',22.0,3.1)")
    con.execute("INSERT INTO stock_pledge VALUES('600519.SH','2026-10-31',0.5)")
    for ed, rows in {"2026-06-30": [("香港中央结算", 1e8, 6.0), ("某基金A", 5e7, 3.0),
                                     ("张三个人", 3e7, 1.0)],
                     "2026-09-30": [("香港中央结算", 1.1e8, 6.5), ("某基金A", 6e7, 3.5)]}.items():
        con.executemany("INSERT INTO stock_holders VALUES('600519.SH',?,?,?,?)",
                        [(ed, n, a, r) for n, a, r in rows])
    con.executemany("INSERT INTO stock_holdernum VALUES(?,?,?)",
                    [("600519.SH", "2026-06-30", 8e4), ("600519.SH", "2026-09-30", 8.5e4)])
    con.commit()
    con.close()


def test_finance_full_payload_groups(env):
    _seed_full(env)
    d = FS.finance_full_payload("600519", env)
    assert d["ok"] and d["dates"] == ["2026-06-30", "2026-09-30"]
    # 盈利组: 元→亿/增速/比率/股息率
    assert d["profit"]["revenue"] == [300.0, 320.0]
    assert d["profit"]["n_income"] == [150.0, 160.0]
    assert d["profit"]["or_yoy"] == [10.0, 12.0]
    assert d["profit"]["npm"] == [50.0, 49.0]
    assert d["profit"]["dv_ttm"] == 3.1
    # 现金流组
    assert d["cashflow"]["ocf"] == [120.0, 140.0]
    assert d["cashflow"]["cash"] == [500.0, 520.0]
    assert d["cashflow"]["inv_turn"] == [0.8, 0.9]
    assert d["cashflow"]["ar_turn"] == [55.0, 52.0]   # review修复: 占位符→真实管线
    # 资产负债组: 净资产自算=总资产-总负债, 商誉占比=商誉/净资产
    assert d["balance"]["assets"] == [3000.0, 3100.0]
    assert d["balance"]["gw_eq"] == [round(3e8 / (3e11-5e10) * 100, 1),
                                     round(3e8 / (3.1e11-5.2e10) * 100, 1)]
    # 筹码组: 机构聚合(香港中央结算=北向+基金=公募, 个人不计) 户数
    # inst取近3期按日期DESC(新到旧), 与dates顺序一致
    assert d["inst"]["ratio"] == [10.0, 9.0]     # 0930: 6.5+3.5 / 0630: 6.0+3.0(个人不计)
    assert d["inst"]["holder_num"] == [8.5e4, 8e4]
    assert d["pledge_ratio"] == 0.5


def test_full_api_autofetch(server_app, monkeypatch):
    import fetch_stock as FS
    c = server_app["client"]
    monkeypatch.setattr(FS, "finance_full_payload",
                        lambda code, n_qua=12, path=None: {"ok": True, "dates": []})
    refs = []
    for fn in ("refresh_income", "refresh_cashflow", "refresh_finance", "refresh_bs"):
        monkeypatch.setattr(FS, fn, lambda c2, path=None: refs.append(1) or 1)
    r = c.get("/admin/api/stock/full?code=600519")
    assert r.status_code == 200 and refs == [1, 1, 1, 1]
    assert c.get("/admin/api/stock/hack").status_code == 400


def test_b1_ui_and_js():
    import re
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    for mk in ("盈利 · 收入与利润", "盈利 · 利润率与回报", "现金流", "营运资金",
               "资产负债", "杠杆与商誉风险", "筹码结构",
               "chartG1", "chartG1b", "chartG2", "chartG2b",
               "chartG3", "chartG3b", "chartG4",
               "renderFull", "loadB1", "_gridOpt", "机构持仓占比",
               "商誉占净资产", "dv_ttm", "质押率"):
        assert mk in html, f"缺: {mk}"
    assert "${{" not in html                          # Jinja陷阱零残留
    if shutil.which("node"):
        blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
        main = max(blocks, key=len)
        r = subprocess.run([shutil.which("node"), "-e",
                            "new Function(process.argv[1])", main],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        assert r.returncode == 0, f"JS语法错误: {(r.stderr or '')[:300]}"


def test_goodwill_graceful_none():
    """商誉无数据公司(茅台): 不画空系列, 显示'无商誉'提示"""
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    assert "hasGW" in html and "无商誉" in html
    assert "if (hasGW)" in html          # 条件入系列


def test_event_card_last_and_datazoom():
    """事件卡在最后 + 全部B1图有dataZoom滑块"""
    import re
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    h2s = re.findall(r"<h2>([^<]+)", html)
    assert "个股大事件" in h2s[-1] or "个股大事件" in h2s[-2], f"事件卡不在尾部: {h2s[-3:]}"
    # _gridOpt含slider dataZoom
    assert "type: 'slider'" in html
    assert html.count("type: 'slider'") >= 3     # _gridOpt+g4+float
