# -*- coding: utf-8 -*-
"""D1-7 测试: staging 原子切换 — swap成功/校验不过/写入只进staging(崩溃安全)/全链"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import db as DB  # noqa: E402


@pytest.fixture()
def tdb(tmp_path):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE sentiment(date TEXT PRIMARY KEY, score REAL, stage TEXT, components TEXT)")
    for d in ("2026-01-05", "2026-01-06", "2026-01-07"):
        conn.execute("INSERT INTO sentiment VALUES(?,?,?,?)", (d, 1.0, "x", None))
    conn.commit()
    yield conn, p
    conn.close()


def test_atomic_swap_success(tdb):
    conn, _ = tdb
    st = DB.make_staging(conn, "sentiment")
    for d in ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"):
        conn.execute(f"INSERT INTO {st} VALUES(?,?,?,?)", (d, 2.0, "y", None))
    conn.commit()
    DB.atomic_swap(conn, "sentiment")
    n = conn.execute("SELECT COUNT(*), MAX(date) FROM sentiment").fetchone()
    assert n == (4, "2026-01-08")
    # 旧表与staging均不存在(已RENAME+DROP)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='sentiment_old'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='sentiment_staging'").fetchone()[0] == 0


def test_atomic_swap_validation_too_few_rows(tdb):
    conn, _ = tdb
    st = DB.make_staging(conn, "sentiment")
    conn.execute(f"INSERT INTO {st} VALUES('2026-01-07',1,'x',NULL)")   # 1 < 3×0.95
    conn.commit()
    with pytest.raises(RuntimeError, match="行数"):
        DB.atomic_swap(conn, "sentiment")
    assert conn.execute("SELECT COUNT(*) FROM sentiment").fetchone()[0] == 3   # 旧表完好


def test_atomic_swap_validation_date_regression(tdb):
    conn, _ = tdb
    st = DB.make_staging(conn, "sentiment")
    for d in ("2026-01-05", "2026-01-06"):                                # 最新日回退
        conn.execute(f"INSERT INTO {st} VALUES(?,1,'x',NULL)", (d,))
    conn.commit()
    with pytest.raises(RuntimeError, match="回退"):
        DB.atomic_swap(conn, "sentiment")
    assert conn.execute("SELECT MAX(date) FROM sentiment").fetchone()[0] == "2026-01-07"


def test_atomic_swap_empty_staging(tdb):
    conn, _ = tdb
    DB.make_staging(conn, "sentiment")
    with pytest.raises(RuntimeError, match="为空"):
        DB.atomic_swap(conn, "sentiment")


def test_score_save_to_db_staging_path(tdb, monkeypatch):
    """写入只进staging(崩溃安全本质): 重放NaT事故——date含NaT致strftime崩溃 → 正式表零变化"""
    import pandas as pd
    import score as SC
    conn, _ = tdb
    monkeypatch.setattr(SC, "connect", lambda: conn)
    score_df = pd.DataFrame({
        "date": list(pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-08"])) + [pd.NaT],
        "score": [10.0, 20.0, 30.0, 40.0],                  # NaT排序在末位且score有效
        "stage": ["a", "b", "d", "e"]})
    comp = pd.DataFrame({"rsi": [1.0, 2.0, 3.0, 4.0]})
    with pytest.raises(ValueError):                          # NaT.strftime → ValueError(即当年事故)
        SC.save_to_db(conn, score_df, comp)
    assert conn.execute("SELECT COUNT(*) FROM sentiment").fetchone()[0] == 3   # 旧表零变化
    assert conn.execute("SELECT MAX(date) FROM sentiment").fetchone()[0] == "2026-01-07"
    # staging 残留(未切换), 下次 make_staging 自动覆盖


def test_score_save_to_db_success_swap(tdb, monkeypatch):
    import pandas as pd
    import score as SC
    conn, _ = tdb
    score_df = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]),
        "score": [10.0, 20.0, float("nan"), 30.0],
        "stage": ["a", "b", "c", "d"]})
    comp = pd.DataFrame({"rsi": [1.0, 2.0, 3.0, 4.0]})
    SC.save_to_db(conn, score_df, comp)
    n, mx = conn.execute("SELECT COUNT(*), MAX(date) FROM sentiment").fetchone()
    assert (n, mx) == (3, "2026-01-08")                    # NaN行跳过→3行但日期更新
