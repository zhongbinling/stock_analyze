# -*- coding: utf-8 -*-
"""D1-18 M4: 巴菲特指标 — mv_total列迁移/累计口径/值域豁免/NULL补拉/比值分位/页面接线"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(REPO))

import fetch_mktval as MV  # noqa: E402


@pytest.fixture()
def mv_env(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
    """)
    for d in ("2026-10-22", "2026-10-23"):
        c.execute("INSERT INTO calendar VALUES(?,1)", (d.replace("-", ""),))
        c.execute("INSERT INTO index_daily VALUES('1.000001',?,3000)", (d,))
    # 旧schema(无mv_total) → 测增列迁移
    c.execute("""CREATE TABLE mkt_val_daily(
      date TEXT PRIMARY KEY, alla_pe REAL, alla_pb REAL,
      pe50 REAL, pb50 REAL, pe300 REAL, pb300 REAL,
      pe500 REAL, pb500 REAL, pe1000 REAL, pb1000 REAL)""")
    c.commit()
    c.close()
    monkeypatch.setattr(MV, "connect", lambda: sqlite3.connect(str(p)))
    return {"path": p, "MV": MV}


def _basic_df():
    return pd.DataFrame([
        ("000001.SZ", 10.0, 1.0, 300.0),
        ("000002.SZ", 20.0, 2.0, 200.0),
        ("000004.SZ", -5.0, 0.5, 50.0),      # 亏损: pe剔除但市值计入
    ], columns=["ts_code", "pe_ttm", "pb", "total_mv"])


def test_ensure_tables_migrates_mv_column(mv_env):
    conn = sqlite3.connect(str(mv_env["path"]))
    MV._ensure_tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mkt_val_daily)")}
    conn.close()
    assert "mv_total" in cols
    conn2 = sqlite3.connect(str(mv_env["path"]))          # 幂等
    MV._ensure_tables(conn2)
    conn2.close()


def test_fetch_one_day_writes_mv_total(mv_env, monkeypatch):
    """mv_total=Σ有效total_mv/1e4(万元→亿), 与pe/pb有效性解耦(亏损股计入)"""
    monkeypatch.setattr(MV.T, "call", lambda *a, **k: _basic_df())
    conn = sqlite3.connect(str(mv_env["path"]))
    MV._ensure_tables(conn)
    assert MV._fetch_one_day(conn, "2026-10-22", verbose=False, min_n=1) is True
    row = conn.execute("SELECT alla_pe, mv_total FROM mkt_val_daily "
                       "WHERE date='2026-10-22'").fetchone()
    conn.close()
    assert row[1] == round((300 + 200 + 50) / 1e4, 1)   # 0.055亿(含亏损股50)
    assert row[0] is not None                            # pe仅由有效3行…此df仅2行有效


def test_val_check_allows_large_mv(mv_env):
    """M4: mv_total(亿元级)豁免<1000值域; pe>1000仍拒"""
    chk = MV._val_check("2026-10-22")
    ok_row = ("2026-10-22", 15.0, 1.5, None, None, None, None, None, None,
              None, None, 900000.0)                      # 90万亿=9e5亿
    assert chk([ok_row]) is None
    bad_pe = ("2026-10-22", 1500.0, 1.5, None, None, None, None, None,
              None, None, None, 900000.0)
    assert chk([bad_pe]) is not None


def test_run_todo_includes_mv_null_days(mv_env, monkeypatch):
    """旧史已有估值但 mv_total NULL → 重拉补列; 补齐后 todo 空"""
    conn = sqlite3.connect(str(mv_env["path"]))
    MV._ensure_tables(conn)
    conn.execute("INSERT INTO mkt_val_daily(date,alla_pe) VALUES('2026-10-22',15.0)")
    conn.commit()
    conn.close()
    calls = []
    monkeypatch.setattr(MV.T, "has_token", lambda: True)
    monkeypatch.setattr(MV, "backfill_members", lambda conn=None: None)
    monkeypatch.setattr(MV.T, "call", lambda *a, **k: (calls.append(1), _basic_df())[1])
    MV.run("20261022")
    assert len(calls) == 2                                # 两个交易日都重拉
    conn = sqlite3.connect(str(mv_env["path"]))
    n = conn.execute("SELECT COUNT(*) FROM mkt_val_daily WHERE mv_total IS NOT NULL").fetchone()[0]
    conn.close()
    assert n == 2
    calls.clear()
    MV.run("20261022")                                    # 补齐后零调用
    assert calls == []


# ── 比值与分位 ──

def test_gdp_and_buffett(tmp_path, monkeypatch):
    import server.macro as MC
    years = sorted(MC.GDP_ANNUAL)
    assert years == list(range(2005, 2026))               # 连续20+1年
    assert all(MC.GDP_ANNUAL[y] < MC.GDP_ANNUAL[y + 1] for y in years[:-1])
    assert MC._gdp_for(2026) == 140.0 and MC._gdp_for(2004) is None

    db = tmp_path / "sentiment.db"
    c = sqlite3.connect(str(db))
    c.execute("""CREATE TABLE mkt_val_daily(date TEXT PRIMARY KEY, mv_total REAL)""")
    # 2026日: 上年GDP=140万亿 → ratio=mv/(140e4)*100
    c.executemany("INSERT INTO mkt_val_daily VALUES(?,?)",
                  [("2026-01-05", 1000000.0),             # 100万亿 → 71.4%
                   ("2026-06-30", 1250000.0),             # 125万亿 → 89.3%
                   ("2026-10-27", 1100000.0)])            # 110万亿 → 78.6%
    c.commit()
    c.close()
    r = MC.buffett(path_sentiment=db)
    assert len(r["series"]) == 3
    assert r["series"][0]["ratio"] == round(1000000 / 1.4e6 * 100, 1)
    assert r["series"][0]["gdp_year"] == 2025
    L = r["latest"]
    assert L["ratio"] == round(1100000 / 1.4e6 * 100, 1)
    assert L["pct_all"] == 66.7                            # 110万 > 100万 → 2/3


def test_buffett_missing_data(tmp_path):
    import server.macro as MC
    assert MC.buffett(path_sentiment=tmp_path / "none.db") == {"series": [], "latest": None}


def test_api_buffett_empty(server_app):
    r = server_app["client"].get("/admin/api/macro/buffett")
    assert r.status_code == 200
    j = r.get_json()
    assert "series" in j and "latest" in j


def test_page_buffett_wired():
    html = (REPO / "server" / "templates" / "admin" / "macro.html").read_text(encoding="utf-8")
    for marker in ("chartBuffett", "echarts.min.js", "api/macro/buffett",
                   "巴菲特指标", "yAxis: 80", "yAxis: 100", "bfState"):
        assert marker in html, f"缺接线: {marker}"
