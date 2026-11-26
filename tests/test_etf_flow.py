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

def _mk_fake(calls, fail_chunks=(), rows_for=None):
    """fund_share替身: 每块返回 rows_for(start_date) 行(默认1行, trade_date=块起始日)"""
    import pandas as pd

    def fake(api, fields="", **kw):
        calls.append((kw.get("ts_code"), kw.get("start_date"), kw.get("end_date")))
        if (kw.get("ts_code"), kw.get("start_date")) in fail_chunks:
            raise RuntimeError("每分钟最多访问该接口20次")
        n = rows_for(kw["start_date"]) if rows_for else 1
        dates = ([kw["start_date"]] if n == 1 else
                 [f"{kw['start_date'][:4]}{i:04d}" for i in range(1, n + 1)])
        return pd.DataFrame({"ts_code": [kw["ts_code"]] * n,
                             "trade_date": dates, "fd_share": [100.0] * n})
    return fake


def _year_chunks():
    import datetime as dt
    today = dt.date.today()                     # 期望年块动态构造(与实现同构)
    chunks, lo = [], dt.date(2024, 1, 1)
    while lo <= today:
        hi = min(dt.date(lo.year, 12, 31), today)
        chunks.append((lo.strftime("%Y%m%d"), hi.strftime("%Y%m%d")))
        lo = dt.date(lo.year + 1, 1, 1)
    return today, chunks


def test_backfill_chunks_pace_idempotent(env, monkeypatch):
    """按年分块×抓取清单(国家队8只+深成指ETF)逐块拉取; 块间控频sleep; upsert幂等(二跑不重复)"""
    import fetch_etf_flow as EF
    calls, sleeps = [], []
    monkeypatch.setattr(EF.T, "has_token", lambda: True)
    monkeypatch.setattr(EF.T, "call", _mk_fake(calls))
    monkeypatch.setattr(EF.time, "sleep", lambda s: sleeps.append(s))
    n = EF.backfill("20240101", env, pace_s=6)
    today, chunks = _year_chunks()
    n_etf = len(EF.ETF_ALL)
    assert n == n_etf * len(chunks)
    assert calls == [(c, s, e) for c in EF.ETF_ALL for s, e in chunks]
    assert sleeps == [6] * (n_etf * len(chunks))        # 每块后控频
    con = sqlite3.connect(str(env))
    assert con.execute("SELECT COUNT(*) FROM etf_share").fetchone()[0] == n
    con.close()
    n2 = EF.backfill("20240101", env, pace_s=6)
    con = sqlite3.connect(str(env))
    assert con.execute("SELECT COUNT(*) FROM etf_share").fetchone()[0] == n
    con.close()
    # 夹具各年块仅1行: 参照年不足完整年份下限 → 无可信基准 → 全部照常重拉
    # (完整年份的跳过行为见 test_backfill_skips_complete_years)
    assert n2 == n


def test_backfill_skips_complete_years(env, monkeypatch):
    """D1-33: 重跑时已完整的历史年块不再调用接口 — 参照ETF各历史年243行(≥下限),
    其余7只行数与之相等; 当年块每次都重拉(取新数据)"""
    import fetch_etf_flow as EF
    today, chunks = _year_chunks()
    this_year = str(today.year)
    n_past = len([c for c in chunks if c[0][:4] < this_year])
    calls, sleeps = [], []
    monkeypatch.setattr(EF.T, "has_token", lambda: True)
    monkeypatch.setattr(EF.T, "call", _mk_fake(
        calls, rows_for=lambda s: 243 if s[:4] < this_year else 1))
    monkeypatch.setattr(EF.time, "sleep", lambda s: sleeps.append(s))
    EF.backfill("20240101", env, pace_s=6)
    n_etf = len(EF.ETF_ALL)
    assert len(calls) == n_etf * len(chunks)           # 首跑: 全部年块
    calls.clear()
    sleeps.clear()
    n2 = EF.backfill("20240101", env, pace_s=6)
    assert n2 == n_etf and len(calls) == n_etf and len(sleeps) == n_etf   # 仅当年块(每只×1块)
    assert {c for c, _s, _e in calls} == set(EF.ETF_ALL)
    assert all(s[:4] == this_year for _c, s, _e in calls)
    con = sqlite3.connect(str(env))
    assert con.execute("SELECT COUNT(*) FROM etf_share").fetchone()[0] == n_etf * (n_past * 243 + 1)
    con.close()


