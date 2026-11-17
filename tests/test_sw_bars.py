# -*- coding: utf-8 -*-
"""sw_bars: SW指数日线 — 增量分页刷新/幂等豁免与probe跳过/连续异常熔断"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import sw_bars as SB  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE index_daily(code TEXT, date TEXT, close REAL)")
    con.execute("INSERT INTO index_daily VALUES('1.000001','2026-11-16',3600)")
    con.execute("CREATE TABLE sw_components(index_code TEXT, level TEXT, "
                "industry_name TEXT, con_code TEXT, in_date TEXT, out_date TEXT)")
    con.executemany("INSERT INTO sw_components VALUES(?,?,NULL,NULL,NULL,NULL)",
                    [("801010.SI", "L1"), ("801020.SI", "L1")])
    con.execute("CREATE TABLE sw_daily(index_code TEXT, date TEXT, open REAL, "
                "high REAL, low REAL, close REAL, volume REAL, amount REAL, "
                "pct_chg REAL, PRIMARY KEY(index_code, date))")
    con.execute("CREATE TABLE sw_daily_probe(index_code TEXT PRIMARY KEY, "
                "checked_at TEXT)")
    con.commit(); con.close()
    monkeypatch.setattr(SB, "connect", lambda: sqlite3.connect(str(db)))
    monkeypatch.setattr(SB.T, "has_token", lambda: True)
    return db


def _seed_sw_daily(db, ic, last):
    con = sqlite3.connect(str(db))
    con.execute("INSERT OR REPLACE INTO sw_daily VALUES "
                "(?,?,1,2,0.5,1.5,100,200,1.0)", (ic, last))
    con.commit(); con.close()


def test_refresh_pagination_upsert(env, monkeypatch):
    """分页: 恰满4000行继续翻页; 空页停; upsert入库ISO日期"""
    pages = [
        pd.DataFrame([{"ts_code": "801010.SI", "trade_date": "20261116",
                       "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                       "vol": 100, "amount": 200, "pct_change": 1.0}] * 4000),
        pd.DataFrame([{"ts_code": "801020.SI", "trade_date": "20261116",
                       "open": 2.0, "high": 3.0, "low": 1.5, "close": 2.5,
                       "vol": 300, "amount": 400, "pct_change": 2.0}]),
    ]
    calls = []
    monkeypatch.setattr(SB.T, "call",
                        lambda api, fields="", **kw: calls.append(kw)
                        or pages.pop(0))
    assert SB.refresh(days=12) is True
    assert len(calls) == 2 and calls[1]["offset"] == 4000     # 第二页offset
    con = sqlite3.connect(str(env))
    r = con.execute("SELECT date, close FROM sw_daily WHERE index_code='801020.SI'"
                    ).fetchall()
    con.close()
    assert r == [("2026-11-16", 2.5)]                          # YMD→ISO入库


def test_refresh_fail_returns_false(env, monkeypatch):
    monkeypatch.setattr(SB.T, "call",
                        lambda api, fields="", **kw: (_ for _ in ()).throw(
                            RuntimeError("每分钟最多访问该接口20次")))
    assert SB.refresh() is False


def test_backfill_idempotent_skip_and_probe(env, monkeypatch):
    """最新≥行情-7日 → 幂等跳过; probe过的无数据指数不再重试"""
    _seed_sw_daily(env, "801010.SI", "2026-11-13")            # ≥cutoff(11-09)
    con = sqlite3.connect(str(env))
    con.execute("INSERT INTO sw_daily_probe VALUES('801020.SI','2026-10-01')")
    con.commit(); con.close()
    called = []
    monkeypatch.setattr(SB.T, "call", lambda api, fields="", **kw:
                        called.append(kw.get("ts_code")) or pd.DataFrame())
    assert SB.backfill() is True
    assert called == []                                        # 两个都跳过


def test_backfill_fetches_stale_and_probes_empty(env, monkeypatch):
    """过期指数拉取; 返回空 → 登记probe不再算待补"""
    _seed_sw_daily(env, "801010.SI", "2026-01-01")            # 过期
    res = {}

    def fake(api, fields="", **kw):
        ic = kw.get("ts_code")
        if ic == "801010.SI":
            return pd.DataFrame([{"trade_date": "20261116", "open": 1.0,
                                  "high": 2.0, "low": 0.5, "close": 1.5,
                                  "vol": 1, "amount": 2, "pct_change": 1.0}])
        res[ic] = True
        return pd.DataFrame()                                  # 801020无官方数据
    monkeypatch.setattr(SB.T, "call", fake)
    assert SB.backfill() is True
    con = sqlite3.connect(str(env))
    assert con.execute("SELECT COUNT(*) FROM sw_daily_probe WHERE "
                       "index_code='801020.SI'").fetchone()[0] == 1
    n = con.execute("SELECT COUNT(*) FROM sw_daily WHERE index_code='801010.SI'"
                    ).fetchone()[0]
    con.close()
    assert n == 2                                              # 旧+新行共存
    # 二跑: 801010已新, 801020已probe → 零调用
    calls2 = []
    monkeypatch.setattr(SB.T, "call", lambda api, fields="", **kw:
                        calls2.append(1) or pd.DataFrame())
    SB.backfill()
    assert calls2 == []


def test_backfill_err_streak_circuit_breaker(env, monkeypatch):
    """连续5次异常熔断返回False; 间隔sleep被调用"""
    sleeps = []
    monkeypatch.setattr(SB.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(SB.T, "call", lambda api, fields="", **kw:
                        (_ for _ in ()).throw(RuntimeError("网络挂了")))
    # 两指数都无数据 → 都进todo; 需要更多指数凑连续5次 → 注入5个
    con = sqlite3.connect(str(env))
    for i in range(5):
        con.execute("INSERT INTO sw_components VALUES(?,?,NULL,NULL,NULL,NULL)",
                    (f"8010{i}.SI", "L1"))
    con.commit(); con.close()
    assert SB.backfill() is False
    assert len(sleeps) == 4                                    # 第5次不再sleep
