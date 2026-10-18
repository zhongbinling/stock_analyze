# -*- coding: utf-8 -*-
"""D1-6 P1 测试: raw_daily 落库/幂等/快速失败 + SW聚合计数/排除规则/幂等 + 注册表"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import raw_daily as RD  # noqa: E402
import tushare_api as T  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时 raw 库 + 临时 sentiment 库(含 sw_components/index_daily) + mock tushare"""
    raw_p = tmp_path / "raw_daily.db"
    sent_p = tmp_path / "sentiment.db"

    sent = sqlite3.connect(str(sent_p))
    sent.executescript("""
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date));
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, up_count INTEGER, down_count INTEGER);
      CREATE TABLE sw_components(index_code TEXT, level TEXT, industry_name TEXT,
        con_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT,
        PRIMARY KEY(index_code, con_code, in_date));
    """)
    # 两个L1行业: 农林牧渔(000001,000002) / 食品饮料(000600); 000999无行业归属
    for c in ("000001.SZ", "000002.SZ"):
        sent.execute("INSERT INTO sw_components VALUES('801010.SI','L1','农林牧渔',?,'19900101',NULL,'Y')", (c,))
    sent.execute("INSERT INTO sw_components VALUES('801120.SI','L1','食品饮料','000600.SH','19900101',NULL,'Y')")
    for d in ("2005-01-04", "2005-01-05", "2005-01-06", "2005-01-07",
              "2005-01-10", "2005-01-11"):
        sent.execute("INSERT INTO index_daily VALUES('1.000001',?,1000)", (d,))
    sent.execute("INSERT INTO mood_raw VALUES('2005-01-06', 2, 1)")
    sent.commit()
    sent.close()

    import pandas as pd
    days = {"20050104": [("000001.SZ", 1.5), ("000002.SZ", -0.5), ("000600.SH", 0.0), ("000999.SZ", 2.0)],
            "20050105": [("000001.SZ", None), ("000002.SZ", 3.0), ("000600.SH", -1.0), ("000999.SZ", 0.0)],
            "20050106": [("000001.SZ", 1.0), ("000002.SZ", 2.0), ("000600.SH", -2.0), ("000999.SZ", 1.0)]}

    def fake_call(api, fields="", **kw):
        if api == "index_daily":
            return pd.DataFrame()                     # 夹具 index_daily 已够用, 无更早数据
        if api == "daily":
            d = kw["trade_date"]
            return pd.DataFrame([{"ts_code": c, "trade_date": d, "open": 1, "high": 1, "low": 1,
                                  "close": 1, "pre_close": 1, "change": 0, "pct_chg": p,
                                  "vol": 100, "amount": 1e6} for c, p in days.get(d, [])])
        raise AssertionError(f"未预期的API: {api} {kw}")

    monkeypatch.setattr(T, "call", fake_call)
    monkeypatch.setattr(RD, "RAW_DB", raw_p)
    monkeypatch.setattr(RD, "connect", lambda: sqlite3.connect(str(sent_p)))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return {"raw": raw_p, "sent": sent_p}


def test_backfill_writes_and_idempotent(env):
    assert RD.backfill("20050101") is True
    conn = sqlite3.connect(str(env["raw"]))
    n1 = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    conn.close()
    assert n1 == 12                                     # 3日 × 4股
    assert RD.backfill("20050101") is True              # 二跑: 已有日跳过, 不重复调用后仍12
    conn = sqlite3.connect(str(env["raw"]))
    n2 = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    conn.close()
    assert n2 == 12


def test_agg_counts_and_exclusions(env):
    RD.backfill("20050101")
    assert RD.agg("20050101") is True
    conn = sqlite3.connect(str(env["sent"]))
    # 2005-01-04: 农林牧渔 000001(+1.5)涨 000002(-0.5)跌 → up1 down1; 食品饮料 000600(0.0)平
    r = conn.execute("SELECT up,down,flat,total FROM sw_breadth WHERE date='2005-01-04' "
                     "AND index_code='801010.SI'").fetchone()
    assert r == (1, 1, 0, 2)
    r = conn.execute("SELECT up,down,flat,total FROM sw_breadth WHERE date='2005-01-04' "
                     "AND index_code='801120.SI'").fetchone()
    assert r == (0, 0, 1, 1)
    # 2005-01-05: 000001 pct为NULL → 不计total; 000999无行业 → 永不出现
    r = conn.execute("SELECT up,down,flat,total FROM sw_breadth WHERE date='2005-01-05' "
                     "AND index_code='801010.SI'").fetchone()
    assert r == (1, 0, 0, 1)                            # 仅000002(+3.0涨)计入
    r = conn.execute("SELECT COUNT(*) FROM sw_breadth WHERE date='2005-01-05' "
                     "AND index_code='801010.SI'").fetchone()
    assert r[0] == 1
    n999 = conn.execute("SELECT COUNT(*) FROM sw_breadth s JOIN sw_components c "
                        "ON s.index_code=c.index_code WHERE c.con_code='000999.SZ'").fetchone()[0]
    assert n999 == 0
    conn.close()
    # 幂等: 二跑不重复
    RD.agg("20050101")
    conn = sqlite3.connect(str(env["sent"]))
    n = conn.execute("SELECT COUNT(*) FROM sw_breadth").fetchone()[0]
    conn.close()
    assert n == 6                                       # 3日 × 2行业


def test_backfill_fastfail(env, monkeypatch):
    def boom(api, fields="", **kw):
        raise ConnectionError("RST")
    monkeypatch.setattr(T, "call", boom)
    assert RD.backfill("20050101") is False


def test_run_chains_backfill_agg(env):
    assert RD.run("20050101") is True
    conn = sqlite3.connect(str(env["sent"]))
    assert conn.execute("SELECT COUNT(*) FROM sw_breadth").fetchone()[0] == 6
    conn.close()


def test_registry_entry(env, monkeypatch):
    import fetch_daily as FD
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(env["sent"])))
    r = FD.run_backfill("sw_breadth", "20050101")
    assert r["status"] == "ok"
    assert r["rows_written"] == 6                       # sw_breadth 逐日增量度量
    assert r["date_from"] == "2005-01-04"


def test_calibrate_reports(env, capsys):
    RD.run("20050101")
    RD.calibrate(days=5)
    out = capsys.readouterr().out
    # 2005-01-06: ΣL1(涨+跌)=3(000001,000002,000600) vs mood_raw(2+1=3) → 偏差0
    assert "平均偏差 0.00%" in out
