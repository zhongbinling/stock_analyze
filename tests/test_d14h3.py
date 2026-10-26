# -*- coding: utf-8 -*-
"""D1-14 H3: 宽基ETF链 — 分页续拉/回补/日增/守卫/注册"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import fetch_etf as FE  # noqa: E402


@pytest.fixture()
def etf_env(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    monkeypatch.setattr(FE, "connect", lambda: sqlite3.connect(str(p)))
    return {"path": p, "FE": FE}


def test_run_paginates_5000_cap(etf_env, monkeypatch):
    """单次5000行上限 → 分页续拉拼全史; 白名单8只; start过滤"""
    pages = []

    def fake(api, fields="", **kw):
        code, beg = kw.get("ts_code"), kw.get("start_date")
        if code == "510050.SH":
            pages.append(beg)
            if beg == "20050223":        # 首页满页4900+1行(触发翻页)
                days = [f"20{y:02d}{m:02d}{d:02d}" for y in range(5, 26)
                        for m in range(1, 13) for d in range(1, 29)][:4900]
                days.append("20250401")  # 页尾 → 次页起点20250402
                n = len(days)
                return pd.DataFrame({"trade_date": days, "close": [1.0] * n,
                                     "amount": [100.0] * n, "vol": [10.0] * n})
            if beg == "20250402":        # 次页不满页 → 结束
                return pd.DataFrame({"trade_date": ["20261026"], "close": [2.0],
                                     "amount": [50.0], "vol": [5.0]})
            return pd.DataFrame(columns=["trade_date", "close", "amount", "vol"])
        return pd.DataFrame({"trade_date": ["20260105"], "close": [1.5],
                             "amount": [80.0], "vol": [8.0]})   # 其余ETF单页短史
    monkeypatch.setattr(FE.T, "has_token", lambda: True)
    monkeypatch.setattr(FE.T, "call", fake)
    assert FE.run("20050223") is True
    assert pages == ["20050223", "20250402"]           # 翻页续拉发生
    c = sqlite3.connect(str(etf_env["path"]))
    n = c.execute("SELECT COUNT(*) FROM etf_daily WHERE code='510050.SH'").fetchone()[0]
    assert n == 4902                                    # 两页拼接
    assert c.execute("SELECT COUNT(DISTINCT code) FROM etf_daily").fetchone()[0] == 8
    c.close()


def test_run_rejects_bad_close(etf_env, monkeypatch):
    """守卫: close≤0/NaN 拒整批, 表零污染"""
    bad = pd.DataFrame({"trade_date": ["20261026"], "close": [-1.0],
                        "amount": [1.0], "vol": [1.0]})
    monkeypatch.setattr(FE.T, "has_token", lambda: True)
    monkeypatch.setattr(FE.T, "call", lambda api, fields="", **kw: bad)
    assert FE.run("20050223") is False
    c = sqlite3.connect(str(etf_env["path"]))
    assert c.execute("SELECT COUNT(*) FROM etf_daily").fetchone()[0] == 0
    c.close()


def test_refresh_daily(etf_env, monkeypatch):
    """日增: 逐ETF单日查询聚一批, 当日守卫写入"""
    calls = []

    def fake(api, fields="", **kw):
        calls.append(kw.get("ts_code"))
        if kw.get("ts_code") == "588000.SH":
            return pd.DataFrame(columns=["trade_date", "close", "amount", "vol"])  # 未出数
        return pd.DataFrame({"trade_date": ["20261026"], "close": [4.1],
                             "amount": [1000.0], "vol": [20.0]})
    monkeypatch.setattr(FE.T, "call", fake)
    assert FE.refresh(conn=sqlite3.connect(str(etf_env["path"]))) is True
    assert len(calls) == 8
    c = sqlite3.connect(str(etf_env["path"]))
    n = c.execute("SELECT COUNT(*) FROM etf_daily").fetchone()[0]
    assert n == 7                                       # 7只出数
    c.close()


def test_etf_registered():
    import fetch_daily as FD2
    assert "etf_daily" in FD2.BACKFILLS
    from server import jobs
    assert "etf_daily" in jobs.BACKFILL_DATASETS
    assert jobs.BACKFILL_DEFAULT_START["etf_daily"] == "20050223"
    from server import market
    assert any(s[0] == "etf" for s in market._ASSET_DATE_TABLES)
    assert market._MIN_ROW_RATIO["etf_daily"] == (0.6, 0.3)
    src = Path(CORE / "fetch_daily.py").read_text(encoding="utf-8")
    assert "fetch_etf.refresh()" in src                 # 更新链挂载
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "etf_daily" in html and "自2005-02起" in html


def test_refresh_mixed_dates_written_separately(etf_env, monkeypatch):
    """D1-14r: 混日批次(部分ETF回落他日行)按日分组写入, 互不拖累"""
    def fake(api, fields="", **kw):
        if kw.get("ts_code") == "159915.SZ":       # 未出数→接口回落昨日
            return pd.DataFrame({"trade_date": ["20261023"], "close": [2.0],
                                 "amount": [10.0], "vol": [1.0]})
        return pd.DataFrame({"trade_date": ["20261026"], "close": [4.1],
                             "amount": [1000.0], "vol": [20.0]})
    monkeypatch.setattr(FE.T, "call", fake)
    assert FE.refresh(conn=sqlite3.connect(str(etf_env["path"]))) is True
    c = sqlite3.connect(str(etf_env["path"]))
    n26 = c.execute("SELECT COUNT(*) FROM etf_daily WHERE date='2026-10-26'").fetchone()[0]
    n23 = c.execute("SELECT COUNT(*) FROM etf_daily WHERE date='2026-10-23'").fetchone()[0]
    assert n26 == 7 and n23 == 1                     # 两日各自落库, 谁也没拖累谁
    c.close()


def test_refresh_lookback_heals_missed_days(etf_env, monkeypatch):
    """D1-14r: 日增回看7天 — 错过日自动补"""
    def fake(api, fields="", **kw):
        return pd.DataFrame({"trade_date": ["20261023", "20261027"],
                             "close": [4.1, 4.2], "amount": [1000.0, 1100.0],
                             "vol": [20.0, 21.0]})
    monkeypatch.setattr(FE.T, "call", fake)
    assert FE.refresh(conn=sqlite3.connect(str(etf_env["path"]))) is True
    c = sqlite3.connect(str(etf_env["path"]))
    for d in ("2026-10-23", "2026-10-27"):
        n = c.execute("SELECT COUNT(*) FROM etf_daily WHERE date=?", (d,)).fetchone()[0]
        assert n == 8                              # 8只全落
    c.close()
