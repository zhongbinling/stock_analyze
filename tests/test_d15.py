# -*- coding: utf-8 -*-
"""D1-5 测试: backfill_sector_history 回补/幂等/跳过/熔断 + 注册表联动"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import fetch_daily as FD  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE sectors(code TEXT PRIMARY KEY, name TEXT, kind TEXT, enabled INTEGER,
        first_date TEXT, last_date TEXT, updated_at TEXT);
      CREATE TABLE sector_daily(code TEXT, date TEXT, open REAL, close REAL, high REAL,
        low REAL, amount REAL, up_count INTEGER, down_count INTEGER,
        PRIMARY KEY(code, date));
    """)
    # 三板块都缺2012-2018段: BK0001(2018起) BK0002(2019-12起) BK0003(无数据)
    for d in ("2018-01-02", "2018-01-03"):
        conn.execute("INSERT INTO sector_daily VALUES('BK0001',?,1,1,1,1,1e6,NULL,NULL)", (d,))
    for d in ("2019-12-30", "2019-12-31"):
        conn.execute("INSERT INTO sector_daily VALUES('BK0002',?,1,1,1,1,1e6,NULL,NULL)", (d,))
    for c in ("BK0001", "BK0002", "BK0003"):
        conn.execute("INSERT INTO sectors VALUES(?,'x','industry',1,NULL,NULL,NULL)", (c,))
    conn.commit()
    conn.close()

    import pandas as pd

    def fake_kline(code, beg="19900101", **kw):
        assert beg == "20120101"                      # beg 直通
        days = ["2012-06-{dd:02d}".format(dd=dd) for dd in (11, 12, 13)]
        return pd.DataFrame({"date": days, "open": [1.0] * 3, "close": [1.0] * 3,
                             "high": [1.0] * 3, "low": [1.0] * 3, "amount": [1e6] * 3})

    monkeypatch.setattr(FD.em_api, "sector_kline", fake_kline)
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(p)))
    monkeypatch.setattr(time, "sleep", lambda s: None)   # 提速(直接patch time模块)
    return {"path": p, "monkeypatch": monkeypatch}


def test_backfill_sector_history_fills_earlier_segment(env):
    rc = FD.backfill_sector_history("20120101")
    assert rc is True
    conn = sqlite3.connect(str(env["path"]))
    # 各板块新增2012-06段3行: BK0001/2 原2行→5, BK0003 原无→3; 最早均前移
    for c, want_n in (("BK0001", 5), ("BK0002", 5), ("BK0003", 3)):
        n, lo = conn.execute(
            "SELECT COUNT(*), MIN(date) FROM sector_daily WHERE code=?", (c,)).fetchone()
        assert n == want_n and lo == "2012-06-11", f"{c}: {n}行 最早{lo}"
    conn.close()


def test_backfill_sector_history_idempotent(env):
    """二跑会重复请求(无法预知源下限)但 upsert 幂等, 行数不增"""
    FD.backfill_sector_history("20120101")
    FD.backfill_sector_history("20120101")
    conn = sqlite3.connect(str(env["path"]))
    total = conn.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0]
    conn.close()
    assert total == 13                                 # (2+3)×3板块 不重复


def test_backfill_sector_history_skip_when_covered(env):
    """板块最早日 ≤ start → 跳过请求(零调用)"""
    calls = []
    import pandas as pd
    conn = sqlite3.connect(str(env["path"]))
    conn.execute("INSERT INTO sector_daily VALUES('BK0003','2019-06-03',1,1,1,1,1e6,NULL,NULL)")
    conn.commit()
    conn.close()
    env["monkeypatch"].setattr(FD.em_api, "sector_kline",
                               lambda code, beg="19900101", **kw: calls.append(code) or pd.DataFrame())
    rc = FD.backfill_sector_history("20200101")       # 三板块最早日均 ≤ 2020-01-01
    assert rc is True and calls == []


def test_backfill_sector_history_fastfail_on_network(env, capsys):
    def boom(code, beg="19900101", **kw):
        raise ConnectionError("RST")
    env["monkeypatch"].setattr(FD.em_api, "sector_kline", boom)
    conn = sqlite3.connect(str(env["path"]))          # 补足≥5个待补板块触发err_streak
    for i in range(5):
        conn.execute("INSERT INTO sectors VALUES(?,'x','industry',1,NULL,NULL,NULL)",
                     (f"BK01{i:02d}",))
    conn.commit()
    conn.close()
    FD.backfill_sector_history("20120101")
    out = capsys.readouterr().out
    assert "连续5次异常" in out


def test_registry_entry_and_delta(env):
    """run_backfill 注册表联动: sector_history 的逐日增量度量"""
    r = FD.run_backfill("sector_history", "20120101")
    assert r["status"] == "ok" and r["rows_written"] == 9      # 三板块各3行
    assert [d["date"] for d in r["days"]] == ["2012-06-11", "2012-06-12", "2012-06-13"]
    assert all(d["rows"] == 3 for d in r["days"])               # 每日三板块各1行
    assert r["date_from"] == "2012-06-11"
