# -*- coding: utf-8 -*-
"""C1: ETF净流入/流出 — 份额差×收盘价 + viewer子图 + fetch_daily挂载"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    import fetch_etf_flow as EF
    monkeypatch.setattr(EF, "MACRO_DB", db)
    return db


def _seed(env):
    import fetch_etf_flow as EF
    con = EF.ensure_db(env)
    con.execute("CREATE TABLE IF NOT EXISTS etf_daily(code TEXT, date TEXT, close REAL)")
    # etf_share: 3日×2ETF
    for d, shares in {"20261110": {"510050.SH": 10000, "510300.SH": 20000},
                      "20261111": {"510050.SH": 11000, "510300.SH": 19500},
                      "20261112": {"510050.SH": 10500, "510300.SH": 21000}}.items():
        for c, s in shares.items():
            con.execute("INSERT INTO etf_share VALUES(?,?,?)", (c, d, s))
    # etf_daily收盘价(同库, ISO日期)
    for d, prices in {"2026-11-11": {"510050.SH": 3.0, "510300.SH": 4.0},
                      "2026-11-12": {"510050.SH": 3.1, "510300.SH": 4.1}}.items():
        for c, p in prices.items():
            con.execute("INSERT INTO etf_daily(code,date,close) VALUES(?,?,?)", (c, d, p))
    con.commit()
    con.close()


def test_net_flow_calculation(env):
    """净流入 = 份额差×收盘价: 510050(+10×3.0=+30万→0.003亿) 510030(-5×4.0=-20万)"""
    _seed(env)
    import fetch_etf_flow as EF
    s = EF.net_flow_series(env, n_days=10)
    assert len(s) == 2
    # 11-11: 510050 (110-100)×3.0=30万=0.003亿; 510300 (195-200)×4.0=-20万=-0.002亿
    d11 = next(x for x in s if x["date"] == "2026-11-11")
    # 510050: (11000-10000)×3.0/1e4=0.3亿; 510300: (19500-20000)×4.0/1e4=-0.2亿
    assert d11["flows"]["510050.SH"] == 0.3
    assert d11["flows"]["510300.SH"] == -0.2
    assert d11["net_yi"] == 0.1
    # 11-12: 510050 (105-110)×3.1=-15.5万; 510300 (210-195)×4.1=61.5万
    d12 = next(x for x in s if x["date"] == "2026-11-12")
    assert d12["flows"]["510050.SH"] == round((10500 - 11000) * 3.1 / 1e4, 2)  # -0.16


def test_viewer_payload_has_flow():
    """viewer payload含etf_flow字段"""
    src = (CORE / "viewer.py").read_text(encoding="utf-8")
    assert "etf_flow" in src and "net_flow_series" in src
    # PANELS注册 + 勾选框 + 空数据守卫
    assert "ckEtfFlow" in src and "etfFlowSeries" in src
    assert "etfFlowMap" in src and "auxDataEmpty" in src


def test_fetch_daily_mounts_flow():
    """fetch_daily挂载etf_flow.refresh(日增链)"""
    src = (CORE / "fetch_daily.py").read_text(encoding="utf-8")
    assert "fetch_etf_flow" in src and "refresh()" in src


def test_refresh_stores(env, monkeypatch):
    """refresh入库: fund_share upsert到etf_share表"""
    import fetch_etf_flow as EF
    import pandas as pd
    df = pd.DataFrame({"ts_code": ["510050.SH"] * 2,
                        "trade_date": ["20261112", "20261111"],
                        "fd_share": [100.0, 99.0]})
    monkeypatch.setattr(EF.T, "has_token", lambda: True)
    def fake(api, fields="", **kw):
        return df if kw.get("ts_code") == "510050.SH" else pd.DataFrame()
    monkeypatch.setattr(EF.T, "call", fake)
    n = EF.refresh(env)
    assert n == 2
    con = sqlite3.connect(str(env))
    n2 = con.execute("SELECT COUNT(*) FROM etf_share").fetchone()[0]
    con.close()
    assert n2 == 2


# ── 历史回补(fund_share全史可回补, 2018实测247行/年) ──

def _mk_fake(calls, fail_chunks=()):
    import pandas as pd

    def fake(api, fields="", **kw):
        calls.append((kw.get("ts_code"), kw.get("start_date"), kw.get("end_date")))
        if (kw.get("ts_code"), kw.get("start_date")) in fail_chunks:
            raise RuntimeError("每分钟最多访问该接口20次")
        return pd.DataFrame({"ts_code": [kw["ts_code"]],
                             "trade_date": [kw["start_date"]],
                             "fd_share": [100.0]})
    return fake


def test_backfill_chunks_pace_idempotent(env, monkeypatch):
    """按年分块×8ETF逐块拉取; 块间控频sleep; upsert幂等(二跑不重复)"""
    import datetime as dt
    import fetch_etf_flow as EF
    calls, sleeps = [], []
    monkeypatch.setattr(EF.T, "has_token", lambda: True)
    monkeypatch.setattr(EF.T, "call", _mk_fake(calls))
    monkeypatch.setattr(EF.time, "sleep", lambda s: sleeps.append(s))
    n = EF.backfill("20240101", env, pace_s=6)
    today = dt.date.today()                     # 期望年块动态构造(与实现同构)
    chunks, lo = [], dt.date(2024, 1, 1)
    while lo <= today:
        hi = min(dt.date(lo.year, 12, 31), today)
        chunks.append((lo.strftime("%Y%m%d"), hi.strftime("%Y%m%d")))
        lo = dt.date(lo.year + 1, 1, 1)
    assert n == 8 * len(chunks)
    assert calls == [(c, s, e) for c in EF.ETF_CODES for s, e in chunks]
    assert sleeps == [6] * (8 * len(chunks))          # 每块后控频
    con = sqlite3.connect(str(env))
    assert con.execute("SELECT COUNT(*) FROM etf_share").fetchone()[0] == n
    con.close()
    n2 = EF.backfill("20240101", env, pace_s=6)       # 幂等: upsert不重复
    con = sqlite3.connect(str(env))
    assert con.execute("SELECT COUNT(*) FROM etf_share").fetchone()[0] == n
    con.close()
    assert n2 == n


def test_backfill_rate_limit_tolerant(env, monkeypatch):
    """限频块失败跳过不中断, 其余块照常入库"""
    import fetch_etf_flow as EF
    fails = {(c, "20240101") for c in EF.ETF_CODES}   # 每只ETF的2024块都限频
    calls = []
    monkeypatch.setattr(EF.T, "has_token", lambda: True)
    monkeypatch.setattr(EF.T, "call", _mk_fake(calls, fails))
    monkeypatch.setattr(EF.time, "sleep", lambda s: None)
    n = EF.backfill("20240101", env, pace_s=0)
    assert n > 0                                       # 其余块正常
    assert len(calls) == n + len(fails)                # 失败块也消耗了调用


def test_backfill_registered_and_window():
    """注册到统一回补入口(BACKFILLS, 默认2006起) + viewer全史窗口共享轴联动"""
    src = (CORE / "fetch_daily.py").read_text(encoding="utf-8")
    assert '"etf_flow"' in src and 'fetch_etf_flow").backfill(start)' in src
    assert '"etf_share", None, "trade_date"' in src and '"20060101"' in src
    vsrc = (CORE / "viewer.py").read_text(encoding="utf-8")
    assert "net_flow_series(n_days=6000)" in vsrc        # 全史窗口
    assert "barWidth: 2" in vsrc and "D.etfFlowMap[D.dates[i]]" in vsrc  # 共享轴
    assert '"date": x["date"], "net_yi": x["net_yi"]' in vsrc  # payload剥离flows瘦身
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "ds: 'etf_flow'" in html                       # 修复页可见
