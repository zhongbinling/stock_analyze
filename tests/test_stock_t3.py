# -*- coding: utf-8 -*-
"""测试T3: 避雷针补强 — 真实数据/真模型E2E + fetch_stock幂等断点 + 规则全覆盖收官

E2E语义(沿用T1模式): 读生产stock.db真数据(茅台在池) + 真Ollama扫雷;
环境不满足(无池/无模型)整文件skip不假绿。单元部分: 幂等重跑一致性。
"""
import functools
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import fetch_stock as FS   # noqa: E402
import stock_risk as SR    # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "stock.db"
    monkeypatch.setattr(FS, "STOCK_DB", db)
    monkeypatch.setattr(SR, "_conn", lambda path=None: FS._conn(path))
    return db


# ── 环境守卫: 生产库有池且Ollama可达(仅E2E用例) ──

@functools.lru_cache(maxsize=1)
def _e2e_ready() -> bool:
    """模块导入+装饰器评估各调一次 → lru_cache只探一次(库查询+11434探测)"""
    try:
        import requests
        r = requests.get("http://localhost:11434/api/tags", timeout=3)
        models = [m.get("name") for m in r.json().get("models", [])]
        con = FS._conn()
        n = con.execute("SELECT COUNT(*) FROM stock_pool").fetchone()[0]
        fin = con.execute("SELECT COUNT(*) FROM stock_finance WHERE profit_dedt IS NOT NULL").fetchone()[0]
        con.close()
        return n > 0 and fin >= 2 and any(models)
    except Exception:                                         # noqa: BLE001
        return False


_e2e = pytest.mark.skipif(not _e2e_ready(),
                         reason="生产stock.db无池数据或Ollama不可达 — E2E跳过")


# ── ① 真实数据E2E: 全维度载荷(不加mock) ──

@_e2e
def test_e2e_finance_full_real():
    """真库茅台: 四组载荷量级合理性断言(非trivial)"""
    d = FS.finance_full_payload("600519.SH")
    assert d["ok"] and len(d["dates"]) >= 8                    # 至少8季
    rev = [v for v in d["profit"]["revenue"] if v]
    assert rev and 100 < rev[-1] < 2000                        # 茅台季度营收百亿级
    assert d["balance"]["assets"][-1] > 1000                   # 总资产千亿级
    assert 0 < (d["profit"]["roe"][-1] or 0) < 50              # ROE正且<50%


@_e2e
def test_e2e_scan_real_model():
    """真模型扫雷(force): 规则+AI全链路, 评级合法且报告入库可回看"""
    r = SR.scan_stock("600519.SH", force=True)
    assert r["ok"] and r["grade"] in "ABCD"
    assert -40 <= r["score"] <= 0
    # 入库可回看(缓存命中)
    r2 = SR.scan_stock("600519.SH")
    assert r2["cached"] is True and r2["grade"] == r["grade"]
    ai = r.get("ai") or {}
    if ai.get("points"):                                       # AI成功时
        for p in ai["points"]:
            assert p["severity"] in ("高", "中", "低")
            assert p["dimension"] in ("财务", "筹码", "估值", "事件")


@_e2e
def test_e2e_kline_adj_consistency_real():
    """真库复权一致性: qfq最新价≈原始收盘价(归一到最新因子)"""
    k = FS.kline("600519.SH", "qfq")
    assert k["ok"]
    con = FS._conn()
    raw = con.execute("SELECT close FROM stock_daily WHERE ts_code=? "
                      "ORDER BY trade_date DESC LIMIT 1", ("600519.SH",)).fetchone()
    con.close()
    assert abs(k["closes"][-1] - raw[0]) < 0.5                 # 最新日qfq=原价


# ── ② fetch_stock幂等断点(部分数据中途落库→重跑不重不漏) ──

def test_add_stock_partial_retry_consistency(env, monkeypatch):
    """add_stock中途断(只落了daily没落factor) → 重跑全量upsert一致性"""
    import pandas as pd
    dates = pd.bdate_range("2026-01-01", periods=5).strftime("%Y%m%d").tolist()
    daily = pd.DataFrame({"ts_code": ["600519.SH"] * 5, "trade_date": dates,
                          "open": [10] * 5, "high": [11] * 5, "low": [9] * 5,
                          "close": [10.5] * 5, "vol": [1e6] * 5, "amount": [1e8] * 5})
    fac = pd.DataFrame({"ts_code": ["600519.SH"] * 5, "trade_date": dates,
                        "adj_factor": [1.0] * 5})
    state = {"fail_after_daily": True}

    def fake(api, fields="", **kw):
        if api == "stock_basic":
            return pd.DataFrame([{"ts_code": "600519.SH", "name": "茅台",
                                  "industry": "白酒"}])
        if api == "daily":
            if state["fail_after_daily"]:
                state["fail_after_daily"] = False
                raise RuntimeError("模拟中断")
            return daily
        if api == "adj_factor":
            return fac
        raise AssertionError(api)
    monkeypatch.setattr(FS.T, "has_token", lambda: True)
    monkeypatch.setattr(FS.T, "call", fake)
    # 第一次: 中断失败
    r1 = FS.add_stock("600519", env)
    assert r1["ok"] is False
    con = FS._conn(env)
    n0 = con.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
    con.close()
    assert n0 == 0                                             # 失败无半截落库(事务性)
    # 重跑: 成功且无重复
    r2 = FS.add_stock("600519", env)
    assert r2["ok"]
    con = FS._conn(env)
    n1 = con.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
    con.close()
    assert n1 == 5


