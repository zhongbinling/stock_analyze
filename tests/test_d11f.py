# -*- coding: utf-8 -*-
"""D1-11 F1-F3: 基础数据写入守卫 — 批次校验/零污染 + 集成(raw daily, calendar)"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import db as DB          # noqa: E402
import raw_daily as RD   # noqa: E402
import fetch_daily as FD  # noqa: E402
import tushare_api as TA  # noqa: E402

GOOD = [("000001.SZ", "20260101", 10.0), ("000002.SZ", "20260101", 20.0)]


@pytest.fixture()
def tconn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE daily(ts_code TEXT, trade_date TEXT, close REAL, "
              "PRIMARY KEY(ts_code, trade_date))")
    return c


# ── F2 原语: guarded_day_write ──

def test_guarded_day_write_ok(tconn):
    n = DB.guarded_day_write(tconn, "INSERT OR REPLACE INTO daily VALUES(?,?,?)",
                             GOOD, "2026-01-01", table="daily")
    assert n == 2
    assert tconn.execute(
        "SELECT COUNT(*) FROM daily WHERE trade_date='20260101'").fetchone()[0] == 2


def test_guarded_day_write_idempotent_rerun(tconn):
    sql = "INSERT OR REPLACE INTO daily VALUES(?,?,?)"
    DB.guarded_day_write(tconn, sql, GOOD, "2026-01-01", table="daily")
    DB.guarded_day_write(tconn, sql, GOOD, "2026-01-01", table="daily")
    assert tconn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 2


@pytest.mark.parametrize("rows,frag", [
    ([], "空批次"),
    ([("000001.SZ", "20251231", 1.0)], "他日"),
    ([("000001.SZ", "20260101", 1.0), ("000001.SZ", "20260101", 2.0)], "重复"),
    ([(None, "20260101", 1.0)], "空值"),
    ([("000002.SZ", "20260101", float("nan"))], ""),   # nan值列合法, 主键正常 → 应通过
    ([("000002.SZ", "20260101", 1.0), (float("nan"), "20260101", 2.0)], "空值/NaN"),
], ids=["empty", "wrong-date", "dup-pk", "null-pk", "nan-value-ok", "nan-pk"])
def test_guarded_day_write_rejects_and_keeps_table_clean(tconn, rows, frag):
    before = tconn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    if not frag:                                  # nan值列(非主键)属合法缺失, 应回归通过
        DB.guarded_day_write(tconn, "INSERT OR REPLACE INTO daily VALUES(?,?,?)",
                             rows, "2026-01-01", table="daily")
        assert tconn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == before + 1
        return
    with pytest.raises(RuntimeError, match=frag):
        DB.guarded_day_write(tconn, "INSERT OR REPLACE INTO daily VALUES(?,?,?)",
                             rows, "2026-01-01", table="daily")
    assert tconn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == before  # 零污染


# ── F2 原语: guarded_batch ──

def test_guarded_batch_check_rejects(tconn):
    def bad_date(rs):
        return "日期格式异常" if any(not str(r[1]).isdigit() for r in rs) else None
    with pytest.raises(RuntimeError, match="格式"):
        DB.guarded_batch(tconn, "INSERT OR REPLACE INTO daily VALUES(?,?,?)",
                         [("000001.SZ", "bad-date", 1.0)], label="t", check=bad_date)
    assert tconn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 0


def test_guarded_batch_ok(tconn):
    n = DB.guarded_batch(tconn, "INSERT OR REPLACE INTO daily VALUES(?,?,?)",
                         GOOD, label="t")
    assert n == 2


# ── F3 集成: raw daily 守卫 ──

@pytest.fixture()
def raw_env(tmp_path, monkeypatch):
    """raw库指tmp + sentiment库(含1个交易日)指tmp, 绕开真实库"""
    monkeypatch.setattr(RD, "RAW_DB", tmp_path / "raw.db")
    monkeypatch.setattr(DB, "DB_PATH", tmp_path / "sentiment.db")
    scon = DB.connect()
    scon.execute("INSERT OR REPLACE INTO index_daily(code,date,open,high,low,close,"
                 "volume,amount,pct_chg) VALUES('1.000001','2026-01-01',1,1,1,1,1,1,1)")
    scon.commit()
    scon.close()
    return tmp_path


def _daily_df(rows):
    cols = RD._DAILY_FIELDS.split(",")
    return pd.DataFrame(rows, columns=cols)


def test_raw_backfill_bad_day_rejected_zero_pollution(raw_env, monkeypatch, capsys):
    """F3: 网络返回错日期批次 → 该日记为失败, 主表零写入, 报错可读"""
    bad = _daily_df([("000001.SZ", "20251231", 1, 1, 1, 1, 1, 0, 1, 100, 100)])
    monkeypatch.setattr(TA, "call", lambda *a, **k: bad)
    RD.backfill("20260101")
    conn = RD.raw_connect()
    assert conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 0
    conn.close()
    assert "拒绝写入" in capsys.readouterr().out


def test_raw_backfill_good_day_written(raw_env, monkeypatch):
    good = _daily_df([("000001.SZ", "20260101", 1, 1, 1, 1, 1, 0, 1, 100, 100),
                      ("000002.SZ", "20260101", 1, 1, 1, 2, 2, 1, 1, 100, 100)])
    monkeypatch.setattr(TA, "call", lambda *a, **k: good)
    RD.backfill("20260101")
    conn = RD.raw_connect()
    assert conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 2
    conn.close()


# ── F3 集成: calendar 守卫 ──

def _cal_df(rows):
    return pd.DataFrame(rows, columns=["cal_date", "is_open", "pretrade_date"])


@pytest.fixture()
def cal_env(tmp_path, monkeypatch):
    monkeypatch.setattr(DB, "DB_PATH", tmp_path / "sentiment.db")
    monkeypatch.setattr(TA, "has_token", lambda: True)
    return DB.connect()


def test_calendar_short_batch_rejected(cal_env, monkeypatch, capsys):
    """F3: 半截响应(行数<下限) → 整批拒写, 旧日历原样保留"""
    cal_env.execute("INSERT INTO calendar VALUES('20260101',1,'20251231')")
    cal_env.commit()
    short = _cal_df([(f"2026{m:02d}{d:02d}", 1, "20251231")
                     for m in range(1, 2) for d in range(1, 6)])   # 5行 < 400
    monkeypatch.setattr(TA, "trade_cal", lambda *a, **k: short)
    assert FD.fetch_calendar() is False
    assert cal_env.execute("SELECT COUNT(*) FROM calendar").fetchone()[0] == 1  # 旧表未动
    assert "下限400" in capsys.readouterr().out


def test_calendar_bad_is_open_rejected(cal_env, monkeypatch):
    bad = _cal_df([(f"{2024 + i // 400}{(i % 400) // 30 + 1:02d}{i % 30 + 1:02d}",
                    2, "20230101") for i in range(500)])            # is_open=2 非法
    monkeypatch.setattr(TA, "trade_cal", lambda *a, **k: bad)
    assert FD.fetch_calendar() is False
    assert cal_env.execute("SELECT COUNT(*) FROM calendar").fetchone()[0] == 0


def test_calendar_good_batch_written(cal_env, monkeypatch):
    good = _cal_df([(f"{y}{m:02d}{d:02d}", (d + m) % 2, "20230101")
                    for y in (2025, 2026) for m in range(1, 13) for d in range(1, 29)])
    monkeypatch.setattr(TA, "trade_cal", lambda *a, **k: good)
    assert FD.fetch_calendar() is True
    assert cal_env.execute("SELECT COUNT(*) FROM calendar").fetchone()[0] == 672


# ── D1-12 F2收尾: 校验构件 + mood值域 + 批写点守卫 ──

def test_check_helpers():
    import db as DB
    rows = [("a", "2026-01-01", 5), ("a", "2026-01-02", 7)]
    assert DB.chk_same(0, "a")(rows) is None
    assert DB.chk_same(0, "b")(rows) is not None
    assert DB.chk_unique(1)(rows) is None
    assert DB.chk_unique(0)(rows) is not None               # 'a'重复
    assert DB.chk_range(2, 0, 10)(rows) is None
    assert DB.chk_range(2, 0, 6)(rows) is not None
    assert DB.chk_range(2, 0, 10)([("a", "d", float("nan"))]) is not None  # NaN越界
    assert DB.chk_range(2, 0, 10)([("a", "d", None)]) is None              # None容许
    combo = DB.all_checks(DB.chk_same(0, "a"), DB.chk_unique(1))
    assert combo(rows) is None
    assert combo([("a", "d", 1), ("a", "d", 2)]) is not None


def test_upsert_mood_domain_validation():
    import db as DB
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER,
      dt_count INTEGER, zb_count INTEGER, zb_rate REAL, lb_max INTEGER,
      lb2_count INTEGER, jl_rate REAL, prev_zt_chg REAL, yzt_count INTEGER,
      up_count INTEGER, down_count INTEGER)""")
    DB.upsert_mood(c, "2026-10-23", zt_count=50, dt_count=3, zb_rate=12.5, lb_max=8)
    assert c.execute("SELECT zt_count FROM mood_raw").fetchone()[0] == 50
    for bad in ({"zt_count": -1}, {"zt_count": 99999}, {"lb_max": 99},
                {"zb_rate": 150.0}, {"jl_rate": -5.0}, {"prev_zt_chg": 200.0}):
        with pytest.raises(RuntimeError, match="越界"):
            DB.upsert_mood(c, "2026-10-24", **bad)
    with pytest.raises(RuntimeError, match="日期非法"):
        DB.upsert_mood(c, "20261024", zt_count=5)
    assert c.execute("SELECT COUNT(*) FROM mood_raw").fetchone()[0] == 1  # 拒写零污染


def test_upsert_bars_guard_rejects_bad_ohlc():
    import sectors as S
    import pandas as pd
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE sector_daily(code TEXT, date TEXT, open REAL, close REAL, "
              "high REAL, low REAL, amount REAL, PRIMARY KEY(code, date))")
    c.execute("CREATE TABLE sectors(code TEXT PRIMARY KEY, name TEXT, kind TEXT, "
              "enabled INTEGER, first_date TEXT, last_date TEXT, updated_at TEXT)")
    bad = pd.DataFrame([{"date": "2026-10-19", "open": 10, "close": 11,
                         "high": 9, "low": 8, "amount": 1}])       # high < open
    with pytest.raises(RuntimeError, match="OHLC"):
        S._upsert_bars(c, "BK0001", bad)
    assert c.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0] == 0
    good = pd.DataFrame([{"date": "2026-10-19", "open": 8, "close": 9,
                          "high": 10, "low": 7, "amount": 1}])
    assert S._upsert_bars(c, "BK0001", good) == 1
