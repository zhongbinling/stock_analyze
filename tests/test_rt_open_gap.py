# -*- coding: utf-8 -*-
"""实时行情盘中缺口自愈: 服务器晚于开盘启动 → tick首样本>09:35 → 分钟线源补开盘前缀"""
import sqlite3
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import server.rt as rt
    monkeypatch.setattr(rt, "RT_DB", tmp_path / "realtime.db")
    monkeypatch.setattr(rt, "_state", {"date": None, "samples": [], "updated_at": None})
    monkeypatch.setattr(rt, "_day_cache", {})
    monkeypatch.setattr(rt, "_open_gap_healed", {})
    return rt


TODAY = "2026-11-06"
NOW_IN = datetime(2026, 11, 6, 9, 46)          # 周五盘中(>09:40)


def _minute(m):
    return {"t": f"09:{m:02d}", "close": 3600.0 + m, "cumvol": 1e8 + m * 1e6,
            "cumamt": None, "score": None, "v1": None, "stage": None,
            "zt": None, "jl": None, "etf": None}


def _mock_deps(monkeypatch, minutes, scores=None):
    import server.rt as rt
    monkeypatch.setattr(rt, "_minute_samples", lambda amt: minutes)
    monkeypatch.setattr(rt, "_fetch_etf_amount_yi", lambda: None)
    monkeypatch.setitem(sys.modules, "rt_score", types.SimpleNamespace(
        rt_series=lambda conn, snap, ms: (scores or [-20.0] * len(ms))))
    monkeypatch.setitem(sys.modules, "db", types.SimpleNamespace(
        connect=lambda: sqlite3.connect(":memory:")))


def test_heal_fills_open_prefix(env, monkeypatch):
    """首样本09:45:22(服务器晚启动) → 补09:30~09:45前缀, tick样本保留尾部"""
    env._state.update(date=TODAY, samples=[
        {"t": "09:45:22", "close": 3627.0, "cumvol": 1.3e8, "score": -19.6,
         "v1": -19.8, "stage": "偏冷"}])
    _mock_deps(monkeypatch, [_minute(m) for m in range(30, 47)])
    assert env._heal_open_gap(NOW_IN, {"date": TODAY, "amount1": 1e11}) is True
    s = env._state["samples"]
    assert s[0]["t"] == "09:30" and s[-1]["t"] == "09:45:22"
    assert len(s) == 16 + 1                          # 16分钟前缀(09:30~09:45) + 1 tick样本
    assert s[0]["score"] == -20.0                    # rt_series回填情绪
    assert s[0]["stage"] == "偏冷"                    # 近似stage继承
    assert env._open_gap_healed[TODAY] is True
    assert env._heal_open_gap(NOW_IN, {}) is False   # 一次性


def test_heal_noop_when_healthy(env, monkeypatch):
    """首样本09:31(tick从早盘起步) → 不动; 09:40前不判"""
    env._state.update(date=TODAY, samples=[{"t": "09:31:00", "close": 1.0}])
    called = []
    monkeypatch.setattr(env, "_minute_samples",
                        lambda amt: called.append(1) or [])
    assert env._heal_open_gap(NOW_IN, {}) is False
    assert not called and TODAY not in env._open_gap_healed   # 未标记可后续再判
    assert env._heal_open_gap(datetime(2026, 11, 6, 9, 38), {}) is False  # <09:40


def test_heal_state_guards(env, monkeypatch):
    """日期不匹配/空样本/分钟源空 → 不炸, 空源当日标记防刷屏"""
    assert env._heal_open_gap(NOW_IN, {}) is False           # 空样本
    env._state.update(date="2026-11-05", samples=[{"t": "09:50:00"}])
    assert env._heal_open_gap(NOW_IN, {}) is False           # 非今日
    env._state.update(date=TODAY, samples=[{"t": "09:50:00", "close": 1}])
    _mock_deps(monkeypatch, [])                              # 分钟源失败
    assert env._heal_open_gap(NOW_IN, {}) is False
    assert env._open_gap_healed[TODAY] is True               # 当日不再试


def test_heal_threshold_semantics(env, monkeypatch):
    """阈值语义(字符串比较): 09:30:05起步=健康不拉源; 09:35:00起步=晚启动
    (缺09:30~09:34)触发自愈 — 与backfill的09:35:00口径一致"""
    env._state.update(date=TODAY, samples=[{"t": "09:30:05", "close": 1.0}])

    def boom(amt):
        raise AssertionError("健康起步不应拉分钟源")
    monkeypatch.setattr(env, "_minute_samples", boom)
    assert env._heal_open_gap(NOW_IN, {}) is False
    env._state["samples"] = [{"t": "09:35:00", "close": 1.0}]
    _mock_deps(monkeypatch, [_minute(m) for m in range(30, 36)])
    assert env._heal_open_gap(NOW_IN, {}) is True
    assert env._state["samples"][0]["t"] == "09:30"     # 补齐09:30~09:34


def test_heal_persisted_to_db(env, monkeypatch):
    """自愈结果落库(重启水合可见)"""
    env._state.update(date=TODAY, samples=[{"t": "09:45:22", "close": 3627.0,
                                            "score": -19.6, "stage": "偏冷"}])
    _mock_deps(monkeypatch, [_minute(m) for m in range(30, 46)])
    env._heal_open_gap(NOW_IN, {"date": TODAY})
    con = sqlite3.connect(str(env.RT_DB))
    n, blob = con.execute("SELECT n, payload FROM rt_day WHERE date=?", (TODAY,)).fetchone()
    con.close()
    import json
    assert n == 16 + 1
    assert json.loads(blob)["samples"][0]["t"] == "09:30"
