# -*- coding: utf-8 -*-
"""测试T2: backtest空白补齐 + rt_series分钟边界 + fetch_*断点续传(失败中断/跳过幂等)"""
import shutil
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


# ══════════ ① backtest(原完全空白) ══════════

def _bt_df(n=150):
    """合成score_df: date/score/v1_score/close — fwd5收益随score单调(构造IC=+1)"""
    dates = pd.bdate_range("2025-01-01", periods=n)
    score = np.arange(n, dtype=float)
    f = 0.001 * (np.arange(n, dtype=float) / 10.0) ** 2   # close=exp(f): 5日前瞻收益严格增
    return pd.DataFrame({"date": dates, "score": score,
                         "v1_score": score * 0.5, "close": np.exp(f)})


def test_nearest_match_and_tolerance():
    import backtest as BT
    df = _bt_df(10)
    i = BT.nearest(df, str(df["date"].iloc[3].date()))     # 精确命中
    assert df["date"].iloc[i] == df["date"].iloc[3]
    assert BT.nearest(df, "1990-01-01", tol=3) is None     # 超容差


def test_run_anchors_counts_hits(capsys):
    import backtest as BT
    # 日期须覆盖锚点年段(2015~2024): 默认分-1, 单个+1锚点翻正, 一锚点剔除窗口
    n = 2600
    df = pd.DataFrame({"date": pd.bdate_range("2015-01-01", periods=n),
                       "score": -1.0, "v1_score": -0.5, "close": 3000.0})
    df.loc[df["date"] == pd.Timestamp("2015-06-12"), "score"] = 30.0   # +1锚 → ✓
    df = df[~df["date"].between("2024-09-24", "2024-10-03")].reset_index(drop=True)
    # 期望: 06-12✓ 08-24✓ 01-04✓ 2018-12-24✓ 2020-07-09✗ 2024-02-05✓ 09-30无数据 10-08✗
    BT.run_anchors(df)
    out = capsys.readouterr().out
    assert "命中 5/7" in out and "无数据" in out


def test_run_ic_perfect_and_insufficient(capsys):
    import backtest as BT
    BT.run_ic(_bt_df(120), horizon=5)                      # 单调构造 → +1.000
    assert "+1.000" in capsys.readouterr().out
    BT.run_ic(_bt_df(50), horizon=5)                       # <60样本 → 跳过
    assert "样本不足" in capsys.readouterr().out


def test_run_quintiles_monotone_and_edges(capsys):
    import backtest as BT
    # 反向构造: fwd随score递减 → Q1(冰点)未来收益最高
    n = 150
    df = pd.DataFrame({"date": pd.bdate_range("2025-01-01", periods=n),
                       "score": np.arange(n, dtype=float),
                       "v1_score": np.arange(n, dtype=float) * 0.5,
                       "close": np.exp(-0.0005 * np.arange(n, dtype=float) ** 1.2)})
    BT.run_quintiles(df, horizon=5)
    out = capsys.readouterr().out
    assert "Q1冰点" in out and "Q5狂热" in out
    q1 = float(out.split("Q1冰点")[1].split("%")[0].strip().lstrip("+"))
    q5 = float(out.split("Q5狂热")[1].split("%")[0].strip().lstrip("+"))
    assert q1 > q5                                          # 冰点后收益 > 狂热后
    BT.run_quintiles(_bt_df(50), horizon=5)                 # <100 → 跳过
    assert "样本不足" in capsys.readouterr().out
    conc = _bt_df(150)
    conc["score"] = 1.0                                     # 分数全同 → qcut炸 → 优雅跳过
    BT.run_quintiles(conc, horizon=5)
    assert "过于集中" in capsys.readouterr().out


# ══════════ ② rt_score.rt_series 分钟序列边界 ══════════