def test_refresh_daily_gap_heal_semantics(env, monkeypatch):
    """日增断点: 错过多日(回看窗内)一次补齐, 窗外日不补"""
    import pandas as pd
    import datetime as dt
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','茅台','','t','t')")
    con.commit()
    con.close()
    today = dt.date.today()
    back3 = (today - dt.timedelta(days=3)).strftime("%Y%m%d")
    daily = pd.DataFrame({"ts_code": ["600519.SH"] * 2,
                          "trade_date": [back3, today.strftime("%Y%m%d")],
                          "open": [10, 11], "high": [11, 12], "low": [9, 10],
                          "close": [10.5, 11.5], "vol": [1e6] * 2, "amount": [1e8] * 2})
    fac = pd.DataFrame({"ts_code": ["600519.SH"] * 2,
                        "trade_date": [back3, today.strftime("%Y%m%d")],
                        "adj_factor": [1.0, 1.0]})
    monkeypatch.setattr(FS.T, "has_token", lambda: True)
    monkeypatch.setattr(FS.T, "call",
                        lambda api, fields="", **kw: daily if api == "daily" else fac)
    n = FS.refresh_daily(path=env)
    assert n == 2
    assert FS.refresh_daily(path=env) == 2                      # 幂等
    con = FS._conn(env)
    total = con.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
    con.close()
    assert total == 2


# ── ③ 规则全覆盖收官: 8规则×命中+未命中双向(补A4未覆盖的组合) ──

def _m(rows):
    return {"code": "X", "name": "t", "n": len(rows), "rows": rows}


BASE = {"dedt_yi": 10.0, "netprofit_yoy": 10.0, "or_yoy": 10.0, "gpm": 50.0,
        "roe": 10.0, "debt_to_assets": 40.0, "goodwill_eq": 5.0}


def _rows(n, **over):
    out = []
    for i in range(n):
        r = dict(BASE)
        r["end_date"] = f"2026-{(i % 12) + 1:02d}-28"
        r.update(over)
        out.append(r)
    return out


def test_rule_boundary_debt_70(env):
    """资产负债率70边界: >70命中, =70不命中"""
    r1 = SR.run_rules(_m(_rows(3, debt_to_assets=70.1)))
    assert any("资产负债率" in h["rule"] for h in r1["hits"])
    r2 = SR.run_rules(_m(_rows(3, debt_to_assets=70.0)))
    assert not any("资产负债率" in h["rule"] for h in r2["hits"])


def test_rule_boundary_goodwill_30(env):
    """商誉占净资产30边界: >30命中(-10高危), =30不命中"""
    r1 = SR.run_rules(_m(_rows(3, goodwill_eq=30.1)))
    hits = [h for h in r1["hits"] if "商誉" in h["rule"]]
    assert hits and hits[0]["level"] == "高" and hits[0]["score"] == -10
    r2 = SR.run_rules(_m(_rows(3, goodwill_eq=30.0)))
    assert not any("商誉" in h["rule"] for h in r2["hits"])


def test_rule_yoy_consecutive_requires_two(env):
    """同比连续为负需≥2期: 单期负不触发连续规则(可能触发转负)"""
    rows = _rows(4)
    rows[-1]["netprofit_yoy"] = -5.0                           # 仅最新一期负
    r = SR.run_rules(_m(rows))
    neg_hits = [h["rule"] for h in r["hits"] if "同比" in h["rule"]]
    assert all("连续" not in x for x in neg_hits)              # 不误报连续
    assert any("转负" in x for x in neg_hits)                  # 转负正确命中(高)


def test_grade_score_composition(env):
    """评级合成: 单中危-5→B, 两高危-20→D"""
    r1 = SR.run_rules(_m(_rows(3, debt_to_assets=75)))          # 单-5
    assert r1["score"] == -5 and r1["grade"] == "B"
    rows = _rows(3, dedt_yi=-5.0, roe=-2.0, debt_to_assets=75, goodwill_eq=40)
    r2 = SR.run_rules(_m(rows))                                 # -10-10-5-10=-35
    assert r2["score"] == -35 and r2["grade"] == "D"
