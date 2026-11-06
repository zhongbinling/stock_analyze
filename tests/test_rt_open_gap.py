# -*- coding: utf-8 -*-
"""实时行情定轴断块: 盘中断块自愈(开盘前缀+中间缺口/午休边界不误判/失败冷却)"""
import sqlite3
import sys
import types
from datetime import datetime, timedelta
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
    monkeypatch.setattr(rt, "_gap_cooldown", {"until": None})
    return rt


TODAY = "2026-11-06"
NOW_IN = datetime(2026, 11, 6, 9, 46)          # 周五盘中(>09:40)


def _minute(h, m):
    return {"t": f"{h:02d}:{m:02d}", "close": 3600.0 + m, "cumvol": 1e8 + m * 1e6,
            "cumamt": None, "score": None, "v1": None, "stage": None,
            "zt": None, "jl": None, "etf": None}


def _mock_deps(monkeypatch, minutes):
    import server.rt as rt
    monkeypatch.setattr(rt, "_minute_samples", lambda amt: minutes)
    monkeypatch.setattr(rt, "_fetch_etf_amount_yi", lambda: None)
    monkeypatch.setitem(sys.modules, "rt_score", types.SimpleNamespace(
        rt_series=lambda conn, snap, ms: [-20.0] * len(ms)))
    monkeypatch.setitem(sys.modules, "db", types.SimpleNamespace(
        connect=lambda: sqlite3.connect(":memory:")))


def test_heal_open_prefix(env, monkeypatch):
    """首样本09:45:22(tick晚启动) → 补09:30~09:44(严格介于, 不含09:45分钟)"""
    env._state.update(date=TODAY, samples=[
        {"t": "09:45:22", "close": 3627.0, "cumvol": 1.3e8, "score": -19.6,
         "v1": -19.8, "stage": "偏冷"}])
    _mock_deps(monkeypatch, [_minute(9, m) for m in range(30, 60)])
    n = env._heal_gaps(NOW_IN, {"date": TODAY, "amount1": 1e11})
    assert n == 15                                  # 09:30~09:44
    s = env._state["samples"]
    assert s[0]["t"] == "09:30" and s[-1]["t"] == "09:45:22"
    assert s[0]["score"] == -20.0 and s[0]["stage"] == "偏冷"
    assert env._heal_gaps(NOW_IN, {}) == 0          # 补后缺口闭合(幂等)


def test_heal_interior_gap(env, monkeypatch):
    """中间断块(09:35→10:30停机55分) → 原位拼接09:36~10:29;
    首样本09:35:00按阈值语义同时算晚启动 → 开盘前缀09:30~09:34一并补(2块)"""
    env._state.update(date=TODAY, samples=[
        {"t": "09:35:00", "close": 1.0, "cumvol": 1e8, "score": -19.0, "stage": "偏冷"},
        {"t": "10:30:10", "close": 2.0, "cumvol": 2e8, "score": -19.5, "stage": "偏冷"}])
    _mock_deps(monkeypatch, [_minute(9, m) for m in range(30, 60)]
               + [_minute(10, m) for m in range(0, 40)])
    n = env._heal_gaps(datetime(2026, 11, 6, 10, 31), {})
    assert n == 5 + 54                             # 前缀09:30~09:34 + 09:36~10:29
    s = env._state["samples"]
    assert s[0]["t"] == "09:30" and s[-1]["t"] == "10:30:10"
    assert s[5]["t"] == "09:35:00"                 # 锚点原位保留
    assert s[6]["t"] == "09:36" and s[-2]["t"] == "10:29"


def test_heal_lunch_boundary_not_gap(env, monkeypatch):
    """午休边界11:30→13:00(交易分钟序号相邻)不算断块, 不拉分钟源"""
    env._state.update(date=TODAY, samples=[
        {"t": "11:29:50", "close": 1.0},
        {"t": "13:00:20", "close": 1.1}])

    def boom(amt):
        raise AssertionError("午休边界不应触发补块")
    import server.rt as rt
    monkeypatch.setattr(rt, "_minute_samples", boom)
    assert env._heal_gaps(datetime(2026, 11, 6, 13, 5), {}) == 0


