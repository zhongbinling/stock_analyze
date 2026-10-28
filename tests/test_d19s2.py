# -*- coding: utf-8 -*-
"""D1-19 段2: ETF逐日分母假缺口归零 / thin涨跌仅近端 / 同表多target度量 / scanned落库"""
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


# ── P2: ETF 逐日分母 ──

@pytest.fixture()
def etf_db(tmp_path, monkeypatch):
    """迷你库: 8只ETF分批上市(10-26单只→10-28满员8只→10-29真半截4只)"""
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE etf_daily(code TEXT, date TEXT, close REAL, amount REAL,
        PRIMARY KEY(code, date));
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, up_count INTEGER);
      CREATE TABLE sector_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE sector_pool_stat(date TEXT, code TEXT, zt_count INTEGER);
      CREATE TABLE sector_fundflow(code TEXT, date TEXT, main_net REAL);
      CREATE TABLE margin_daily(date TEXT PRIMARY KEY, rzrqye REAL);
      CREATE TABLE sw_daily(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_index_comp(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_breadth(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_pool(index_code TEXT, date TEXT, x REAL);
      CREATE TABLE sw_fflow(index_code TEXT, date TEXT, x REAL);
    """)
    days = ("2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29")
    for d in days:
        conn.execute("INSERT INTO calendar VALUES(?,1)", (d.replace("-", ""),))
        conn.execute("INSERT INTO index_daily VALUES('1.000001',?,3000)", (d,))
    conn.execute("INSERT INTO etf_daily VALUES('A','2026-10-26',1,1)")
    conn.execute("INSERT INTO etf_daily VALUES('A','2026-10-27',1,1)")
    conn.execute("INSERT INTO etf_daily VALUES('B','2026-10-27',1,1)")
    for c in "ABCDEFGH":                     # 10-28: 满员8只(健康水位锚)
        conn.execute("INSERT INTO etf_daily VALUES(?, '2026-10-28',1,1)", (c,))
    for c in "ABGH":                         # 10-29: 真半截(4/8 < 60%)
        conn.execute("INSERT INTO etf_daily VALUES(?, '2026-10-29',1,1)", (c,))
    conn.commit()
    conn.close()
    import server.market as M
    monkeypatch.setattr(M, "SENT_DB", p)
    monkeypatch.setattr(M, "_cfg_cache", None)
    cfgcore = tmp_path / "c"
    cfgcore.mkdir()
    shutil.copy(CORE / "config.yaml", cfgcore / "config.yaml")
    monkeypatch.setattr(M, "CORE", cfgcore)
    return M


def test_covered_dates_etf_per_day_denominator(etf_db):
    """早期按当时上市规模判(1只上市1行=覆盖); 满员后的真半截日(4/8)未覆盖"""
    M = etf_db
    con = M._conn()
    cov = M._covered_dates(con, "etf_daily", "date", "", "etf_daily")
    con.close()
    assert cov == {"2026-10-26", "2026-10-27", "2026-10-28"}   # 10-29(4/8)剔除


def test_etf_phantom_gap_gone_and_real_gap_counted(etf_db, monkeypatch):
    """分批上市不再产生假缺口: 10-26(1行)/10-27(2行)均为覆盖;
    同库仅 10-29 计1缺口(不是3) — 2013-2018同型45日假缺口回归"""
    ds = {d["code"]: d for d in etf_db.datasets()}
    e = ds["etf"]
    assert e["missing_n"] == 1 and e["missing_recent"] == ["2026-10-29"]
    assert e["completeness"] == 75 and e["health"] == "bad"   # <80%按规则bad
    days = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
    monkeypatch.setattr(etf_db, "_trading_days_since", lambda w: list(days))
    monkeypatch.setattr(etf_db, "_today", lambda: "2026-10-30")
    by = {a["code"]: a for a in etf_db.scan_gaps(30)["assets"]}
    assert by["etf"]["missing"] == ["2026-10-29"]


# ── P5: thin 涨跌仅近端 ──

def test_thin_dates_ud_requirement_only_recent(tmp_path, monkeypatch):
    """老日期OHLC全但涨跌缺 → 不再判瘦(涨跌源只存近期); 近端涨跌缺 → 判瘦;
    OHLC不足 → 无论新老都判瘦(半截日防护不变)"""
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE sector_daily(code TEXT, date TEXT, close REAL, "
                 "up_count INTEGER, PRIMARY KEY(code, date))")
    dates = [f"2026-0{i}-0{d}" for i in (1, 2) for d in range(1, 4)] + ["2026-03-01"]
    for d in dates[:-1]:                       # 前6日: OHLC全, 涨跌全无(老日期)
        for c in ("BK0", "BK1", "BK2"):
            conn.execute("INSERT INTO sector_daily VALUES(?,?,10,NULL)", (c, d))
    for c in ("BK0", "BK1", "BK2"):            # 末日: OHLC全+涨跌全 → 健康
        conn.execute("INSERT INTO sector_daily VALUES(?,?,10,5)", (c, dates[-1]))
    conn.commit()
    conn.close()
    import fetch_dc as FD
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(p)))
    conn = FD.connect()
    thin = FD._thin_dates(conn, dates, need=3, ud_window=1)   # 仅末日制"近期"
    conn.close()
    assert thin == []                          # 老日期涨跌缺不再触发
    # 近端涨跌缺 → 判瘦
    conn = sqlite3.connect(str(p))
    conn.execute("UPDATE sector_daily SET up_count=NULL WHERE date=?", (dates[-1],))
    conn.commit()
    conn.close()
    conn = FD.connect()
    assert FD._thin_dates(conn, dates, need=3, ud_window=1) == [dates[-1]]
    # OHLC不足(半截) → 老日期也判瘦
    conn.execute("DELETE FROM sector_daily WHERE date=? AND code!='BK0'", (dates[0],))
    conn.commit()
    conn.close()
    conn = FD.connect()
    thin = FD._thin_dates(conn, dates, need=3, ud_window=1)
    conn.close()
    assert dates[0] in thin


# ── P4a/P4b: 同表多 target 度量 + scanned ──

@pytest.fixture()
def bf2(tmp_path, monkeypatch):
    import fetch_daily as FD
    import backup as BK
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE sector_daily(code TEXT, date TEXT, close REAL, up_count INTEGER,
        PRIMARY KEY(code, date));
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
    """)
    for d in ("2026-09-24", "2026-09-25", "2026-09-28"):
        conn.execute("INSERT INTO index_daily VALUES('1.000001',?,3000)", (d,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(BK, "snapshot", lambda *a, **k: None)
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(p)))
    return FD


def test_same_table_targets_no_phantom_delta(bf2, monkeypatch):
    """D1-19 P4a: dc_daily型(同表close+up_count双target)幂等重写不再被计成
    新增全史 — pre 按target元组隔离, 第二跑增量0"""
    def writer(start):
        conn = bf2.connect()
        for d in ("2026-09-24", "2026-09-25", "2026-09-28"):
            for c in ("BK0", "BK1"):
                conn.execute(
                    "INSERT INTO sector_daily VALUES(?,?,10,NULL) "
                    "ON CONFLICT(code,date) DO UPDATE SET close=excluded.close",
                    (c, d))
        conn.commit()
        conn.close()
    tgts = [("sector_daily", "close", "date"), ("sector_daily", "up_count", "date")]
    monkeypatch.setitem(bf2.BACKFILLS, "_t", ("测试", tgts, writer, "20260901"))
    r1 = bf2.run_backfill("_t", "20260901")
    assert r1["rows_written"] == 6 and len(r1["days"]) == 3     # 首跑=真实新增
    r2 = bf2.run_backfill("_t", "20260901")                     # 幂等重写
    assert r2["rows_written"] == 0 and r2["days"] == []         # 旧口径此处=6(假)
    assert r2["scanned"] == 3                                   # 3个交易日扫过


def test_scanned_counts_trading_days(bf2, monkeypatch):
    """P4b: scanned = [start, 数据末日]内交易日(与新增日分离)"""
    def writer(start):
        conn = bf2.connect()
        conn.execute("INSERT INTO sector_daily VALUES('BK0','2026-09-25',10,NULL)")
        conn.commit()
        conn.close()
    monkeypatch.setitem(bf2.BACKFILLS, "_t",
                        ("测试", [("sector_daily", "close", "date")],
                         writer, "20260901"))
    r = bf2.run_backfill("_t", "20260901")     # 写1日; 数据末日=09-25 → 扫2
    assert r["days"] == [{"date": "2026-09-25", "rows": 1}]
    assert r["scanned"] == 2                   # 09-24/09-25(末日=数据末日, 尾部无数据日不计)


def test_scanned_capped_at_today(bf2, monkeypatch):
    """P4r: calendar类target数据末日在未来(2027) — 未来日历日不计入扫描"""
    def writer(start):
        conn = bf2.connect()
        for d in ("2026-09-24", "2026-09-25", "2026-09-28", "2027-01-01"):
            conn.execute("INSERT INTO calendar VALUES(?,1)", (d.replace("-", ""),))
        conn.commit()
        conn.close()
    monkeypatch.setitem(bf2.BACKFILLS, "_c",
                        ("测试", [("calendar", None, "cal_date")],
                         writer, "20260901"))
    r = bf2.run_backfill("_c", "20260901")
    assert len(r["days"]) == 4                               # 新增照计(含未来日历行)
    assert r["scanned"] == 3                # horizon=2027被today截断, 未来日不计(旧口径=4)


# ── P4 落库与展示 ──

def test_task_backfill_stores_scanned(server_app, monkeypatch):
    """task_backfill 落 scanned_n; 日志API带回 scanned"""
    from server import jobs
    from server.models import DataBackfillLog

    def fake_run(cmd, append, cwd=None, env_extra=None):
        append("@@BACKFILL@@ " + json.dumps({
            "dataset": "etf_daily", "label": "x", "status": "ok",
            "date_from": "2026-01-01", "date_to": "2026-01-02",
            "rows_written": 4, "scanned": 5277,
            "days": [{"date": "2026-01-01", "rows": 4}],
            "duration_s": 1, "error": None}))
        return 0
    monkeypatch.setattr(jobs, "_run", fake_run)
    with server_app["app"].app_context():
        assert jobs.task_backfill("etf_daily", "20260101")(lambda s: None) == 0
        log = DataBackfillLog.query.order_by(DataBackfillLog.id.desc()).first()
        assert log.scanned_n == 5277
    r = server_app["client"].get("/admin/api/backfill/logs?dataset=etf_daily")
    assert r.get_json()["logs"][0]["scanned"] == 5277


def test_html_last_cell_dual_counts():
    """UI: '新增N日/扫M日'双数(旧记录无scanned时优雅降级)"""
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "新增${(L.days" in html and "/扫${L.scanned}" in html
    assert "写${(L.days" not in html and "+${(L.days" not in html


def test_scanned_n_column_and_migration():
    """models列 + Alembic迁移文件存在(升级路径可复现)"""
    from server.models import DataBackfillLog
    assert hasattr(DataBackfillLog, "scanned_n")
    mig = REPO / "migrations" / "versions" / "d19p4_data_backfill_log_scanned.py"
    assert mig.exists() and "scanned_n" in mig.read_text(encoding="utf-8")
