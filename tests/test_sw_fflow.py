# -*- coding: utf-8 -*-
"""D1-6 P3 测试: moneyflow 落库/单位换算/聚合/幂等/快速失败/注册表"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import sw_fflow as SF  # noqa: E402
import tushare_api as T  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    raw_p = tmp_path / "raw.db"
    sent_p = tmp_path / "sent.db"
    sent = sqlite3.connect(str(sent_p))
    sent.executescript("""
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE sw_components(index_code TEXT, level TEXT, industry_name TEXT,
        con_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT,
        PRIMARY KEY(index_code, con_code, in_date));
    """)
    for c in ("000001.SZ", "000002.SZ"):
        sent.execute("INSERT INTO sw_components VALUES('801010.SI','L1','农林牧渔',?,'19900101',NULL,'Y')", (c,))
    sent.execute("INSERT INTO sw_components VALUES('801120.SI','L1','食品饮料','600100.SH','19900101',NULL,'Y')")
    for d in ("2007-01-04", "2007-01-05", "2007-01-08", "2007-01-09",
              "2007-01-10", "2007-01-11"):
        sent.execute("INSERT INTO index_daily VALUES('1.000001',?,1000)", (d,))
    sent.commit()
    sent.close()

    import pandas as pd
    # 万元: 000001 主力净 = (100+50)-(60+40)=+50万 → +50万元; 000600 = -30万
    days = {"20070104": [("000001.SZ", 100.0, 60.0, 50.0, 40.0),
                          ("000002.SZ", 10.0, 10.0, 10.0, 10.0),      # 0
                          ("600100.SH", 10.0, 30.0, 10.0, 10.0)],     # -20万
            "20070105": [("000001.SZ", 200.0, 100.0, 0.0, 0.0)]}     # +100万

    def fake_call(api, fields="", **kw):
        assert api == "moneyflow" and fields == SF._FIELDS
        d = kw["trade_date"]
        return pd.DataFrame(
            [{"ts_code": c, "trade_date": d, "buy_elg_amount": be, "sell_elg_amount": se,
              "buy_lg_amount": bb, "sell_lg_amount": sb} for c, be, se, bb, sb in days.get(d, [])])
    monkeypatch.setattr(T, "call", fake_call)

    import raw_daily as RD
    monkeypatch.setattr(RD, "RAW_DB", raw_p)
    monkeypatch.setattr(SF, "connect", lambda: sqlite3.connect(str(sent_p)))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return {"raw": raw_p, "sent": sent_p}


def test_backfill_and_agg(env):
    assert SF.backfill("20070101") is True
    conn = sqlite3.connect(str(env["raw"]))
    n, nd = conn.execute("SELECT COUNT(*), COUNT(DISTINCT trade_date) FROM moneyflow").fetchone()
    conn.close()
    assert (n, nd) == (4, 2)
    assert SF.agg("20070101") is True
    conn = sqlite3.connect(str(env["sent"]))
    # 农林牧渔 0104: (100+50-60-40)=50万 + 0 = 50万 → ×1e4 = 500,000元
    r = conn.execute("SELECT main_net FROM sw_fflow WHERE date='2007-01-04' "
                     "AND index_code='801010.SI'").fetchone()
    assert r[0] == 500000.0
    r = conn.execute("SELECT main_net FROM sw_fflow WHERE date='2007-01-04' "
                     "AND index_code='801120.SI'").fetchone()
    assert r[0] == -200000.0                                # -20万×1e4
    r = conn.execute("SELECT main_net FROM sw_fflow WHERE date='2007-01-05' "
                     "AND index_code='801010.SI'").fetchone()
    assert r[0] == 1000000.0                                # +100万×1e4
    conn.close()


def test_idempotent(env):
    SF.run("20070101")
    SF.run("20070101")
    conn = sqlite3.connect(str(env["raw"]))
    n = conn.execute("SELECT COUNT(*) FROM moneyflow").fetchone()[0]
    conn.close()
    conn = sqlite3.connect(str(env["sent"]))
    m = conn.execute("SELECT COUNT(*) FROM sw_fflow").fetchone()[0]
    conn.close()
    assert n == 4 and m == 3                                 # 2日×(1~2行业)


def test_fastfail(env, monkeypatch):
    def boom(api, fields="", **kw):
        raise ConnectionError("RST")
    monkeypatch.setattr(T, "call", boom)
    assert SF.backfill("20070101") is False


def test_registry(env, monkeypatch):
    import fetch_daily as FD
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(env["sent"])))
    r = FD.run_backfill("sw_fflow", "20070101")
    assert r["status"] == "ok"
    assert r["rows_written"] == 3                            # sw_fflow 3行
    assert r["date_from"] == "2007-01-04"


def test_schema_guard_against_stale_table(env, monkeypatch):
    """旧schema表(buy_big列)存在时, IF NOT EXISTS不重建 → 守卫应报错而非错位写入"""
    import raw_daily as RD
    raw = sqlite3.connect(str(env["raw"]))
    raw.execute("""CREATE TABLE moneyflow(ts_code TEXT, trade_date TEXT,
      buy_elg_amount REAL, sell_elg_amount REAL,
      buy_big_amount REAL, sell_big_amount REAL, PRIMARY KEY(ts_code, trade_date))""")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="列缺失"):
        SF.backfill("20070101")
