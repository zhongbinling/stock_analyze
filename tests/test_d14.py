# -*- coding: utf-8 -*-
"""D1-14 H1: 指数估值链 — 回补/日增/守卫/注册接线"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import fetch_val as FV  # noqa: E402


@pytest.fixture()
def val_env(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    monkeypatch.setattr(FV, "connect", lambda: sqlite3.connect(str(p)))
    return {"path": p, "FV": FV}


def _hist_df():
    return pd.DataFrame([
        ("20130601", 9.5, 1.2),      # 早于start → 滤除
        ("20140620", 10.0, 1.3),
        ("20261023", 13.9, 1.36),
    ], columns=["trade_date", "pe", "pb"])


def test_run_backfills_per_code(val_env, monkeypatch):
    """按代码回补: start前滤除, 白名单7指数, 幂等行数正确"""
    seen = []

    def fake(api, fields="", **kw):
        seen.append(kw.get("ts_code"))
        return _hist_df()
    monkeypatch.setattr(FV.T, "has_token", lambda: True)
    monkeypatch.setattr(FV.T, "call", fake)
    assert FV.run("20140601") is True
    assert sorted(seen) == sorted(FV.VAL_INDEXES)      # 7指数逐个查
    c = sqlite3.connect(str(val_env["path"]))
    assert c.execute("SELECT COUNT(*) FROM index_val_daily").fetchone()[0] == 14  # 7×2日
    assert c.execute("SELECT COUNT(*) FROM index_val_daily "
                     "WHERE date='2013-06-01'").fetchone()[0] == 0
    row = c.execute("SELECT name FROM index_val_daily WHERE code='000300.SH' LIMIT 1").fetchone()
    assert row[0] == "沪深300"
    c.close()


def test_run_rejects_bad_pe(val_env, monkeypatch):
    """守卫: pe越界(负值/超500)整批拒绝, 表零污染"""
    bad = pd.DataFrame([("20261023", -5.0, 1.0)], columns=["trade_date", "pe", "pb"])
    monkeypatch.setattr(FV.T, "has_token", lambda: True)
    monkeypatch.setattr(FV.T, "call", lambda api, fields="", **kw: bad)
    assert FV.run("20140601") is False
    c = sqlite3.connect(str(val_env["path"]))
    assert c.execute("SELECT COUNT(*) FROM index_val_daily").fetchone()[0] == 0
    c.close()


def test_refresh_daily_batch(val_env, monkeypatch):
    """日增: 按trade_date一次拿全指数, 白名单过滤, 当日一批"""
    daily = pd.DataFrame([
        ("000300.SH", 13.9, 1.36), ("000016.SH", 11.7, 1.24),
        ("399005.SZ", 26.6, 2.58),                      # 不在白名单 → 滤除
    ], columns=["ts_code", "pe", "pb"])
    monkeypatch.setattr(FV.T, "call", lambda api, fields="", **kw: daily)
    assert FV.refresh(conn=sqlite3.connect(str(val_env["path"]))) is True
    c = sqlite3.connect(str(val_env["path"]))
    n, codes = c.execute("SELECT COUNT(*), COUNT(DISTINCT code) FROM index_val_daily").fetchone()
    assert (n, codes) == (2, 2)
    c.close()


def test_index_val_registered():
    """注册: BACKFILLS/jobs/资产表行/体检表"""
    import fetch_daily as FD2
    assert "index_val" in FD2.BACKFILLS
    from server import jobs
    assert "index_val" in jobs.BACKFILL_DATASETS
    assert jobs.BACKFILL_DEFAULT_START["index_val"] == "20140601"
    from server import market
    assert any(s[0] == "index_val" for s in market._ASSET_DATE_TABLES)
    assert market._MIN_ROW_RATIO["index_val_daily"] == (0.6, 0.3)
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "index_val" in html and "自2014-06起" in html
    # 更新链挂载(fetch_daily主链)
    src = Path(CORE / "fetch_daily.py").read_text(encoding="utf-8")
    assert "fetch_val.refresh()" in src
