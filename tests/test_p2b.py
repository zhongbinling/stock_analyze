# -*- coding: utf-8 -*-
"""P2b 测试: namechange拉取/name_at区间与ST判定/_day_flags ST 5%/refresh只刷非官方日"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import sw_names as SN  # noqa: E402
import sw_pool as SP  # noqa: E402
import tushare_api as T  # noqa: E402
from decimal import Decimal  # noqa: E402


@pytest.fixture()
def name_db(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.execute("""CREATE TABLE name_hist(ts_code TEXT, name TEXT,
      start_date TEXT, end_date TEXT, PRIMARY KEY(ts_code, start_date))""")
    conn.executemany("INSERT INTO name_hist VALUES(?,?,?,?)", [
        ("000001.SZ", "平安银行", "19910403", None),
        ("000004.SZ", "国农科技", "20100101", "20150630"),
        ("000004.SZ", "ST国农", "20150701", "20170630"),
        ("000004.SZ", "中国宝安", "20170701", None),
    ])
    conn.commit()
    conn.close()
    monkeypatch.setattr(SN, "connect", lambda: sqlite3.connect(str(p)))
    return p


def test_name_at_intervals(name_db):
    nt = SN.NameTimeline()
    assert nt.name_at("000004.SZ", "20140101") == "国农科技"
    assert nt.name_at("000004.SZ", "20150701") == "ST国农"        # start当日生效
    assert nt.name_at("000004.SZ", "20160101") == "ST国农"
    assert nt.name_at("000004.SZ", "20170701") == "中国宝安"
    assert nt.name_at("000001.SZ", "20260101") == "平安银行"
    assert nt.name_at("999999.SZ", "20260101") is None            # 无记录


def test_name_at_monotonic_pointer(name_db):
    """days升序时指针均摊推进, 日期不回退场景正确"""
    nt = SN.NameTimeline()
    seq = [("20140601", "国农科技"), ("20150630", "国农科技"),
           ("20150701", "ST国农"), ("20171231", "中国宝安")]
    for d, want in seq:
        assert nt.name_at("000004.SZ", d) == want


def test_names_build_idempotent(tmp_path, monkeypatch):
    import pandas as pd
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    monkeypatch.setattr(SN, "connect", lambda: sqlite3.connect(str(p)))
    calls = []

    def fake(api, fields="", **kw):
        calls.append(kw)
        return pd.DataFrame([
            {"ts_code": "000004.SZ", "name": "ST国农",
             "start_date": "20150701", "end_date": "20170630", "ann_date": "20150701"},
            {"ts_code": "000004.SZ", "name": "ST国农",
             "start_date": "20150701", "end_date": "20170630", "ann_date": "20150702"},
        ])
    monkeypatch.setattr(T, "call", fake)
    SN.build(year_from=2015, year_to=2015)
    SN.build(year_from=2015, year_to=2015)
    n = conn.execute("SELECT COUNT(*) FROM name_hist").fetchone()[0]
    conn.close()
    assert n == 1                                             # PK去重, 重复区间不增


def test_day_flags_st_limit():
    rows = [
        ("000004.SZ", 10.00, 10.50, 10.50),   # ST 5%: 涨停价10.50 → zt
        ("000001.SZ", 10.00, 11.00, 11.00),   # 非ST 10%: 涨停价11.00 → zt
        ("000005.SZ", 10.00, 10.50, 11.00),   # 非ST: 触及11.00未封 → 炸板
    ]
    f = SP._day_flags(rows, "20200101", st={"000004.SZ"})
    assert f["000004.SZ"][0] is True                          # ST按5%封板计涨停
    assert f["000001.SZ"][0] is True                          # 非ST不受影响
    assert f["000005.SZ"] == (False, False, True)             # 10%口径下是炸板


def test_limit_ratio_st_override():
    # st_fn 路径绕过 limit_ratio 直接0.05 —— 确认Decimal分支
    rows = [("000004.SZ", 20.00, 21.00, 21.00)]               # 5%→21.00 zt; 10%→22.00非
    f = SP._day_flags(rows, "20200101", st={"000004.SZ"})
    assert f["000004.SZ"][0] is True
    f2 = SP._day_flags(rows, "20200101", st=None)
    assert f2["000004.SZ"][0] is False                        # 10%下21.00不是涨停


def test_build_est_does_not_overwrite_official(tmp_path, monkeypatch):
    """est写入不得覆盖同PK的official行(否则overlay幂等集清空→全量官方API重拉)"""
    import raw_daily as RD
    raw_p, sent_p = tmp_path / "raw.db", tmp_path / "sent.db"
    sent = sqlite3.connect(str(sent_p))
    sent.executescript("""
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, dt_count INTEGER,
        zb_count INTEGER, zb_rate REAL, lb_max INTEGER, lb2_count INTEGER,
        jl_rate REAL, yzt_count INTEGER, up_count INTEGER, down_count INTEGER, zt_source TEXT);
      CREATE TABLE sw_components(index_code TEXT, level TEXT, industry_name TEXT,
        con_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT);
      CREATE TABLE sw_pool(level TEXT, index_code TEXT, industry_name TEXT, date TEXT,
        zt_count INTEGER, dt_count INTEGER, zb_count INTEGER, lb_max INTEGER,
        lb2_count INTEGER, source TEXT, PRIMARY KEY(level, index_code, date));
    """)
    sent.execute("INSERT INTO sw_components VALUES('801010.SI','L1','农林牧渔','000001.SZ','19900101',NULL,'Y')")
    # 0104已有official行
    sent.execute("INSERT INTO sw_pool VALUES('L1','801010.SI','农林牧渔','2005-01-04',9,9,9,9,9,'official')")
    sent.commit()
    sent.close()
    raw = sqlite3.connect(str(raw_p))
    raw.executescript("""
      CREATE TABLE daily(ts_code TEXT, trade_date TEXT, pre_close REAL, close REAL, high REAL);
      CREATE INDEX idx_daily_tradedate ON daily(trade_date);
    """)
    raw.execute("INSERT INTO daily VALUES('000001.SZ','20050104',10.0,11.0,11.0)")
    raw.commit()
    raw.close()
    monkeypatch.setattr(RD, "RAW_DB", raw_p)
    monkeypatch.setattr(SP, "connect", lambda: sqlite3.connect(str(sent_p)))
    monkeypatch.setattr(SP.time, "sleep", lambda s: None)
    SP.build("20050101", refresh=True)
    conn = sqlite3.connect(str(sent_p))
    r = conn.execute("SELECT zt_count, source FROM sw_pool WHERE date='2005-01-04'").fetchone()
    conn.close()
    assert r == (9, "official")                        # official保留, est不清空幂等集


def test_build_refresh_only_nonofficial(tmp_path, monkeypatch):
    """refresh=True: est/缺失日重刷为est2, 官方段(zt_source IS NULL)不动"""
    import raw_daily as RD
    import pandas as pd
    raw_p, sent_p = tmp_path / "raw.db", tmp_path / "sent.db"
    sent = sqlite3.connect(str(sent_p))
    sent.executescript("""
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, dt_count INTEGER,
        zb_count INTEGER, zb_rate REAL, lb_max INTEGER, lb2_count INTEGER,
        jl_rate REAL, yzt_count INTEGER, zt_source TEXT);
      CREATE TABLE sw_components(index_code TEXT, level TEXT, industry_name TEXT,
        con_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT,
        PRIMARY KEY(index_code, con_code, in_date));
      CREATE TABLE name_hist(ts_code TEXT, name TEXT, start_date TEXT, end_date TEXT);
    """)
    sent.execute("INSERT INTO sw_components VALUES('801010.SI','L1','农林牧渔','000001.SZ','19900101',NULL,'Y')")
    # 0105官方(ts_source NULL) / 0106旧est / 0107缺失
    sent.execute("INSERT INTO mood_raw(date, zt_count, zt_source) VALUES('2005-01-05', 99, NULL)")
    sent.execute("INSERT INTO mood_raw(date, zt_count, zt_source) VALUES('2005-01-06', 55, 'est')")
    sent.commit()
    sent.close()
    raw = sqlite3.connect(str(raw_p))
    raw.executescript("""
      CREATE TABLE daily(ts_code TEXT, trade_date TEXT, pre_close REAL, close REAL,
        high REAL, PRIMARY KEY(ts_code, trade_date));
      CREATE INDEX idx_daily_tradedate ON daily(trade_date);
    """)
    for d, close in (("20050105", 11.0), ("20050106", 11.0), ("20050107", 11.0)):
        raw.execute("INSERT INTO daily VALUES('000001.SZ',?,?,?,?)", (d, 10.0, close, 11.0))
    raw.commit()
    raw.close()

    monkeypatch.setattr(RD, "RAW_DB", raw_p)
    monkeypatch.setattr(SP, "connect", lambda: sqlite3.connect(str(sent_p)))
    import sw_names as SN2
    monkeypatch.setattr(SN2, "connect", lambda: sqlite3.connect(str(sent_p)))
    monkeypatch.setattr(SP.time, "sleep", lambda s: None)
    assert SP.build("20050101", refresh=True) is True
    conn = sqlite3.connect(str(sent_p))
    r = conn.execute("SELECT zt_count, zt_source FROM mood_raw WHERE date='2005-01-05'").fetchone()
    assert r == (99, None)                                    # 官方不动
    r = conn.execute("SELECT zt_source FROM mood_raw WHERE date='2005-01-06'").fetchone()
    assert r == ("est2",)                                     # 旧est重刷
    r = conn.execute("SELECT zt_count, zt_source FROM mood_raw WHERE date='2005-01-07'").fetchone()
    assert r[1] == "est2" and r[0] == 1                       # 缺失日补est2
    conn.close()
