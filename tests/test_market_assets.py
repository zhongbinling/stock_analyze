# -*- coding: utf-8 -*-
"""D1-1 数据资产清单测试: 注册表驱动 datasets() 的完成度%/缺口/基准回退/SW映射"""
import datetime as dt
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server import market  # noqa: E402

GAPS_INDEX = ["2026-09-10", "2026-09-11"]           # 指数挖2天
GAPS_SECTOR = ["2026-09-17", "2026-09-18", "2026-09-21"]  # 板块日线挖3天


def _weekdays(d1, d2):
    d, out = d1, []
    while d <= d2:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def build_asset_db(path: Path):
    days = _weekdays(dt.date(2026, 8, 3), dt.date(2026, 9, 30))
    conn = sqlite3.connect(str(path))
    conn.executescript("""
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, up_count INTEGER);
      CREATE TABLE sector_daily(code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
      CREATE TABLE sector_pool_stat(date TEXT, code TEXT, zt_count INTEGER);
      CREATE TABLE sector_fundflow(date TEXT, code TEXT, main_net REAL);
      CREATE TABLE margin_daily(date TEXT PRIMARY KEY, rzrqye REAL);
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE sectors(code TEXT PRIMARY KEY, name TEXT, kind TEXT, enabled INTEGER,
        first_date TEXT, last_date TEXT, updated_at TEXT);
      CREATE TABLE sw1_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT, PRIMARY KEY(bk_code));
      CREATE TABLE sw2_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT, sw2_name TEXT, PRIMARY KEY(bk_code));
    """)
    for d in days:
        conn.execute("INSERT INTO calendar VALUES(?,1)",
                     (d.replace("-", ""),))
        if d not in GAPS_INDEX:
            conn.execute("INSERT INTO index_daily VALUES('1.000001',?,3000)", (d,))
        conn.execute("INSERT INTO mood_raw VALUES(?,55,2300)", (d,))
        if d not in GAPS_SECTOR:
            for c in ("BK0001", "BK0002"):
                conn.execute("INSERT INTO sector_daily VALUES(?,?,10)", (c, d))
        conn.execute("INSERT INTO sector_pool_stat VALUES(?,'BK0001',3)", (d,))
        conn.execute("INSERT INTO sector_fundflow VALUES(?,'BK0001',1e6)", (d,))
        conn.execute("INSERT INTO margin_daily VALUES(?,1.8e12)", (d,))
    conn.execute("INSERT INTO sectors VALUES('BK0001','半导体','industry',1,NULL,NULL,'2026-09-30')")
    conn.execute("INSERT INTO sectors VALUES('BK0002','白酒','industry',1,NULL,NULL,'2026-09-30')")
    conn.execute("INSERT INTO sw1_map VALUES('BK0001','半导体','信息技术')")
    conn.execute("INSERT INTO sw1_map VALUES('BK0002','白酒','消费')")
    conn.execute("INSERT INTO sw2_map VALUES('BK0001','半导体','信息技术','半导体')")
    conn.execute("INSERT INTO sw2_map VALUES('BK0002','白酒','消费','白酒')")
    conn.commit()
    conn.close()
    return len(days)                  # 期望交易日数


@pytest.fixture()
def assets(tmp_path, monkeypatch):
    p = tmp_path / "assets.db"
    total = build_asset_db(p)
    monkeypatch.setattr(market, "SENT_DB", p)
    monkeypatch.setattr(market, "_cfg_cache", None)
    # 夹具日期固定在2026-09(历史), 陈旧度置0以专测完成度/缺口逻辑
    monkeypatch.setattr(market, "_stale_days", lambda last: 0)
    return {"path": p, "total": total, "market": market}


def _by_code(ds, code):
    return next(d for d in ds if d["code"] == code)


def test_registry_entries_present(assets):
    codes = [d["code"] for d in assets["market"].datasets()]
    for c in ("index", "pool", "breadth", "sector_daily", "sector_pool",
              "sector_fflow", "margin", "calendar", "sectors", "sw1", "sw2", "sw3"):
        assert c in codes, f"缺资产 {c}"


def test_completeness_and_missing_index(assets):
    total = assets["total"]
    d = _by_code(assets["market"].datasets(), "index")
    assert d["missing_n"] == 2
    assert set(GAPS_INDEX) <= set(d["missing_recent"])
    assert d["completeness"] == round((total - 2) / total * 100)
    assert d["health"] == "warn"                      # <95 因缺口(43-2)/43≈95→round 95


def test_completeness_and_missing_sector_daily(assets):
    total = assets["total"]
    d = _by_code(assets["market"].datasets(), "sector_daily")
    assert d["missing_n"] == 3
    assert d["completeness"] == round((total - 3) / total * 100)
    assert d["codes"] == 2                            # 板块数单列
    assert d["health"] == "warn"


def test_full_dataset_completeness_100(assets):
    d = _by_code(assets["market"].datasets(), "pool")
    assert d["completeness"] == 100 and d["missing_n"] == 0 and d["health"] == "ok"


def test_extra_rows_clamped_to_100(assets):
    """多余非交易日行(脏数据)不推高完成度"""
    conn = sqlite3.connect(str(assets["path"]))
    conn.execute("INSERT INTO sector_fundflow VALUES('BK0001','2026-08-08',0)")  # 周六
    conn.execute("INSERT INTO sector_fundflow VALUES('BK0001','2026-08-09',0)")  # 周日
    conn.commit()
    conn.close()
    d = _by_code(assets["market"].datasets(), "sector_fflow")
    assert d["completeness"] == 100


def test_fallback_when_calendar_empty(assets):
    """日历为空 → 回退主力指数日期作基准, 缺口结论一致"""
    conn = sqlite3.connect(str(assets["path"]))
    conn.execute("DELETE FROM calendar")
    conn.commit()
    conn.close()
    ds = assets["market"].datasets()
    assert _by_code(ds, "sector_daily")["missing_n"] == 3
    # 指数自身基准=自身日期 → 完成度100
    assert _by_code(ds, "index")["completeness"] == 100


def test_sw_map_rows_and_sw3_placeholder(assets):
    ds = assets["market"].datasets()
    s1 = _by_code(ds, "sw1")
    assert s1["rows"] == 2 and "覆盖 2 个东财BK" in s1["note"] and "2 个一级" in s1["note"]
    s3 = _by_code(ds, "sw3")
    assert s3["rows"] == 0 and s3["health"] == "warn" and "未构建" in s3["note"]


def test_note_contains_gap_detail(assets):
    d = _by_code(assets["market"].datasets(), "sector_daily")
    assert "缺 3 天" in d["note"] and "2026-09-21" in d["note"]


def test_missing_table_skipped_gracefully(assets):
    conn = sqlite3.connect(str(assets["path"]))
    conn.execute("DROP TABLE margin_daily")
    conn.commit()
    conn.close()
    codes = [d["code"] for d in assets["market"].datasets()]
    assert "margin" not in codes                     # 表缺失跳过, 不炸
