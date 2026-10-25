# -*- coding: utf-8 -*-
"""sectors 增量日线分支: 限流熔断/进度输出/streak重置 (用户卡住事故的修复回归)"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import sectors as S  # noqa: E402


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
      CREATE TABLE sector_alias(name TEXT PRIMARY KEY, code TEXT);
    """)
    for i in range(12):                     # 12个启用板块 → 足以触发10连断
        c = f"BK{i:04d}"
        conn.execute("INSERT INTO sectors VALUES(?,'x','industry',1,NULL,NULL,NULL)", (c,))
        conn.execute("INSERT INTO sector_daily VALUES(?, '2026-10-17',1,1,1,1,1,NULL,NULL)", (c,))
    conn.commit()
    conn.close()

    # 隔离依赖: 清单/资金流/重映射/聚合全部 no-op
    monkeypatch.setattr(S, "connect", lambda: sqlite3.connect(str(p)))
    monkeypatch.setattr(S, "refresh_lists", lambda conn: {})
    monkeypatch.setattr(S, "refresh_fundflow", lambda conn, force=False: None)
    monkeypatch.setattr(S, "remap_pending", lambda conn: None)
    monkeypatch.setattr(S, "agg_today", lambda conn: None)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    # D1-12 G1: 本组测试针对东财兜底路径 — 强制dc主源失败(亦防误连真实库)
    monkeypatch.setattr(S, "_update_via_dc", lambda **k: False)
    return {"path": p, "monkeypatch": monkeypatch}


def test_incremental_fastfail_on_outage(env, capsys):
    """push2his不可达: 每~15-40s/板块的超时爬行→连续10次熔断跳出(原版无熔断致'卡住')"""
    calls = []

    def boom(code, beg="19900101"):
        calls.append(code)
        raise ConnectionError("RemoteDisconnected")
    env["monkeypatch"].setattr(S.em_api, "sector_kline", boom)
    S.update_daily(conn=None, auto_backfill=False)
    out = capsys.readouterr().out
    assert len(calls) == 10                        # 第10次即断, 不再爬300个
    assert "连续10次失败" in out and "跳过本次日线增量" in out


def test_incremental_streak_reset_on_success(env, capsys):
    """失败-成功交替不误断(熔断只认连续)"""
    import pandas as pd
    n = {"i": 0}

    def flaky(code, beg="19900101"):
        n["i"] += 1
        if n["i"] % 2 == 1:
            raise ConnectionError("RST")
        return pd.DataFrame({"date": ["2026-10-17"], "open": [1.0], "close": [1.0],
                             "high": [1.0], "low": [1.0], "amount": [1.0]})
    env["monkeypatch"].setattr(S.em_api, "sector_kline", flaky)
    S.update_daily(conn=None, auto_backfill=False)
    out = capsys.readouterr().out
    assert "连续10次失败" not in out               # 交替失败不触发熔断
    assert "增量日线(东财兜底): 6/12" in out                 # 6个成功(偶数次调用)


def test_incremental_progress_printed(env, capsys):
    """即使全失败也有熔断消息与结束汇总, 不再'看起来卡住'"""
    import pandas as pd

    def ok(code, beg="19900101"):
        return pd.DataFrame()                     # 空数据也计fails → 10次熔断
    env["monkeypatch"].setattr(S.em_api, "sector_kline", ok)
    S.update_daily(conn=None, auto_backfill=False)
    out = capsys.readouterr().out
    assert "连续10次失败" in out                  # 空数据连续也熔断
    assert "增量日线(东财兜底): 0/12" in out                # 结束汇总必输出(不再静默爬行)