def test_net_flow_series_ignores_extra_etf_rows(env):
    """MI-1: 份额表里多出国家队口径之外的ETF(深成指ETF)时, 国家队净申购序列不变 —
    原按 LIMIT 天数×8 取行, 每天多一行就会把窗口挤短、序列起点上移"""
    import fetch_etf_flow as EF
    assert len(EF.ETF_CODES) == 8 and EF.ETF_EXTRA_CODES == ("159903.SZ",)
    con = EF.ensure_db(env)
    con.execute("CREATE TABLE IF NOT EXISTS etf_daily(code TEXT, date TEXT, close REAL)")
    days = [f"202610{d:02d}" for d in range(20, 31)]          # 10 个"交易日"
    for i, d8 in enumerate(days):
        iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        for c in EF.ETF_CODES:
            con.execute("INSERT INTO etf_share VALUES(?,?,?)", (c, d8, 10000 + 100 * i))
        for c, p in (("510050.SH", 3.0), ("510300.SH", 4.0)):
            con.execute("INSERT INTO etf_daily(code,date,close) VALUES(?,?,?)", (c, iso, p))
    con.commit()
    before = EF.net_flow_series(env, n_days=9)
    assert len(before) == 8                                 # 窗口首日只作基准
    for i, d8 in enumerate(days):                           # 深成指ETF的份额行
        con.execute("INSERT INTO etf_share VALUES(?,?,?)", ("159903.SZ", d8, 500 + i))
    con.commit()
    con.close()
    after = EF.net_flow_series(env, n_days=9)
    assert len(after) == 8 and after == before              # 窗口与数值都不变


def test_backfill_incomplete_year_not_skipped(env):
    """D1-33: 行数与参照不一致的年份(上市晚/源端缺日)不算完整, 重跑照常重拉;
    参照自身不足完整年份下限的年份不作为基准; 当年及以后不判"""
    import fetch_etf_flow as EF
    con = EF.ensure_db(env)

    def yr(code, year, n):
        con.executemany("INSERT INTO etf_share VALUES(?,?,1)",
                        [(code, f"{year}{i:04d}") for i in range(1, n + 1)])
    yr(EF._REF_CODE, "2024", 243)
    yr("510300.SH", "2024", 243)                    # 与参照相等
    yr("159915.SZ", "2024", 244)                    # 多1行 ≠ 参照
    yr("510500.SH", "2024", 208)                    # 源端缺日
    yr(EF._REF_CODE, "2023", 12)                    # 参照该年只有12行
    yr("510300.SH", "2023", 12)                     # 与参照相等, 但基准不可信
    con.commit()
    con.close()
    c = sqlite3.connect(str(env))
    done = EF._complete_years(c, "2099")
    assert (EF._REF_CODE, "2024") in done and ("510300.SH", "2024") in done
    assert ("159915.SZ", "2024") not in done and ("510500.SH", "2024") not in done
    assert ("510300.SH", "2023") not in done and (EF._REF_CODE, "2023") not in done
    assert EF._complete_years(c, "2024") == set()   # 当年及以后不判
    c.close()


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


def test_backfill_partial_reference_year_not_complete(env):
    """review(1913742): 参照ETF某年自身只有部分数据(如先跑过半年的回补)时,
    其他ETF同年行数"与参照相等"不代表完整 — 否则之后再跑全年回补会把该年
    整块跳过, 上半年永远缺失。参照年行数达到完整年份下限才可作比较基准。"""
    import fetch_etf_flow as EF
    con = EF.ensure_db(env)
    for code in EF.ETF_CODES:
        # 2025: 每只都只有下半年约百日(半年回补后的状态)
        con.executemany("INSERT INTO etf_share VALUES(?,?,1)",
                        [(code, f"2025{m:02d}{d:02d}") for m in range(7, 11) for d in range(1, 29)])
        # 2024: 参照与510300整年243行; 159915缺3行
        n = 243 if code in (EF._REF_CODE, "510300.SH") else 240
        con.executemany("INSERT INTO etf_share VALUES(?,?,1)",
                        [(code, f"2024{m:02d}{d:02d}") for m in range(1, 13) for d in range(1, 29)
                         if d <= 24][:n])
    con.commit()
    con.close()
    done = EF._complete_years(sqlite3.connect(str(env)), "2026")
    assert not any(y == "2025" for _c, y in done)                 # 参照2025仅112行 → 该年无可信基准
    assert (EF._REF_CODE, "2024") in done and ("510300.SH", "2024") in done
    assert ("159915.SZ", "2024") not in done
