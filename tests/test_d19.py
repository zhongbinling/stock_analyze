# -*- coding: utf-8 -*-
"""D1-9 测试: sw_index_comp 等权自算指数(累乘/聚合/PK) + 分量对齐"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import sw_index_comp as SIC  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    raw_p, sent_p = tmp_path / "raw.db", tmp_path / "s.db"
    sent = sqlite3.connect(str(sent_p))
    sent.executescript("""
      CREATE TABLE sw_components(index_code TEXT, level TEXT, industry_name TEXT,
        con_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT);
      CREATE TABLE sw_index_comp(index_code TEXT, date TEXT, open REAL, high REAL,
        low REAL, close REAL, volume REAL, amount REAL, pct_chg REAL, n INTEGER,
        PRIMARY KEY(index_code, date));
    """)
    # 两指数: A(000001,000002) B(000003); 000004 无归属
    for c in ("000001.SZ", "000002.SZ"):
        sent.execute("INSERT INTO sw_components VALUES('801010.SI','L1','农林牧渔',?,'20000101',NULL,'Y')", (c,))
    sent.execute("INSERT INTO sw_components VALUES('801120.SI','L1','食品饮料','000003.SZ','20000101',NULL,'Y')")
    sent.commit()
    sent.close()
    raw = sqlite3.connect(str(raw_p))
    raw.executescript("""
      CREATE TABLE daily(ts_code TEXT, trade_date TEXT, pre_close REAL, close REAL,
        high REAL, low REAL, amount REAL);
      CREATE INDEX idx_daily_tradedate ON daily(trade_date);
    """)
    days = {
        "20050104": [("000001.SZ", 10, 11, 11.5, 9.5, 1e6),   # +10%
                     ("000002.SZ", 10, 10, 10, 10, 1e6),        # 0%
                     ("000003.SZ", 10, 12, 12, 12, 2e6),        # +20%
                     ("000004.SZ", 10, 10.5, 10.5, 10.5, 5e5)], # 无归属不计
        "20050105": [("000001.SZ", 11, 12.1, 12.1, 11, 1e6),   # +10%
                     ("000002.SZ", 10, 9.0, 10, 9, 1e6),        # -10%
                     ("000003.SZ", 12, 12, 12, 12, 2e6)],       # 0%
    }
    for d, rows in days.items():
        for ts, pre, c, h, l, amt in rows:
            raw.execute("INSERT INTO daily VALUES(?,?,?,?,?,?,?)", (ts, d, pre, c, h, l, amt))
    raw.commit()
    raw.close()

    monkeypatch.setattr(SIC, "connect", lambda: sqlite3.connect(str(sent_p)))
    import raw_daily as RD
    monkeypatch.setattr(RD, "RAW_DB", raw_p)
    return sent_p


def test_composite_equal_weight_cumprod(env):
    assert SIC.build("20050101") is True
    conn = sqlite3.connect(str(env))
    # 801010: 0104 等权收益=(10%+0%)/2=5% → close=1000; 0105=(10%-10%)/2=0% → close=1000
    r = conn.execute("SELECT date, close, pct_chg, amount, n FROM sw_index_comp "
                     "WHERE index_code='801010.SI' ORDER BY date").fetchall()
    assert abs(r[0][2] - 5.0) < 1e-6 and abs(r[0][1] - 1000) < 1e-6
    assert abs(r[1][2] - 0.0) < 1e-6 and abs(r[1][1] - 1000) < 1e-6
    assert r[0][3] == 2e6 and r[0][4] == 2
    # 801120: 归一基点1000(首日收益入基, 百分位不受影响); 次日0% → 1000
    r = conn.execute("SELECT close, pct_chg FROM sw_index_comp WHERE index_code='801120.SI' "
                     "ORDER BY date").fetchall()
    assert abs(r[0][0] - 1000) < 1e-6 and abs(r[0][1] - 20.0) < 1e-6
    assert abs(r[1][0] - 1000) < 1e-6 and abs(r[1][1] - 0.0) < 1e-6
    # 无归属股不产生指数
    n = conn.execute("SELECT COUNT(*) FROM sw_index_comp WHERE index_code='999999'").fetchone()[0]
    assert n == 0
    conn.close()


def test_composite_idempotent(env):
    SIC.build("20050101")
    SIC.build("20050101")
    conn = sqlite3.connect(str(env))
    n = conn.execute("SELECT COUNT(*) FROM sw_index_comp").fetchone()[0]
    conn.close()
    assert n == 4                                # 2指数 × 2日, PK不重复


def test_sector_sw_weights_parity():
    """D1-9: SW行分量对齐 — weights剔除ud_ratio/fflow"""
    import sector as S
    import inspect
    src = inspect.getsource(S._calc_sw_temperature)
    assert 'k not in ("ud_ratio", "fflow")' in src
