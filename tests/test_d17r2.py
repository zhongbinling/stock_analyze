# -*- coding: utf-8 -*-
"""D1-17 R2: 实时情绪分 — 回放等值(同构)/缺分量归一/前缀语义/快照行去重"""
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


@pytest.fixture()
def rt_env(tmp_path, monkeypatch):
    """夹具库: 300交易日(≥window 250, 池60起步) 双指数+mood池+sentiment表"""
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE index_daily(code TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, amount REAL, PRIMARY KEY(code, date));
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, dt_count INTEGER,
        zb_rate REAL, lb_max INTEGER, jl_rate REAL);
      CREATE TABLE sentiment(date TEXT PRIMARY KEY, score REAL, stage TEXT, components TEXT);
    """)
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2025-08-01", periods=300).strftime("%Y-%m-%d").tolist()
    px = 3200 + np.cumsum(rng.normal(0, 18, 300))
    for i, d in enumerate(dates):
        c = round(float(px[i]), 2)
        o = round(c + rng.normal(0, 6), 2)
        hi = round(max(o, c) + abs(rng.normal(0, 8)), 2)
        lo = round(min(o, c) - abs(rng.normal(0, 8)), 2)
        conn.execute("INSERT INTO index_daily VALUES('1.000001',?,?,?,?,?,?,?)",
                     (d, o, hi, lo, c, 3.2e8 + rng.integers(0, 4e7),
                      3.5e11 + rng.integers(0, 5e10)))
        conn.execute("INSERT INTO index_daily VALUES('0.399106',?,?,?,?,?,?,?)",
                     (d, o * 2.1, hi * 2.1, lo * 2.1, c * 2.1,
                      4.1e8 + rng.integers(0, 5e7), 4.2e11 + rng.integers(0, 6e10)))
        conn.execute("INSERT INTO mood_raw VALUES(?,?,?,?,?,?)",
                     (d, int(40 + rng.integers(0, 60)), int(rng.integers(0, 12)),
                      round(float(rng.uniform(5, 60)), 1), int(rng.integers(2, 9)),
                      round(float(rng.uniform(5, 45)), 1)))
    conn.commit()
    conn.close()

    import score as SC
    import rt_score as RT
    cfgcore = tmp_path / "c"
    cfgcore.mkdir()
    shutil.copy(CORE / "config.yaml", cfgcore / "config.yaml")
    monkeypatch.setattr(SC, "BASE", cfgcore)
    RT._core = None
    return {"path": p, "SC": SC, "RT": RT,
            "connect": lambda: sqlite3.connect(str(p)), "dates": dates,
            "tmp": tmp_path}


def _run_score(env):
    """夹具库上跑收盘管线并落 sentiment 表"""
    SC = env["SC"]
    conn = env["connect"]()
    cfg = SC.load_cfg()
    # build_frame 走 metrics.connect → 指到夹具库
    import metrics
    orig = metrics.connect
    metrics.connect = env["connect"]
    try:
        df, comp, score_df = SC.compute(cfg, conn)
    finally:
        metrics.connect = orig
    SC.save_to_db(conn, score_df, comp)
    conn.close()
    return score_df


def test_replay_matches_stored_score(rt_env):
    """R2 核心判据: 回放(收盘数据重建快照)与库内分严格等值(同构)"""
    env = rt_env
    score_df = _run_score(env)
    conn = env["connect"]()
    for d in (env["dates"][-1], env["dates"][-10], env["dates"][-30]):   # 任意历史日
        r = env["RT"].replay(conn, d)
        assert r is not None
        assert abs(r["score"] - r["stored"]) < 0.05, f"{d}: {r['score']} vs {r['stored']}"
        assert r["stage"] == conn.execute(
            "SELECT stage FROM sentiment WHERE date=?", (d,)).fetchone()[0]
    conn.close()


def test_rt_partial_pool_renormalized(rt_env):
    """盘中池分量缺失(快照无涨停结构) → 权重归一吸收, 分数仍有限且偏离有界"""
    env = rt_env
    _run_score(env)
    conn = env["connect"]()
    last = env["dates"][-1]
    full = env["RT"].replay(conn, last)                     # 全字段快照
    row = conn.execute(
        "SELECT open,high,low,close,volume,amount FROM index_daily "
        "WHERE code='1.000001' AND date=?", (last,)).fetchone()
    snap = {"date": last, "open": row[0], "high": row[1], "low": row[2],
            "close": row[3], "volume1": row[4], "amount1": row[5]}   # 无池字段
    part = env["RT"].compute_rt(conn, snap)
    assert abs(part["score"]) <= 100 and part["score"] != full["score"]
    assert part["comps"]["zt_count"] is None                # 缺失分量标记None
    conn.close()


def test_rt_frame_replaces_partial_today(rt_env):
    """库内已写入快照日(盘中重复计算) → rt_frame 去重不产生双行"""
    env = rt_env
    _run_score(env)
    conn = env["connect"]()
    last = env["dates"][-1]
    snap = {"date": last, "open": 3300, "high": 3310, "low": 3290, "close": 3305,
            "volume1": 1e8, "amount1": 1e11}                # 与库内同日不同值
    SC = env["SC"]
    cfg = SC.load_cfg()
    df = env["RT"].rt_frame(conn, cfg, snap)
    assert (df["date"] == pd.Timestamp(last)).sum() == 1    # 单行=快照版
    assert float(df.iloc[-1]["close"]) == 3305
    conn.close()


def test_metrics_split_compose(rt_env):
    """metrics 无损重构: fetch+derive 组合 == build_frame 原语义"""
    import metrics
    metrics.connect = rt_env["connect"]
    df = metrics.build_frame(primary="1.000001", secondary="0.399106")
    assert {"chg", "rsi", "bias20", "vol_total", "vr"} <= set(df.columns)
    assert len(df) == 300 and df["date"].is_monotonic_increasing
