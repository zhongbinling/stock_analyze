# -*- coding: utf-8 -*-
"""温度引擎纯函数测试 (core/sector.py)"""
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

import sector as S


# ── stage_of: STAGES 边界 ──
@pytest.mark.parametrize("score,want", [
    (-100, "极度冰点"), (-80, "极度冰点"), (-79.9, "冰点/恐慌"), (-60, "冰点/恐慌"),
    (-30, "偏冷/退潮"), (-10, "中性偏弱"), (10, "中性"), (30, "中性偏强"),
    (60, "偏热/发酵"), (80, "狂热/高潮"), (80.1, "极度狂热"), (100, "极度狂热"),
])
def test_stage_of_boundaries(score, want):
    assert S.stage_of(score) == want


# ── _pct: 滚动分位 ──
def test_pct_known_values():
    up = pd.Series(range(1, 301), dtype=float)      # 严格递增
    p = S._pct(up, 250)
    assert pd.isna(p.iloc[248])                     # 窗口未满(min_periods=250)
    assert p.iloc[249] == 1.0                       # 当前值=窗口最大 → 1
    dn = pd.Series(range(300, 0, -1), dtype=float)  # 严格递减
    p2 = S._pct(dn, 250)
    assert p2.iloc[249] == 0.0                      # 当前值=窗口最小 → 0


def test_pct_constant_series_is_nan():
    s = pd.Series([5.0] * 300)
    p = S._pct(s, 250)
    assert np.isnan(p.iloc[260])                    # 0/0 → NaN


# ── _asym: 非对称方向化(V2.1 口径) ──
def test_asym_four_cases():
    sp = pd.Series([10.0, -5.0, -5.0, -5.0])
    chg = pd.Series([2.0, 2.0, -1.0, 3.0])
    out = S._asym(sp, chg)
    assert out.iloc[0] == 10.0      # 放量×涨 → 正
    assert out.iloc[1] == -2.5      # 缩量×涨 → 减半
    assert out.iloc[2] == -5.0      # 缩量×跌 → 全负
    assert out.iloc[3] == -2.5      # 缩量×涨 → 减半


# ── calc_one: 合成行情 ──
WEIGHTS = {"rsi": 0.1, "chg": 0.1, "gap": 0.05, "kr": 0.05, "vol_dir": 0.1,
           "vr": 0.1, "bias20": 0.05, "bias60": 0.05, "pos": 0.1, "amp": 0.1,
           "zt": 0.1, "zt_share": 0.05, "ud_ratio": 0.05}


def _mk_df(n=320, drift=1.0, seed=7):
    rng = np.random.default_rng(seed)
    close = np.cumsum(rng.normal(drift, 0.5, n)) + 100
    prev = np.roll(close, 1)
    prev[0] = close[0]
    return pd.DataFrame({
        "date": [f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)],
        "code": "BKTEST",
        "open": prev, "close": close, "high": close + 1, "low": close - 1,
        "amount": 1e6 + np.arange(n) * 1e4,
        "up_count": 80, "down_count": 20, "zt_count": 5,
    })


def test_calc_one_rising_positive_falling_negative():
    up = S.calc_one(_mk_df(drift=1.0), None, pd.Series([100.0] * 320), WEIGHTS)
    dn = S.calc_one(_mk_df(drift=-1.0), None, pd.Series([100.0] * 320), WEIGHTS)
    assert up["score"].iloc[-1] > 0
    assert dn["score"].iloc[-1] < 0


def test_calc_one_clamped_and_warmup():
    out = S.calc_one(_mk_df(drift=3.0), None, pd.Series([100.0] * 320), WEIGHTS)
    valid = out["score"].dropna()
    assert valid.between(-100, 100).all()          # 限幅
    assert valid.index.size >= 60                   # 320行 - 250窗口 ≈ 71 有效
    assert out["score"].first_valid_index() >= 240  # 250日分位窗口 → 头部NaN


def test_calc_one_components_json_valid():
    out = S.calc_one(_mk_df(), None, pd.Series([100.0] * 320), WEIGHTS)
    row = out.iloc[-1]
    c = json.loads(row["components"])
    assert isinstance(c, dict) and len(c) > 0
    assert all(v is not None for v in c.values())   # NaN键已剔除


def test_calc_one_weight_renormalization():
    """缺一个分量权重 → 其余权重归一化, 仍出分"""
    w = dict(WEIGHTS)
    w.pop("vr")
    out = S.calc_one(_mk_df(), None, pd.Series([100.0] * 320), w)
    assert out["score"].iloc[-1] == out["score"].iloc[-1]  # 非 NaN


def test_calc_one_output_columns():
    out = S.calc_one(_mk_df(n=300), None, pd.Series([100.0] * 300), WEIGHTS)
    assert list(out.columns) == ["date", "code", "score", "components"]
    assert (out["code"] == "BKTEST").all()


