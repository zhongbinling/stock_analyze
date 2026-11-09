# -*- coding: utf-8 -*-
"""避雷针A4: AI财报风险评级 — 规则引擎/混合评级/幻觉校验/缓存失效/降级/API/UI"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import fetch_stock as FS  # noqa: E402
import stock_risk as SR   # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "stock.db"
    monkeypatch.setattr(FS, "STOCK_DB", db)
    monkeypatch.setattr(SR, "_conn", lambda path=None: FS._conn(path))
    return db


def _metrics(rows):
    return {"code": "X", "name": "测试", "n": len(rows), "rows": rows}


# ── 规则引擎 ──

def test_rules_all_healthy(env):
    m = _metrics([{"end_date": f"2026-0{i}-31", "dedt_yi": 10.0 - i,
                   "netprofit_yoy": 15.0 - i, "or_yoy": 10.0,
                   "gpm": 50.0, "roe": 8.0, "debt_to_assets": 40.0,
                   "goodwill_eq": 5.0} for i in range(1, 5)])
    r = SR.run_rules(m)
    assert r["score"] == 0 and r["grade"] == "A" and r["hits"] == []


def test_rules_grade_ladder(env):
    """扣分→评级映射: -5=B, -15=C, ≤-20=D"""
    rows = [{"end_date": "2026-09-30", "dedt_yi": -5.0, "netprofit_yoy": -40.0,
             "netprofit_yoy_prev": None, "or_yoy": -10.0, "gpm": 50.0,
             "roe": -2.0, "debt_to_assets": 80.0, "goodwill_eq": 45.0}]
    # 构造连续序列使"同比连续为负"命中
    rows = [{"end_date": f"2026-0{i}-31", "dedt_yi": -5.0, "netprofit_yoy": -30.0 - i,
             "or_yoy": -10.0 if i >= 3 else 10.0, "gpm": 50.0, "roe": -2.0,
             "debt_to_assets": 80.0, "goodwill_eq": 45.0} for i in range(1, 5)]
    r = SR.run_rules(_metrics(rows))
    rules = [h["rule"] for h in r["hits"]]
    assert any("扣非净利润为负" in x for x in rules)
    assert any("同比连续为负" in x for x in rules)
    assert any("资产负债率" in x for x in rules)
    assert any("商誉" in x for x in rules)
    assert any("ROE为负" in x for x in rules)
    assert any("营收同比连续为负" in x for x in rules)
    assert r["score"] <= -40 and r["grade"] == "D"


def test_rules_gpm_decline_and_turn_negative(env):
    rows = [{"end_date": f"2025-{m:02d}-28" if m != 12 else "2025-12-31",
             "dedt_yi": 5.0, "netprofit_yoy": 20.0 if m != 12 else -5.0,
             "or_yoy": 5.0, "gpm": 50.0 - m, "roe": 5.0,
             "debt_to_assets": 40.0, "goodwill_eq": None}
            for m in (3, 6, 9, 12)]
    r = SR.run_rules(_metrics(rows))
    rules = [h["rule"] for h in r["hits"]]
    assert any("毛利率连续3期下滑" in x for x in rules)      # 47>46>45>44
    assert any("同比转负" in x for x in rules)                # 20→-5


# ── 幻觉校验(N2) ──

def test_validate_ai_points_suspect():
    m = _metrics([{"end_date": "2026-09-30", "dedt_yi": 5.0, "netprofit_yoy": -45.2,
                   "or_yoy": 5.0, "gpm": 50.0, "roe": 5.0,
                   "debt_to_assets": 40.0, "goodwill_eq": None}])
    ai = {"points": [
        {"title": "业绩下滑", "evidence": "扣非净利同比=-45.2%", "severity": "高"},
        {"title": "编造", "evidence": " imaginary_ratio=99", "severity": "高"}]}
    out = SR._validate_ai_points(ai, m)
    assert out["points"][0]["suspect"] in (False, None) or not out["points"][0]["suspect"]
    assert out["points"][1]["suspect"] is True


# ── 主流程: 缓存命中/失效(ann_date变化)/次新降级/AI失败降级 ──

def test_fresh_stock_degrades(env):
    assert SR.analyze_stock_finance("000000.SZ", env)["ok"] is False


def test_cache_and_ann_date_invalidation(env, monkeypatch):
    import analyze_news as AN
    monkeypatch.setattr(AN, "get_ai_config",
                        lambda: {"enabled": False, "model": "", "api_key": "",
                                 "base_url": ""})
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','茅台','','t','t')")
    con.executemany("INSERT INTO stock_finance(ts_code,end_date,ann_date,profit_dedt,"
                    "netprofit_yoy,or_yoy,grossprofit_margin,roe,debt_to_assets) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [("600519.SH", "2026-06-30", "2026-08-15", 2e9, 5.0, 8.0,
                      91.0, 15.0, 30.0),
                     ("600519.SH", "2026-09-30", "2026-10-28", 2.2e9, 6.0, 9.0,
                      91.0, 15.0, 30.0)])
    con.commit()
    con.close()
    r1 = SR.analyze_stock_finance("600519.SH", env)
    assert r1["ok"] and r1["cached"] is False and r1["grade"] == "A"
    r2 = SR.analyze_stock_finance("600519.SH", env)
    assert r2["cached"] is True                              # 同报告期同公告日 → 缓存
    con = FS._conn(env)
    con.execute("UPDATE stock_finance SET ann_date='2026-10-30' "
                "WHERE end_date='2026-09-30'")               # 更正版财报
    con.commit()
    con.close()
    r3 = SR.analyze_stock_finance("600519.SH", env)
    assert r3["cached"] is False                             # ann_date变化 → 重析


def test_ai_failure_degrades_to_rules(env, monkeypatch):
    import analyze_news as AN
    import ai_client
    monkeypatch.setattr(AN, "get_ai_config",
                        lambda: {"enabled": True, "model": "m", "api_key": "",
                                 "base_url": "http://x"})
    monkeypatch.setattr(ai_client, "chat",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ollama down")))
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','茅台','','t','t')")
    con.executemany("INSERT INTO stock_finance(ts_code,end_date,ann_date,profit_dedt,"
                    "netprofit_yoy,or_yoy,grossprofit_margin,roe,debt_to_assets) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [("600519.SH", "2026-06-30", None, 2e9, 5.0, 8.0, 91.0, 15.0, 30.0),
                     ("600519.SH", "2026-09-30", None, 2.2e9, 6.0, 9.0, 91.0, 15.0, 30.0)])
    con.commit()
    con.close()
    r = SR.analyze_stock_finance("600519.SH", env)
    assert r["ok"] and r["grade"] == "A"                     # 规则照常
    assert "失败" in r["ai"]["summary"]                       # AI降级提示


# ── API + UI ──

def test_finance_ai_api(server_app, monkeypatch):
    c = server_app["client"]
    import stock_risk
    monkeypatch.setattr(stock_risk, "analyze_stock_finance",
                        lambda code: {"ok": False, "msg": "数据不足"})
    assert c.get("/admin/api/stock/finance_ai?code=x").status_code == 404
    # POST: 不再要求AI开关(纯规则分析合法), 先保障finance/bs数据
    import fetch_stock as FS
    refs = []
    monkeypatch.setattr(FS, "refresh_finance", lambda c2, path=None: refs.append("fin"))
    monkeypatch.setattr(FS, "refresh_bs", lambda c2, path=None: refs.append("bs"))
    assert c.post("/admin/api/stock/finance_ai", json={"code": "x"}).status_code == 404
    assert refs == ["fin", "bs"]


def test_validate_chinese_evidence_not_suspect():
    """review修复回归: AI依据用中文指标名+四舍五入值(真实场景)不误判存疑"""
    m = _metrics([{"end_date": "2026-09-30", "dedt_yi": 5.0, "netprofit_yoy": -45.234,
                   "or_yoy": 5.0, "gpm": 50.0, "roe": 5.0,
                   "debt_to_assets": 40.0, "goodwill_eq": None}])
    ai = {"points": [
        {"title": "业绩下滑", "evidence": "扣非净利同比=-45.2%(连续2期)", "severity": "高"},
        {"title": "毛利率走弱", "evidence": "毛利率由52降至50附近", "severity": "低"},
        {"title": "编造", "evidence": " nonexistent_ratio=99", "severity": "高"}]}
    out = SR._validate_ai_points(ai, m)
    assert not out["points"][0]["suspect"]          # 中文基名命中
    assert not out["points"][1]["suspect"]          # 毛利率基名命中(无精确数值也过)
    assert out["points"][2]["suspect"] is True      # 无中生有仍拦


def test_a4_ui_markers():
    import re
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    for mk in ("btnAiFin", "aiFinBody", "GRADE_COLOR", "loadAiFin", "依据存疑",
               "非投资建议", "规则分"):
        assert mk in html, f"缺: {mk}"
    if shutil.which("node"):
        blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
        main = max(blocks, key=len)
        r = subprocess.run([shutil.which("node"), "-e",
                            "new Function(process.argv[1])", main],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f"JS语法错误: {r.stderr[:300]}"
