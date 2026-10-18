# -*- coding: utf-8 -*-
"""D1-6 P2 测试: 涨停价/限幅边界/判定/连板追踪/行业聚合/官方覆盖/mood只填缺失/注册表"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import sw_pool as SP  # noqa: E402
import tushare_api as T  # noqa: E402
from decimal import Decimal  # noqa: E402


# ── 纯函数: 涨停价与判定 ──

def test_limit_price_rounding_half_up():
    assert SP.limit_price(10.04, Decimal("0.10")) == 11.04      # 11.044→11.04
    assert SP.limit_price(9.87, Decimal("0.10")) == 10.86       # 10.857→10.86(五入)
    assert SP.limit_price(4.17, Decimal("0.10")) == 4.59        # 4.587→4.59
    assert SP.limit_price(10.0, Decimal("0.10"), up=False) == 9.00
    assert SP.limit_price(5.55, Decimal("0.20")) == 6.66        # 6.66


def test_limit_ratio_board_and_date():
    assert SP.limit_ratio("000001.SZ", "20200101") == Decimal("0.10")
    assert SP.limit_ratio("300750.SZ", "20200821") == Decimal("0.10")   # 创业板改革前
    assert SP.limit_ratio("300750.SZ", "20200824") == Decimal("0.20")   # 改革后
    assert SP.limit_ratio("688001.SH", "20190722") == Decimal("0.20")   # 科创板
    assert SP.limit_ratio("830001.BJ", "20220101") == Decimal("0.30")   # 北交所
    assert SP.limit_ratio("600519.SH", "20260101") == Decimal("0.10")


def test_day_flags_zt_dt_zb():
    rows = [
        ("000001.SZ", 10.00, 11.00, 11.00),   # 恰好涨停 → zt
        ("000002.SZ", 10.00, 11.05, 11.05),   # 超限(新股/异常) → 非zt非zb
        ("000003.SZ", 10.00, 10.50, 11.00),   # 触及未封 → zb
        ("000004.SZ", 10.00, 9.00, 9.00),     # 恰好跌停 → dt
        ("000005.SZ", None, 10.0, 10.0),      # 无pre → 排除
    ]
    f = SP._day_flags(rows, "20200101")
    assert f["000001.SZ"] == (True, False, False)
    assert f["000002.SZ"][0] is False and f["000002.SZ"][2] is False
    assert f["000003.SZ"] == (False, False, True)
    assert f["000004.SZ"] == (False, True, False)
    assert "000005.SZ" not in f


# ── 集成: build/overlay/mood 填充 ──

@pytest.fixture()
def env(tmp_path, monkeypatch):
    raw_p = tmp_path / "raw.db"
    sent_p = tmp_path / "sent.db"
    sent = sqlite3.connect(str(sent_p))
    sent.executescript("""
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, dt_count INTEGER,
        zb_count INTEGER, zb_rate REAL, lb_max INTEGER, lb2_count INTEGER,
        jl_rate REAL, yzt_count INTEGER, zt_source TEXT);
      CREATE TABLE sw_components(index_code TEXT, level TEXT, industry_name TEXT,
        con_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT,
        PRIMARY KEY(index_code, con_code, in_date));
    """)
    # 两行业各两股
    for c in ("000001.SZ", "000002.SZ"):
        sent.execute("INSERT INTO sw_components VALUES('801010.SI','L1','农林牧渔',?,'19900101',NULL,'Y')", (c,))
    for c in ("600100.SH", "600200.SH"):
        sent.execute("INSERT INTO sw_components VALUES('801120.SI','L1','食品饮料',?,'19900101',NULL,'Y')", (c,))
    sent.commit()
    sent.close()

    raw = sqlite3.connect(str(raw_p))
    raw.executescript("""
      CREATE TABLE daily(ts_code TEXT, trade_date TEXT, pre_close REAL, close REAL,
        high REAL, PRIMARY KEY(ts_code, trade_date));
      CREATE INDEX idx_daily_tradedate ON daily(trade_date);
    """)
    # 三日行情: 000001 连续两板→第三日断; 000002 首日炸板; 600100 单板
    days = {
        "20050104": [("000001.SZ", 10.0, 11.0, 11.0), ("000002.SZ", 10.0, 10.5, 11.0),
                     ("600100.SH", 20.0, 22.0, 22.0), ("600200.SH", 30.0, 30.5, 30.6)],
        "20050105": [("000001.SZ", 11.0, 12.1, 12.1), ("000002.SZ", 10.5, 10.6, 10.7),
                     ("600100.SH", 22.0, 22.5, 22.6), ("600200.SH", 30.5, 31.0, 31.0)],
        "20050106": [("000001.SZ", 12.1, 12.5, 12.6), ("000002.SZ", 10.6, 10.7, 10.8),
                     ("600100.SH", 22.5, 24.75, 24.75), ("600200.SH", 31.0, 31.5, 31.6)],
    }
    for d, rows in days.items():
        for r in rows:
            raw.execute("INSERT INTO daily VALUES(?,?,?,?,?)", (*r, d) if False else
                        (r[0], d, r[1], r[2], r[3]))
    raw.commit()
    raw.close()

    monkeypatch.setattr(SP, "connect", lambda: sqlite3.connect(str(sent_p)))
    import raw_daily as RD
    monkeypatch.setattr(RD, "RAW_DB", raw_p)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return {"raw": raw_p, "sent": sent_p, "monkeypatch": monkeypatch}


def test_build_est_and_mood_fill(env):
    assert SP.build("20050101") is True
    conn = sqlite3.connect(str(env["sent"]))
    # sw_pool: 两行业 × 3日
    r = conn.execute("SELECT COUNT(*) FROM sw_pool WHERE source='est'").fetchone()[0]
    assert r == 6
    # 20050104 农林牧渔: 000001涨停(streak1) + 000002炸板 → zt1 zb1 lb_max1
    r = conn.execute("SELECT zt_count, dt_count, zb_count, lb_max, lb2_count FROM sw_pool "
                     "WHERE date='2005-01-04' AND index_code='801010.SI'").fetchone()
    assert r == (1, 0, 1, 1, 0)
    # 20050105 000001 二连板 → lb_max2 lb2=1
    r = conn.execute("SELECT zt_count, lb_max, lb2_count FROM sw_pool "
                     "WHERE date='2005-01-05' AND index_code='801010.SI'").fetchone()
    assert r == (1, 2, 1)
    # 20050106 000001 断板(12.5<涨停价13.31) → zt0; jl_rate次日可算
    r = conn.execute("SELECT zt_count FROM sw_pool "
                     "WHERE date='2005-01-06' AND index_code='801010.SI'").fetchone()
    assert r[0] == 0
    # mood_raw est2: 3日全填; 0104全市场 zt2(000001,600100) zb1 lb_max1
    r = conn.execute("SELECT zt_count, zb_count, lb_max, yzt_count, zt_source FROM mood_raw "
                     "WHERE date='2005-01-04'").fetchone()
    assert r == (2, 1, 1, 0, "est2")
    r = conn.execute("SELECT jl_rate FROM mood_raw WHERE date='2005-01-05'").fetchone()
    assert r[0] == 50.0                                   # lb2=1 / 昨日zt=2
    conn.close()


def test_build_mood_fill_skips_existing(env):
    conn = sqlite3.connect(str(env["sent"]))
    conn.execute("INSERT INTO mood_raw(date, zt_count, zt_source) VALUES('2005-01-04', 99, 'ts')")
    conn.commit()
    conn.close()
    SP.build("20050101")
    conn = sqlite3.connect(str(env["sent"]))
    r = conn.execute("SELECT zt_count, zt_source FROM mood_raw WHERE date='2005-01-04'").fetchone()
    assert r == (99, "ts")                                # 已有官方日不动
    conn.close()


def test_overlay_official(env, monkeypatch):
    import pandas as pd
    SP.build("20050101")
    calls = []

    def fake_ll(d):
        calls.append(d)
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "limit": "U", "limit_times": "3"},
            {"ts_code": "000002.SZ", "limit": "Z", "limit_times": ""},
            {"ts_code": "600100.SH", "limit": "D", "limit_times": ""},
        ])
    monkeypatch.setattr(T, "limit_list_d", fake_ll)
    assert SP.overlay("20050104") is True
    conn = sqlite3.connect(str(env["sent"]))
    # 农林牧渔 official: zt=1(000001, 3连板→lb_max3 lb2=1) zb=1(000002)
    r = conn.execute("SELECT zt_count, dt_count, zb_count, lb_max, lb2_count, source "
                     "FROM sw_pool WHERE date='2005-01-04' AND index_code='801010.SI'").fetchone()
    assert r == (1, 0, 1, 3, 1, "official")
    # 幂等: 二跑跳过(不再调用)
    SP.overlay("20050104")
    assert len(calls) == 3                                # 首轮3天, 二轮0
    conn.close()


def test_run_and_registry(env, monkeypatch):
    import fetch_daily as FD
    import pandas as pd
    monkeypatch.setattr(T, "limit_list_d", lambda d: pd.DataFrame([
        {"ts_code": "000001.SZ", "limit": "U", "limit_times": "2"},
        {"ts_code": "600100.SH", "limit": "U", "limit_times": "1"}]))
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(env["sent"])))
    r = FD.run_backfill("sw_pool", "20050101")
    assert r["status"] == "ok"
    conn = sqlite3.connect(str(env["sent"]))
    n_est = conn.execute("SELECT COUNT(*) FROM sw_pool WHERE source='est'").fetchone()[0]
    n_off = conn.execute("SELECT COUNT(*) FROM sw_pool WHERE source='official'").fetchone()[0]
    n_mood = conn.execute("SELECT COUNT(*) FROM mood_raw WHERE zt_source='est2'").fetchone()[0]
    conn.close()
    assert n_mood == 3                                    # mood_raw est2 双目标之一
    assert n_est + n_off > 0                              # sw_pool 增量已度量
    assert r["rows_written"] >= 3 + n_est + n_off