def test_heal_healthy_noop(env, monkeypatch):
    """健康序列(09:30起步, 相邻<3分)不触发"""
    env._state.update(date=TODAY, samples=[
        {"t": "09:30:05", "close": 1.0}, {"t": "09:31:00", "close": 1.1}])

    def boom(amt):
        raise AssertionError("健康不应拉分钟源")
    import server.rt as rt
    monkeypatch.setattr(rt, "_minute_samples", boom)
    assert env._heal_gaps(NOW_IN, {}) == 0


def test_heal_failure_cooldown(env, monkeypatch):
    """分钟源空 → 0点+冷却10分钟; 冷却期内不再拉源(重试等源恢复)"""
    env._state.update(date=TODAY, samples=[{"t": "09:50:00", "close": 1.0}])
    calls = []
    import server.rt as rt
    monkeypatch.setattr(rt, "_minute_samples", lambda amt: calls.append(1) or [])
    monkeypatch.setattr(rt, "_fetch_etf_amount_yi", lambda: None)
    assert env._heal_gaps(datetime(2026, 11, 6, 9, 55), {}) == 0
    assert len(calls) == 1 and env._gap_cooldown["until"] is not None
    assert env._heal_gaps(datetime(2026, 11, 6, 9, 58), {}) == 0
    assert len(calls) == 1                          # 冷却期内不再试
    assert env._heal_gaps(datetime(2026, 11, 6, 10, 10), {}) == 0
    assert len(calls) == 2                          # 过冷却重试(仍空, 再冷却)


def test_fixed_axis_ui_markers():
    """定轴断块UI: 全日242槽固定轴 + 槽对齐序列 + 差分跨缺槽携带 + node语法门禁"""
    import re
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "realtime.html").read_text(encoding="utf-8")
    for mk in ("const SLOTS", "const A = SLOTS.map", "const ts = SLOTS;",
               "A.map(x => x.close ?? null)", "let pv = null, pc = null",
               "bySlot.set(x.t.slice(0, 5), x)"):
        assert mk in html, f"缺: {mk}"
    assert "const ts = S.map" not in html            # 轴不再数据驱动
    if shutil.which("node"):
        blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
        main = max(blocks, key=len)
        r = subprocess.run([shutil.which("node"), "-e",
                            "new Function(process.argv[1])", main],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f"JS语法错误: {r.stderr[:300]}"


def test_heal_duplicate_anchor_single_insert(env, monkeypatch):
    """review修复回归: 重复t锚点(手动tick与轮询同秒) → 补块只插一次"""
    env._state.update(date=TODAY, samples=[
        {"t": "10:00:00", "close": 1.0},
        {"t": "10:00:00", "close": 1.0},          # 同秒重复锚点
        {"t": "10:30:00", "close": 2.0}])
    _mock_deps(monkeypatch, [_minute(10, m) for m in range(0, 31)])
    n = env._heal_gaps(datetime(2026, 11, 6, 10, 31), {})
    assert n == 30                                 # 10:00~10:29 仅一份
    s = [x["t"] for x in env._state["samples"]]
    assert s.count("10:01") == 1 and s.count("10:29") == 1


def test_heal_persisted_and_prefix_count(env, monkeypatch):
    """自愈落库(重启水合可见) + 早于09:40不判"""
    env._state.update(date=TODAY, samples=[{"t": "09:45:22", "close": 3627.0,
                                            "score": -19.6, "stage": "偏冷"}])
    assert env._heal_gaps(datetime(2026, 11, 6, 9, 38), {}) == 0   # <09:40
    _mock_deps(monkeypatch, [_minute(9, m) for m in range(30, 46)])
    env._heal_gaps(NOW_IN, {"date": TODAY})
    con = sqlite3.connect(str(env.RT_DB))
    n, blob = con.execute("SELECT n, payload FROM rt_day WHERE date=?", (TODAY,)).fetchone()
    con.close()
    import json
    assert n == 15 + 1
    assert json.loads(blob)["samples"][0]["t"] == "09:30"