# ── _calc_sw1 / _calc_sw2: NaN 行不入库 ──
def _mem_conn():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sector_scores(date TEXT, code TEXT, score REAL, "
                 "rank_pct REAL, stage TEXT, components TEXT, PRIMARY KEY(date, code))")
    conn.execute("CREATE TABLE sw1_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT, PRIMARY KEY(bk_code))")
    conn.execute("CREATE TABLE sw2_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT, sw2_name TEXT, PRIMARY KEY(bk_code))")
    conn.executemany("INSERT INTO sw1_map VALUES(?,?,?)",
                     [("BK0001", "半导体", "信息技术"), ("BK0002", "白酒", "消费"),
                      ("BK0003", "证券", "金融")])
    conn.executemany("INSERT INTO sw2_map VALUES(?,?,?,?)",
                     [("BK0001", "半导体", "信息技术", "半导体"),
                      ("BK0002", "白酒", "消费", "白酒")])
    return conn


def test_calc_sw1_skips_nan_rows():
    conn = _mem_conn()
    allsc = pd.DataFrame({
        "date": ["2026-01-01"] * 3,
        "code": ["BK0001", "BK0002", "BK0003"],
        "score": [50.0, np.nan, -50.0],
    })
    S._calc_sw1(conn, allsc)
    rows = conn.execute("SELECT code, score FROM sector_scores").fetchall()
    assert all(r[1] is not None for r in rows)      # NaN行未入库
    # 消费组唯一成分NaN → 整组NaN不入库; 信息技术(50) + 金融(-50)
    assert sorted(rows) == [("SW1_信息技术", 50.0), ("SW1_金融", -50.0)]
    conn.close()


def test_calc_sw2_skips_nan_rows():
    conn = _mem_conn()
    allsc = pd.DataFrame({
        "date": ["2026-01-01"] * 3,
        "code": ["BK0001", "BK0002", "BK0003"],
        "score": [np.nan, 30.0, -20.0],
    })
    S._calc_sw2(conn, allsc)
    rows = conn.execute("SELECT code FROM sector_scores").fetchall()
    assert len(rows) == 1 and rows[0][0] == "SW2_白酒"  # BK0003 无 sw2 映射, BK0001 NaN
    conn.close()


def test_calc_sw_temperature_filters_nan_and_ranks(tmp_path, monkeypatch):
    """P5review: SW行预热NaN不入库 + rank_pct按同层级行业内百分位"""
    import pandas as pd
    import sw_pool  # noqa: F401
    p = tmp_path / "t.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE sector_scores(date TEXT, code TEXT, score REAL, rank_pct REAL,
        stage TEXT, components TEXT, PRIMARY KEY(date, code));
      CREATE TABLE sw_components(index_code TEXT, level TEXT, industry_name TEXT,
        con_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT);
      CREATE TABLE mood_raw(date TEXT, zt_count INTEGER);
      CREATE TABLE sw_daily(index_code TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, amount REAL);
      CREATE TABLE sw_pool(level TEXT, index_code TEXT, date TEXT, zt_count INTEGER);
      CREATE TABLE sw_breadth(level TEXT, index_code TEXT, date TEXT, up INTEGER, down INTEGER);
      CREATE TABLE sw_fflow(level TEXT, index_code TEXT, date TEXT, main_net REAL);
    """)
    # 两个L1指数 × 320日(前250日NaN预热)
    for k, ic in enumerate(("801010.SI", "801120.SI")):
        conn.execute("INSERT INTO sw_components VALUES(?, 'L1', ?, '000001.SZ','19900101',NULL,'Y')",
                     (ic, "农林牧渔" if k == 0 else "食品饮料"))
        for i in range(320):
            d = f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}"
            conn.execute("INSERT OR REPLACE INTO sw_daily VALUES(?,?,?,?,?,?,?,?)",
                         (ic, d, 1, 2, 0.5, 1.5 + i * 0.001 * (1 if k == 0 else -1), 100, 1e6))
    conn.execute("INSERT INTO mood_raw VALUES('2025-06-01', 50)")
    conn.commit()
    monkeypatch.setattr(S, "connect", lambda: sqlite3.connect(str(p)))
    S._calc_sw_temperature(conn, table="sector_scores")
    n_null = conn.execute(
        "SELECT COUNT(*) FROM sector_scores WHERE score IS NULL").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM sector_scores").fetchone()[0]
    assert n_null == 0 and n > 0                      # 预热NaN过滤
    # rank_pct: 同层级行业内百分位(两行业分列0~100)
    r = conn.execute("SELECT MIN(rank_pct), MAX(rank_pct) FROM sector_scores").fetchone()
    assert r[0] is not None and 0 <= r[0] <= 100 and 0 < r[1] <= 100
    conn.close()
