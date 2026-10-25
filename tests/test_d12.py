# -*- coding: utf-8 -*-
"""D1-12: tushare dc系列板块数据 — 半截日判定/守卫写入/启用过滤/注册接线"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))


@pytest.fixture()
def dc_env(tmp_path, monkeypatch):
    """迷你库: 3个启用行业板块 + sector_daily(10-16健康/10-19半截/10-20缺) + 日历"""
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE sectors(code TEXT PRIMARY KEY, name TEXT, kind TEXT, enabled INTEGER,
        first_date TEXT, last_date TEXT, updated_at TEXT);
      CREATE TABLE sector_daily(code TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, amount REAL, up_count INTEGER, down_count INTEGER,
        PRIMARY KEY(code, date));
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER);
      CREATE TABLE sector_fundflow(code TEXT, date TEXT, main_net REAL,
        PRIMARY KEY(code, date));
    """)
    for i in range(3):
        conn.execute("INSERT INTO sectors VALUES(?,?,'industry',1,NULL,NULL,NULL)",
                     (f"BK{i:04d}", f"板块{i}"))
        conn.execute("INSERT INTO sector_daily(code,date,close,up_count) "
                     "VALUES(?, '2026-10-16', 100, 10)", (f"BK{i:04d}",))
    conn.execute("INSERT INTO sector_daily(code,date,close) "
                 "VALUES('BK0000','2026-10-19',99)")        # 半截: 仅1/3板块
    for d in ("2026-10-16", "2026-10-19", "2026-10-20"):
        conn.execute("INSERT INTO calendar VALUES(?,1)", (d.replace("-", ""),))
        conn.execute("INSERT INTO index_daily VALUES('1.000001', ?, 3000)", (d,))
    conn.commit()
    conn.close()
    import fetch_dc as FD
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(p)))
    return {"path": p, "FD": FD}


def _dc_df():
    return pd.DataFrame([
        ("BK0000.DC", 1, 2, 0.5, 1.5, 100.0),
        ("BK0001.DC", 1, 2, 0.5, 1.5, 100.0),
        ("BK0002.DC", 1, 2, 0.5, 1.5, 100.0),
        ("BK9999.DC", 9, 9, 9, 9, 9.0),           # 未启用/概念 → 应过滤
    ], columns=["ts_code", "open", "high", "low", "close", "amount"])


def _ix_df():
    return pd.DataFrame([
        ("BK0000.DC", 10, 5), ("BK0001.DC", 20, 3),
        ("BK0002.DC", 30, 1), ("BK9999.DC", 99, 99),
    ], columns=["ts_code", "up_num", "down_num"])


def test_dc_run_fills_thin_and_missing(dc_env, monkeypatch):
    """只补半截(10-19)与缺失(10-20)交易日; 健康日(10-16)不动; 未启用板块过滤"""
    FD = dc_env["FD"]
    calls = []

    def fake(api, fields="", **kw):
        calls.append((api, kw.get("trade_date")))
        return _dc_df() if api == "dc_daily" else _ix_df()
    monkeypatch.setattr(FD.T, "has_token", lambda: True)
    monkeypatch.setattr(FD.T, "call", fake)
    assert FD.run("20261016") is True
    touched = sorted({d for _, d in calls})
    assert touched == ["20261019", "20261020"]      # 10-16健康日跳过
    conn = sqlite3.connect(str(dc_env["path"]))
    row = conn.execute("SELECT close, up_count FROM sector_daily "
                       "WHERE code='BK0000' AND date='2026-10-20'").fetchone()
    assert row == (1.5, 10)                          # OHLC+涨跌家数齐写
    n19 = conn.execute("SELECT COUNT(*) FROM sector_daily "
                       "WHERE date='2026-10-19'").fetchone()[0]
    assert n19 == 3                                   # 半截日补齐到3板块
    assert conn.execute("SELECT COUNT(*) FROM sector_daily "
                        "WHERE code='BK9999'").fetchone()[0] == 0
    conn.close()


