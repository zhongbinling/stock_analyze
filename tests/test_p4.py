# -*- coding: utf-8 -*-
"""P4 测试: sw_bars 幂等/熔断/列映射/ts_code参数 + backup raw资产策略"""
import sqlite3
import sys
import time
import zipfile
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import backup as BK  # noqa: E402
import sw_bars as SB  # noqa: E402
import tushare_api as T  # noqa: E402


@pytest.fixture()
def bars_env(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE index_daily(code TEXT, date TEXT, close REAL);
      CREATE TABLE sw_components(index_code TEXT, level TEXT, industry_name TEXT,
        con_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT);
      CREATE TABLE sw_daily(index_code TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, amount REAL, pct_chg REAL, PRIMARY KEY(index_code, date));
    """)
    conn.execute("INSERT INTO index_daily VALUES('1.000001','2026-10-19',3000)")
    conn.execute("INSERT INTO sw_components VALUES('801010.SI','L1','农林牧渔','000001.SZ','19900101',NULL,'Y')")
    for k in range(6):                             # 6个无数据指数 → 熔断可达5连
        conn.execute("INSERT INTO sw_components VALUES(?,'L1','无数据','00000%d.SZ','19900101',NULL,'Y')" % (100+k), (f"9999{k:02d}.SI",))
    conn.commit()
    conn.commit()
    conn.close()

    import pandas as pd

    def fake_call(api, fields="", **kw):
        assert api == "sw_daily" and "ts_code" in kw, f"必须用ts_code参数: {kw}"
        if kw["ts_code"] != "801010.SI":
            return pd.DataFrame()                    # 其余指数无官方数据
        return pd.DataFrame([
            {"ts_code": kw["ts_code"], "trade_date": "20120801", "name": "农林牧渔",
             "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "change": 0.5,
             "pct_change": 0.5, "vol": 100, "amount": 1e6, "pe": 1, "pb": 1,
             "float_mv": 1, "total_mv": 1},
            {"ts_code": kw["ts_code"], "trade_date": "20261019", "name": "农林牧渔",
             "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.6, "change": 0.1,
             "pct_change": 0.1, "vol": 100, "amount": 1e6, "pe": 1, "pb": 1,
             "float_mv": 1, "total_mv": 1},
        ])
    monkeypatch.setattr(T, "call", fake_call)
    monkeypatch.setattr(SB, "connect", lambda: sqlite3.connect(str(p)))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return p


def test_bars_backfill_and_idempotent(bars_env):
    assert SB.backfill("19990101") is True
    conn = sqlite3.connect(str(bars_env))
    rows = conn.execute("SELECT date, close, pct_chg FROM sw_daily WHERE index_code='801010.SI' "
                        "ORDER BY date").fetchall()
    conn.close()
    assert rows == [("2012-08-01", 1.5, 0.5), ("2026-10-19", 1.6, 0.1)]   # ISO+列映射
    # 幂等: 有数据指数已最新 → 跳过; 无数据指数已探明 → 跳过 → 零调用
    calls = []
    orig = T.call
    import pandas as pd
    T.call = lambda api, fields="", **kw: calls.append(kw) or pd.DataFrame()
    try:
        SB.backfill("19990101")
    finally:
        T.call = orig
    assert calls == []


def test_bars_fastfail(bars_env, monkeypatch):
    def boom(api, fields="", **kw):
        raise ConnectionError("RST")
    monkeypatch.setattr(T, "call", boom)
    assert SB.backfill("19990101") is False


# ── backup raw 资产策略 ──

@pytest.fixture()
def bk_env(tmp_path, monkeypatch):
    core = tmp_path / "core"
    (core / "data").mkdir(parents=True)
    raw = core / "data" / "raw_daily.db"
    conn = sqlite3.connect(str(raw))
    conn.execute("CREATE TABLE daily(x INTEGER)")
    conn.execute("INSERT INTO daily VALUES(1)")
    conn.commit()
    conn.close()
    bdir = tmp_path / "backups"
    monkeypatch.setattr(BK, "ASSETS", {"raw_daily": ("sqlite", raw)})
    monkeypatch.setattr(BK, "BACKUP_DIR", bdir)
    monkeypatch.setattr(BK, "MANIFEST", bdir / "manifest.json")
    monkeypatch.setattr(BK, "LOG_FILE", bdir / "backup.log")
    return {"raw": raw, "bdir": bdir, "monkeypatch": monkeypatch}


def test_raw_skipped_on_daily_snapshot(bk_env):
    assert BK.snapshot("raw_daily", "daily") is None          # 策略: 仅weekly/manual
    assert not (bk_env["bdir"] / "raw_daily_x.db.z").exists()


def test_raw_weekly_compressed(bk_env):
    e = BK.snapshot("raw_daily", "weekly")
    assert e is not None
    # D1-10 B1/B3: weekly_{ts}.zip 内含 raw_daily.db.z(原名义件)
    root = e["file"].split("/")[0]
    zp = bk_env["bdir"] / root
    assert zp.suffix == ".zip" and zp.exists()
    with zipfile.ZipFile(zp) as zf:
        assert "raw_daily.db.z" in zf.namelist()
        raw = zf.read("raw_daily.db.z")
    assert zlib.decompress(raw)[:15] == b"SQLite format 3"   # 可解压且是库


def test_raw_keep_rolling(bk_env, monkeypatch):
    import datetime as _dt
    real = _dt.datetime
    state = {"n": 0}

    class FakeDT(real):                              # 递增假时钟: 快照名互不相同
        @classmethod
        def now(cls, tz=None):
            state["n"] += 60
            return real(2026, 1, 1) + _dt.timedelta(seconds=state["n"])
    monkeypatch.setattr(BK.dt, "datetime", FakeDT)
    for i in range(6):                               # 6次周备(内容变化)
        conn = sqlite3.connect(str(bk_env["raw"]))
        conn.execute("INSERT INTO daily VALUES(?)", (i + 10,))
        conn.commit()
        conn.close()
        BK.snapshot("raw_daily", "weekly")
    BK.prune()
    zips = [p for p in bk_env["bdir"].glob("*.zip")
            if "raw_daily.db.z" in zipfile.ZipFile(p).namelist()]
    assert len(zips) == 4                             # keep=4(D1-10 B1: 目录改zip)


def test_raw_pre_restore_snapshot_allowed(bk_env):
    """pre-restore 对 raw 必须兜底(review修复: 曾被policy跳过)"""
    e = BK.snapshot("raw_daily", "pre-restore")
    assert e is not None and e["file"].endswith("raw_daily.db.z")
    # pre-calc 仍排除(避免每次全量重算压缩2.5GB)
    assert BK.snapshot("raw_daily", "pre-calc") is None


def test_raw_restore_decompress(bk_env):
    e = BK.snapshot("raw_daily", "weekly")
    conn = sqlite3.connect(str(bk_env["raw"]))
    conn.execute("DELETE FROM daily")
    conn.commit()
    conn.close()
    assert BK.restore(e["file"], yes=True) is True
    conn = sqlite3.connect(str(bk_env["raw"]))
    assert conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 1
    conn.close()
