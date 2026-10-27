# -*- coding: utf-8 -*-
"""D1-15 I1: 全A/宽基估值聚合 — 整体法/成分快照/守卫/注册接线"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import fetch_mktval as MV  # noqa: E402


@pytest.fixture()
def mv_env(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE index_val_daily(code TEXT, date TEXT, pe REAL, pb REAL,
        name TEXT, PRIMARY KEY(code, date));
    """)
    for d in ("2026-10-22", "2026-10-23"):
        c.execute("INSERT INTO calendar VALUES(?,1)", (d.replace("-", ""),))
        c.execute("INSERT INTO index_daily VALUES('1.000001',?,3000)", (d,))
    c.commit()
    c.close()
    monkeypatch.setattr(MV, "connect", lambda: sqlite3.connect(str(p)))
    return {"path": p, "MV": MV}


def _basic_df():
    # 全A: A/B/C 有效(mv 300/200/100, pe 10/20/40, pb 1/2/4), D 亏损(pe<0), E 无pb
    return pd.DataFrame([
        ("000001.SZ", 10.0, 1.0, 300.0),
        ("000002.SZ", 20.0, 2.0, 200.0),
        ("000003.SZ", 40.0, 4.0, 100.0),
        ("000004.SZ", -5.0, 0.5, 50.0),     # 亏损剔除
        ("000005.SZ", 15.0, None, 60.0),    # pb缺失剔除
    ], columns=["ts_code", "pe_ttm", "pb", "total_mv"])


def test_agg_alla_and_members(mv_env):
    """全A整体法(Σmv/Σ(mv/pe))与成分内聚合的手工验算"""
    rows = [("000001.SZ", 10.0, 1.0, 300.0), ("000002.SZ", 20.0, 2.0, 200.0),
            ("000003.SZ", 40.0, 4.0, 100.0), ("000004.SZ", -5.0, 0.5, 50.0),
            ("000005.SZ", 15.0, None, 60.0)]
    # 全A: Σmv=600, Σ(mv/pe)=30+10+2.5=42.5 → PE=14.1176; Σ(mv/pb)=300+100+25=425 → PB=1.4118
    pe, pb = MV._agg(rows, min_n=1)
    assert pe == pytest.approx(600 / 42.5, abs=1e-3)
    assert pb == pytest.approx(600 / 425, abs=1e-3)
    # 成分={000001,000002}: Σmv=500, Σ(mv/pe)=40 → PE=12.5; Σ(mv/pb)=350 → PB≈1.4286
    pe2, pb2 = MV._agg(rows, {"000001.SZ", "000002.SZ"}, min_n=1)
    assert pe2 == pytest.approx(12.5, abs=1e-3)
    assert pb2 == pytest.approx(1.25, abs=1e-3)


def test_members_on_latest_snapshot(mv_env):
    """成分解析: 取≤目标日的最近快照"""
    c = sqlite3.connect(str(mv_env["path"]))
    MV._ensure_tables(c)
    c.executemany("INSERT INTO mkt_index_members VALUES(?,?,?,?)", [
        ("000300.SH", "20261015", "000001.SZ", 1.0),
        ("000300.SH", "20261015", "000002.SZ", 1.0),
        ("000300.SH", "20261020", "000001.SZ", 1.0),   # 更近快照只含1只
    ])
    c.commit()
    c.close()
    m = MV._members_on(sqlite3.connect(str(mv_env["path"])), "000300.SH", "20261022")
    assert m == {"000001.SZ"}                          # 10-20快照胜出
    assert MV._members_on(sqlite3.connect(str(mv_env["path"])), "000300.SH", "20260101") is None


def test_fetch_one_day_writes(mv_env, monkeypatch):
    """单日聚合落库: 全A+四宽基一齐写入; 亏损股剔除"""
    calls = []

    def fake(api, fields="", **kw):
        calls.append((api, kw.get("trade_date")))
        if api == "daily_basic":
            return _basic_df()
        return pd.DataFrame(columns=["ts_code"])
    monkeypatch.setattr(MV.T, "has_token", lambda: True)
    monkeypatch.setattr(MV.T, "call", fake)
    c = sqlite3.connect(str(mv_env["path"]))
    MV._ensure_tables(c)
    c.executemany("INSERT INTO mkt_index_members VALUES(?,?,?,?)", [
        (ix, "20261020", "000001.SZ", 1.0) for ix in MV.INDEXES])
    c.commit()
    assert MV._fetch_one_day(c, "2026-10-22", min_n=1) is True
    row = c.execute("SELECT alla_pe, alla_pb, pe300, pe1000 FROM mkt_val_daily").fetchone()
    assert row[0] == pytest.approx(600 / 42.5, abs=1e-3)
    assert row[2] == pytest.approx(10.0, abs=1e-3)     # 成分仅000001 → PE=其自身
    assert row[3] == pytest.approx(10.0, abs=1e-3)
    c.close()


def test_mkt_val_registered():
    import fetch_daily as FD2
    assert "mkt_val" in FD2.BACKFILLS
    from server import jobs
    assert "mkt_val" in jobs.BACKFILL_DATASETS
    assert jobs.BACKFILL_DEFAULT_START["mkt_val"] == "20100101"
    from server import market
    assert any(s[0] == "mkt_val" for s in market._ASSET_DATE_TABLES)
    src = Path(CORE / "fetch_daily.py").read_text(encoding="utf-8")
    assert "fetch_mktval.refresh()" in src
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "mkt_val" in html and "自2010-01起" in html