def test_dc_run_guard_rejects_dup_batch(dc_env, monkeypatch):
    """tushare批次主键重复 → 守卫拒绝, 该日失败, 表零污染"""
    FD = dc_env["FD"]
    dup = pd.DataFrame([
        ("BK0000.DC", 1, 2, 0.5, 1.5, 100.0),
        ("BK0000.DC", 1, 2, 0.5, 1.6, 100.0),
    ], columns=["ts_code", "open", "high", "low", "close", "amount"])
    monkeypatch.setattr(FD.T, "has_token", lambda: True)
    monkeypatch.setattr(FD.T, "call",
                        lambda api, fields="", **kw: dup if api == "dc_daily" else _ix_df())
    assert FD.run("20261016") is False
    conn = sqlite3.connect(str(dc_env["path"]))
    assert conn.execute("SELECT COUNT(*) FROM sector_daily "
                        "WHERE date='2026-10-20'").fetchone()[0] == 0
    conn.close()


def test_dc_daily_registered_everywhere():
    """注册接线: BACKFILLS + jobs标签/默认起始 + 资产表行"""
    import fetch_daily as FD2
    assert "dc_daily" in FD2.BACKFILLS and "dc_fflow" in FD2.BACKFILLS
    assert FD2.BACKFILLS["dc_fflow"][3] == "20230901"
    from server import jobs
    assert "dc_daily" in jobs.BACKFILL_DATASETS and "dc_fflow" in jobs.BACKFILL_DATASETS
    assert jobs.BACKFILL_DEFAULT_START["dc_fflow"] == "20230901"
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "dc_daily" in html and "tushare近端" in html
    assert "dc_fflow" in html and "自2023-09起" in html


# ── D1-12 G2: 资金流 dc 迁移 ──

def _mf_df():
    return pd.DataFrame([
        ("BK0000.DC", 1.5e8), ("BK0001.DC", -2.5e8),
        ("BK0002.DC", 3.0e8), ("BK9999.DC", 9.9e9),
    ], columns=["ts_code", "net_amount"])


def test_refresh_fflow_dc_fills_thin_days(dc_env, monkeypatch):
    """G2: 日更主源 — thin日(行数<90%启用板块)按trade_date补, 健康日跳过, 未启用过滤"""
    FD = dc_env["FD"]
    c = sqlite3.connect(str(dc_env["path"]))
    for i in range(3):                                 # 10-16 健康(3/3)
        c.execute("INSERT INTO sector_fundflow VALUES(?, '2026-10-16', 1.0)",
                  (f"BK{i:04d}",))
    c.commit()
    c.close()
    calls = []
    monkeypatch.setattr(FD.T, "has_token", lambda: True)
    monkeypatch.setattr(FD.T, "call", lambda api, fields="", **kw:
                        calls.append(kw.get("trade_date")) or _mf_df())
    assert FD.refresh_fflow_dc(conn=sqlite3.connect(str(dc_env["path"]))) is True
    assert calls == ["20261019", "20261020"]           # 只补thin日(10-19/20)
    c = sqlite3.connect(str(dc_env["path"]))
    row = c.execute("SELECT main_net FROM sector_fundflow "
                    "WHERE code='BK0001' AND date='2026-10-20'").fetchone()
    assert row == (-2.5e8,)
    assert c.execute("SELECT COUNT(*) FROM sector_fundflow "
                     "WHERE code='BK9999'").fetchone()[0] == 0
    c.close()


def test_backfill_fflow_dc_per_code(dc_env, monkeypatch):
    """G2: 历史回补 — 按代码全史, 本地过滤start前, guarded_batch写入"""
    FD = dc_env["FD"]
    hist = pd.DataFrame([
        ("20230501", 1.0),      # 早于默认start → 滤除
        ("20260101", 2.0), ("20261019", 3.0),
    ], columns=["trade_date", "net_amount"])
    monkeypatch.setattr(FD.T, "has_token", lambda: True)
    monkeypatch.setattr(FD.T, "call", lambda api, fields="", **kw: hist)
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(dc_env["path"])))
    assert FD.backfill_fflow_dc("20230901") is True
    c = sqlite3.connect(str(dc_env["path"]))
    assert c.execute("SELECT COUNT(*) FROM sector_fundflow").fetchone()[0] == 6  # 3板块×2日
    assert c.execute("SELECT COUNT(*) FROM sector_fundflow "
                     "WHERE date='2023-05-01'").fetchone()[0] == 0
    c.close()