@pytest.fixture()
def rt_env(tmp_path, monkeypatch):
    """300交易日双指数+mood池(d17r2同构, rt_series最小依赖)"""
    p = tmp_path / "s.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE index_daily(code TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, amount REAL, PRIMARY KEY(code, date));
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, dt_count INTEGER,
        zb_rate REAL, lb_max INTEGER, jl_rate REAL);
      CREATE TABLE sentiment(date TEXT PRIMARY KEY, score REAL, stage TEXT, components TEXT);
    """)
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2025-08-01", periods=300).strftime("%Y-%m-%d").tolist()
    px = 3200 + np.cumsum(rng.normal(0, 15, 300))
    for i, d in enumerate(dates):
        c = round(float(px[i]), 2)
        conn.execute("INSERT INTO index_daily VALUES('1.000001',?,?,?,?,?,?,?)",
                     (d, c - 5, c + 8, c - 8, c, 3.2e8, 3.5e11))
        conn.execute("INSERT INTO index_daily VALUES('0.399106',?,?,?,?,?,?,?)",
                     (d, c * 2, c * 2.1, c * 1.9, c * 2, 4.1e8, 4.2e11))
        conn.execute("INSERT INTO mood_raw VALUES(?,?,?,?,?,?)",
                     (d, 50, 5, 20.0, 4, 15.0))
    conn.commit()
    conn.close()
    import score as SC
    cfgcore = tmp_path / "c"
    cfgcore.mkdir()
    shutil.copy(CORE / "config.yaml", cfgcore / "config.yaml")
    monkeypatch.setattr(SC, "BASE", cfgcore)
    return {"path": p, "SC": SC, "connect": lambda: sqlite3.connect(str(p)),
            "dates": dates, "monkeypatch": monkeypatch}


def test_rt_series_empty_inputs(rt_env):
    """minutes空 / 历史库空 → []"""
    import rt_score as RT
    conn = rt_env["connect"]()
    assert RT.rt_series(conn, {"date": rt_env["dates"][-1]}, []) == []
    empty = sqlite3.connect(":memory:")
    empty.execute("""CREATE TABLE index_daily(code TEXT, date TEXT,
      open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL)""")
    empty.execute("""CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER,
      dt_count INTEGER, zb_rate REAL, lb_max INTEGER, jl_rate REAL)""")
    assert RT.rt_series(empty, {"date": "2026-11-06"}, [{"t": "09:30", "close": 1}]) == []
    conn.close()


def test_rt_series_none_gap_preserved(rt_env):
    """close缺数的分钟 → 对应位None, 其余位有限分数"""
    import rt_score as RT
    conn = rt_env["connect"]()
    ms = [{"t": "09:30", "close": None}, {"t": "09:31", "close": 3300, "cumvol": 1e7},
          {"t": "09:32", "close": None}, {"t": "09:33", "close": 3310, "cumvol": 2e7}]
    out = RT.rt_series(conn, {"date": rt_env["dates"][-1]}, ms)
    assert len(out) == 4
    assert out[0] is None and out[2] is None
    assert all(isinstance(v, float) and np.isfinite(v) for v in (out[1], out[3]))
    conn.close()


def test_rt_series_identical_minute_idempotent(rt_env):
    """两个相同分钟(同close无cumvol) → 改末行幂等, 分数一致"""
    import rt_score as RT
    conn = rt_env["connect"]()
    ms = [{"t": "10:00", "close": 3350}, {"t": "10:01", "close": 3350}]
    out = RT.rt_series(conn, {"date": rt_env["dates"][-1]}, ms)
    assert out[0] is not None and out[0] == out[1]
    conn.close()


# ══════════ ③ fetch_*断点续传 ══════════

def _seed_market_db(p: Path, n_days=15):
    """mktval续传夹具库: 主指数n日(index_daily供trading_dates) + 空表"""
    conn = sqlite3.connect(str(p))
    conn.execute("""CREATE TABLE index_daily(code TEXT, date TEXT, open REAL, high REAL,
      low REAL, close REAL, volume REAL, amount REAL, PRIMARY KEY(code, date))""")
    conn.execute("""CREATE TABLE index_val_daily(code TEXT, date TEXT, pe REAL,
      pb REAL, name TEXT, PRIMARY KEY(code, date))""")
    dates = pd.bdate_range("2026-10-01", periods=n_days).strftime("%Y-%m-%d").tolist()
    for d in dates:
        conn.execute("INSERT INTO index_daily VALUES('1.000001',?,?,?,?,?,?,?)",
                     (d, 3000, 3010, 2990, 3005, 3e8, 3e11))
    conn.commit()
    conn.close()
    return dates


def test_mktval_run_skips_have_days(tmp_path, monkeypatch, capsys):
    """M4续传: mv_total已有日跳过, 只聚合缺口日"""
    import fetch_mktval as FM
    db = tmp_path / "m.db"
    dates = _seed_market_db(db, 15)
    conn = sqlite3.connect(str(db))
    FM._ensure_tables(conn)
    for d in dates[:10]:                                   # 前10日已有mv_total
        conn.execute("INSERT INTO mkt_val_daily(date,mv_total) VALUES(?,1000)", (d,))
    conn.commit()
    conn.close()
    calls = []
    monkeypatch.setattr(FM.T, "has_token", lambda: True)
    monkeypatch.setattr(FM, "backfill_members", lambda conn=None: None)
    monkeypatch.setattr(FM, "connect", lambda: sqlite3.connect(str(db)))
    monkeypatch.setattr(FM, "_fetch_one_day",
                        lambda conn, d, verbose=True, min_n=30: calls.append(d) or True)
    FM.run("20261001")
    assert calls == dates[10:]                             # 只拉后5日
    assert "5/15 交易日待聚合" in capsys.readouterr().out


def test_mktval_run_false_counts_and_breaks(tmp_path, monkeypatch, capsys):
    """M4r: _fetch_one_day吞错返回False也计入连续失败, 10次即中断(不空转)"""
    import fetch_mktval as FM
    db = tmp_path / "m.db"
    _seed_market_db(db, 30)
    n = []
    monkeypatch.setattr(FM.T, "has_token", lambda: True)
    monkeypatch.setattr(FM, "backfill_members", lambda conn=None: None)
    monkeypatch.setattr(FM, "connect", lambda: sqlite3.connect(str(db)))
    monkeypatch.setattr(FM, "_fetch_one_day",
                        lambda conn, d, verbose=True, min_n=30: n.append(1) or False)
    ok = FM.run("20261001")
    assert ok is False and len(n) == 10                    # 恰10次即断, 不空转30日
    assert "连续失败中断" in capsys.readouterr().out


def _etf_df(rows):
    return pd.DataFrame(rows, columns=["trade_date", "close", "amount", "vol"])


def test_etf_run_failfast_and_idempotent(tmp_path, monkeypatch, capsys):
    """连续失败≥5中断(重跑续传) + 成功路径幂等upsert"""
    import fetch_etf as FE
    db = tmp_path / "e.db"
    monkeypatch.setattr(FE.T, "has_token", lambda: True)
    monkeypatch.setattr(FE, "connect", lambda: sqlite3.connect(str(db)))
    fails = []
    monkeypatch.setattr(FE.T, "call",
                        lambda *a, **kw: fails.append(kw) or (_ for _ in ()).throw(RuntimeError("x")))
    assert FE.run() is False
    assert len(fails) == 5                                 # sorted(白名单)第5只即断
    assert "连续失败中断" in capsys.readouterr().out
    # 成功路径: 每代码3行, 二跑幂等
    good = _etf_df([("20261104", 3.95, 1000.0, 200.0), ("20261105", 3.96, 1100.0, 210.0),
                    ("20261106", 3.97, 1200.0, 220.0)])
    monkeypatch.setattr(FE.T, "call", lambda *a, **kw: good)
    assert FE.run() is True
    conn = sqlite3.connect(str(db))
    n1 = conn.execute("SELECT COUNT(*) FROM etf_daily").fetchone()[0]
    conn.close()
    FE.run()
    conn = sqlite3.connect(str(db))
    n2 = conn.execute("SELECT COUNT(*) FROM etf_daily").fetchone()[0]
    conn.close()
    assert n1 == len(FE.ETF_INDEXES) * 3 and n2 == n1      # 幂等


def test_val_run_start_filter_and_failfast(tmp_path, monkeypatch, capsys):
    """start本地过滤(前史不写) + 连续失败中断"""
    import fetch_val as FV
    db = tmp_path / "v.db"
    monkeypatch.setattr(FV.T, "has_token", lambda: True)
    monkeypatch.setattr(FV, "connect", lambda: sqlite3.connect(str(db)))
    df = pd.DataFrame([("20130101", 10.0, 1.2), ("20140701", 10.5, 1.3),
                       ("20140702", 10.6, 1.3)], columns=["trade_date", "pe", "pb"])
    monkeypatch.setattr(FV.T, "call", lambda *a, **kw: df)
    assert FV.run("20140601") is True
    conn = sqlite3.connect(str(db))
    dates = {r[0] for r in conn.execute("SELECT DISTINCT date FROM index_val_daily")}
    conn.close()
    assert dates == {"2014-07-01", "2014-07-02"}           # 2013前史被过滤
    n = []
    monkeypatch.setattr(FV.T, "call",
                        lambda *a, **kw: n.append(1) or (_ for _ in ()).throw(RuntimeError("x")))
    assert FV.run("20140601") is False and len(n) == 5
    assert "连续失败中断" in capsys.readouterr().out
