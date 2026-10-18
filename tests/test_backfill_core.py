# -*- coding: utf-8 -*-
"""D1-2 core 侧测试: run_backfill 快照前置/逐日增量度量/未知数据集/异常/幂等"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import backup as BK  # noqa: E402
import fetch_daily as FD  # noqa: E402

DATES = ["2026-09-24", "2026-09-25", "2026-09-28"]


@pytest.fixture()
def bf(tmp_path, monkeypatch):
    """临时行情库 + 备份落点 + connect/快照替换"""
    p = tmp_path / "sentiment.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, up_count INTEGER);
      CREATE TABLE sector_pool_stat(date TEXT, code TEXT, zt_count INTEGER);
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
    """)
    for d in DATES:
        conn.execute("INSERT INTO index_daily VALUES('1.000001',?,3000)", (d,))
    conn.commit()
    conn.close()

    snaps = []
    monkeypatch.setattr(BK, "snapshot",
                        lambda asset, btype="manual": snaps.append((asset, btype)))
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(p)))
    return {"path": p, "snaps": snaps, "monkeypatch": monkeypatch}


def _fake_writer(dates=DATES):
    """模拟回补函数(幂等, 同真实函数跳过已有日期): 每日写 mood zt 1行 + pool 2行"""
    def fn(start):
        conn = FD.connect()
        for d in dates:
            if not conn.execute("SELECT 1 FROM mood_raw WHERE date=? AND zt_count IS NOT NULL",
                                (d,)).fetchone():
                conn.execute("INSERT INTO mood_raw VALUES(?,55,NULL)", (d,))
            if not conn.execute("SELECT 1 FROM sector_pool_stat WHERE date=?",
                                (d,)).fetchone():
                conn.execute("INSERT INTO sector_pool_stat VALUES(?,'BK0001',3)", (d,))
                conn.execute("INSERT INTO sector_pool_stat VALUES(?,'BK0002',2)", (d,))
        conn.commit()
        conn.close()
    return fn


def test_run_backfill_happy_path(bf):
    bf["monkeypatch"].setitem(FD.BACKFILLS, "zt_ts",
                              ("测试", FD.BACKFILLS["zt_ts"][1], _fake_writer()))
    r = FD.run_backfill("zt_ts", "20260901")
    assert r["status"] == "ok" and r["error"] is None
    assert r["rows_written"] == 9                       # 3日 × (1 mood + 2 pool)
    assert [d["date"] for d in r["days"]] == DATES
    assert all(d["rows"] == 3 for d in r["days"])
    assert r["date_from"] == DATES[0] and r["date_to"] == DATES[-1]
    assert r["duration_s"] >= 0
    assert ("sentiment", "pre-backfill") in bf["snaps"]  # 前置快照


def test_run_backfill_unknown_dataset(bf):
    r = FD.run_backfill("nope", "20260901")
    assert r["status"] == "fail" and "未知数据集" in r["error"]
    assert r["days"] == [] and r["rows_written"] == 0


def test_run_backfill_executor_raises(bf):
    def boom(start):
        raise RuntimeError("tushare挂了")
    bf["monkeypatch"].setitem(FD.BACKFILLS, "zt_ts",
                              ("测试", FD.BACKFILLS["zt_ts"][1], boom))
    r = FD.run_backfill("zt_ts", "20260901")
    assert r["status"] == "fail" and "RuntimeError" in r["error"]


def test_run_backfill_self_reported_failure(bf):
    """执行函数返回False(内部吞错型, 如calendar网络失败) → status fail"""
    bf["monkeypatch"].setitem(FD.BACKFILLS, "calendar",
                              ("测试", FD.BACKFILLS["calendar"][1], lambda start: False))
    r = FD.run_backfill("calendar", "20240101")
    assert r["status"] == "fail" and "自报失败" in r["error"]


def test_run_backfill_idempotent_second_run(bf):
    bf["monkeypatch"].setitem(FD.BACKFILLS, "zt_ts",
                              ("测试", FD.BACKFILLS["zt_ts"][1], _fake_writer()))
    FD.run_backfill("zt_ts", "20260901")
    r2 = FD.run_backfill("zt_ts", "20260901")           # 再跑: 前置计数已含 → 增量0
    assert r2["status"] == "ok"
    assert r2["rows_written"] == 0 and r2["days"] == []
    assert r2["date_from"] == "2026-09-01"              # 无写入回落请求起始


def test_count_by_date_where_col(bf):
    conn = FD.connect()
    conn.execute("INSERT INTO mood_raw VALUES('2026-09-24',55,2300)")
    conn.execute("INSERT INTO mood_raw VALUES('2026-09-25',NULL,2400)")
    conn.commit()
    conn.close()
    bf["monkeypatch"].setattr(FD, "connect", lambda: sqlite3.connect(str(bf["path"])))
    conn = FD.connect()
    zt = FD._count_by_date(conn, "mood_raw", "zt_count", "date")
    up = FD._count_by_date(conn, "mood_raw", "up_count", "date")
    conn.close()
    assert zt == {"2026-09-24": 1}                      # NULL 不计
    assert up == {"2026-09-24": 1, "2026-09-25": 1}