def test_refresh_fundflow_routes_dc(dc_env, monkeypatch):
    """G2: refresh_fundflow 走 dc 主源不触东财; dc失败回退; 5天跳过仍在"""
    import sectors as S
    import fetch_dc as FD
    conn = sqlite3.connect(str(dc_env["path"]))
    calls = {"dc": [], "em": []}
    monkeypatch.setattr(FD, "refresh_fflow_dc",
                        lambda c, win_days=10: calls["dc"].append(1) or True)
    monkeypatch.setattr(S.em_api, "sector_fundflow",
                        lambda code: calls["em"].append(1) or pd.DataFrame())
    S.refresh_fundflow(conn, force=True)
    assert calls == {"dc": [1], "em": []}              # dc成功, 东财未触
    monkeypatch.setattr(FD, "refresh_fflow_dc",
                        lambda c, win_days=10: calls["dc"].append(1) or False)
    S.refresh_fundflow(conn, force=True)
    assert calls["em"]                                  # dc失败回退东财
    conn.execute("INSERT INTO sector_fundflow VALUES('BK0000', '2026-10-24', 1.0)")
    conn.commit()
    S.refresh_fundflow(conn, force=False)               # 近5天内已新 → 全跳过
    assert calls["dc"] == [1, 1]
    conn.close()


def test_dc_run_skips_empty_days(dc_env, monkeypatch):
    """G1: dc对某日无数据(未出数/超窗口) → 跳过不计失败(误判会触发东财回退)"""
    FD = dc_env["FD"]
    empty = pd.DataFrame(columns=["ts_code", "open", "high", "low", "close", "amount"])
    monkeypatch.setattr(FD.T, "has_token", lambda: True)
    monkeypatch.setattr(FD.T, "call", lambda api, fields="", **kw: empty)
    assert FD.run("20261016") is True              # 跳过≠失败
    conn = sqlite3.connect(str(dc_env["path"]))
    assert conn.execute("SELECT COUNT(*) FROM sector_daily "
                        "WHERE date='2026-10-20'").fetchone()[0] == 0
    conn.close()


def test_update_via_dc_no_token_falls_back(dc_env, monkeypatch):
    """D1-12r: 无token时dc主源必须返回False — 返回True会让update_daily
    以为主源成功而彻底跳过板块日线(fetch_dc.run的独立无token跳过语义不适用主源)"""
    import sectors as S
    import tushare_api as TA
    monkeypatch.setattr(TA, "has_token", lambda: False)
    assert S._update_via_dc() is False


def test_update_daily_dc_primary_routing(dc_env, monkeypatch):
    """G1: update_daily 走 dc 主源时不触东财路径; dc失败回退kline+快照;
    资金流两路径都执行"""
    import sectors as S
    calls = {"em": [], "snap": [], "ff": []}
    monkeypatch.setattr(S, "connect", lambda: sqlite3.connect(str(dc_env["path"])))
    monkeypatch.setattr(S, "refresh_lists", lambda c: {})
    monkeypatch.setattr(S, "_kline_incremental_em",
                        lambda *a: calls["em"].append(1))
    monkeypatch.setattr(S, "_snapshot_updown",
                        lambda *a: calls["snap"].append(1))
    monkeypatch.setattr(S, "refresh_fundflow",
                        lambda c, force=False: calls["ff"].append(1))
    monkeypatch.setattr(S, "remap_pending", lambda c: None)
    monkeypatch.setattr(S, "agg_today", lambda c: None)
    monkeypatch.setattr(S, "_unmatched", [])

    monkeypatch.setattr(S, "_update_via_dc", lambda **k: True)
    S.update_daily()
    assert not calls["em"] and not calls["snap"]      # dc主源: 东财路径未触发
    assert calls["ff"] == [1]                         # 资金流独立执行

    monkeypatch.setattr(S, "_update_via_dc", lambda **k: False)
    S.update_daily()
    assert calls["em"] == [1] and calls["snap"] == [1]   # 回退: kline+快照都跑
    assert calls["ff"] == [1, 1]
