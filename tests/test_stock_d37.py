# -*- coding: utf-8 -*-
"""D1-37 规则引擎新增现金流规则(用户要求补上覆盖面的缺口)

① 近12个月经营现金流净流出, 且流出额达到净利润的一成(或本身亏损) → 高危
② 近12个月经营现金流不足净利润的一半(含不构成"净流出"的微小负数) → 关注
③ 滚动12个月值: 年报期即累计值, 其余 = 上年年报 + 本期累计 − 上年同期累计(消除回款
   季节性); 次新股首个年报之前算不出 → 不判
此前规则引擎没有现金流维度: 五粮液2026年前三季度经营现金流合计仅0.47亿(净利润
99.69亿), AI扫雷把"经营现金流连续为负"列为高危, 规则评级却是A。
生产库回测(自选池9只×历史各报告期共430个组合): 75个评级变化(A→B 26 / A→C 18 /
B→C 10 / C→D 14 / D→D 7), 均对应真实存在的现金回流偏弱或净流出; 当前五粮液、
新易盛A→B, 中航沈飞C→D, 其余6只不变。
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import fetch_stock as FS   # noqa: E402
import stock_risk as SR    # noqa: E402

# 茅台真实的年内累计值(亿): 现金流量表经营活动现金流量净额 / 利润表净利润
MAOTAI_OCF = {"2024-12-31": 924.64, "2025-03-31": 88.09, "2025-09-30": 381.97,
              "2025-12-31": 615.22, "2026-09-30": 840.59}
MAOTAI_NI = {"2025-09-30": 668.99, "2025-12-31": 853.10, "2026-09-30": 667.17}


# ════════════ ③ 滚动12个月值 ════════════

def test_ttm_rolling_value():
    """年报期 = 累计值; 中期 = 上年年报 + 本期累计 − 上年同期累计"""
    s = {k: v * 1e8 for k, v in MAOTAI_OCF.items()}
    assert SR._ttm(s, "2025-12-31") == pytest.approx(615.22e8)
    assert SR._ttm(s, "2026-09-30") == pytest.approx((615.22 + 840.59 - 381.97) * 1e8)


def test_ttm_not_computable_until_first_annual_report():
    """缺上年年报或上年同期 → None(次新股首个年报之前算不出; 本期本身缺失也是None)"""
    s = {"2026-03-31": 10.0, "2026-06-30": 25.0, "2026-09-30": 40.0}
    assert SR._ttm(s, "2026-09-30") is None and SR._ttm(s, "2026-12-31") is None
    assert SR._ttm(dict(s, **{"2025-12-31": 30.0}), "2026-09-30") is None      # 缺上年同期
    assert SR._ttm(dict(s, **{"2025-09-30": 22.0}), "2026-09-30") is None      # 缺上年年报
    assert SR._ttm(dict(s, **{"2025-12-31": 30.0, "2025-09-30": 22.0}), "2026-09-30") == 48.0
    assert SR._ttm(dict(s, **{"2026-12-31": 60.0}), "2026-12-31") == 60.0       # 首个年报即可判


def _seed(db, ocf=MAOTAI_OCF, ni=MAOTAI_NI):
    con = FS._conn(db)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','贵州茅台','白酒','t','t')")
    con.executemany(
        "INSERT INTO stock_finance(ts_code,end_date,ann_date,profit_dedt,netprofit_yoy,or_yoy,"
        "grossprofit_margin,roe,debt_to_assets) VALUES(?,?,?,?,?,?,?,?,?)",
        [("600519.SH", d, "2026-10-28", 5e9, 1.0, 1.0, 90.0, 20.0, 20.0) for d in ni])
    con.executemany("INSERT INTO stock_income VALUES(?,?,1,1,?)",
                    [("600519.SH", d, v * 1e8) for d, v in ni.items()])
    con.executemany("INSERT INTO stock_cashflow VALUES(?,?,?)",
                    [("600519.SH", d, v * 1e8) for d, v in ocf.items()])
    con.commit()
    con.close()


def test_finance_metrics_carries_ttm_cashflow(tmp_path):
    """指标载荷带上滚动值(亿): 茅台2026-09-30 → 经营现金流1073.84 / 净利润851.28"""
    db = tmp_path / "stock.db"
    _seed(db)
    rows = {r["end_date"]: r for r in SR.finance_metrics("600519", path=db)["rows"]}
    assert rows["2026-09-30"]["ocf_ttm"] == pytest.approx(1073.84, abs=0.02)
    assert rows["2026-09-30"]["ni_ttm"] == pytest.approx(851.28, abs=0.02)
    assert rows["2025-12-31"]["ocf_ttm"] == pytest.approx(615.22, abs=0.01)
    assert rows["2025-12-31"]["ni_ttm"] == pytest.approx(853.10, abs=0.01)
    assert SR.run_rules({"rows": list(rows.values())})["grade"] == "A"       # 现金充裕: 不命中


def test_finance_metrics_without_income_or_cashflow_tables(tmp_path):
    """没拉过利润表/现金流量表(或次新股算不出滚动值): 字段为None, 规则不命中、不报错"""
    db = tmp_path / "stock.db"
    _seed(db, ocf={}, ni={"2026-09-30": 1.0})
    r = SR.finance_metrics("600519", path=db)["rows"][-1]
    assert (r["ocf_ttm"], r["ni_ttm"]) == (None, None)
    assert SR.run_rules({"rows": [r]})["hits"] == []


# ════════════ ①② 规则 ════════════

def test_cashflow_rules_on_real_cases():
    """生产库真实值: 五粮液2026Q2滚动净流出35.84亿 → 高; 最新12% → 中;
    现金充裕的茅台/紫金矿业/东山精密不命中; 河钢资源亏损但现金流为正 → 不命中(亏损另有规则)"""
    assert SR._cashflow_hits(-35.84, 134.33) == [{
        "rule": "近12个月经营现金流净流出35.84亿(净利润134.33亿)", "level": "高", "score": -10}]
    assert SR._cashflow_hits(15.05, 124.99) == [{
        "rule": "近12个月经营现金流仅为净利润的12%(15.05/124.99亿), 利润现金含量低",
        "level": "中", "score": -5}]
    for ocf, ni in ((1073.84, 851.29), (1075.98, 916.32), (58.76, 54.84), (5.22, -0.10)):
        assert SR._cashflow_hits(ocf, ni) == [], (ocf, ni)
    assert SR._cashflow_hits(-2.85, -2.02)[0]["level"] == "高"        # 亏损且净流出


def test_cashflow_rules_materiality_and_boundaries():
    """微小净流出(-0.23亿对净利润29.74亿, 中航沈飞2025Q3实测)只算关注, 不算高危;
    门槛边界: 净现比0.5(含)不命中; 净流出达到净利润一成(含)才是高危"""
    small = SR._cashflow_hits(-0.23, 29.74)
    assert [(h["level"], h["score"]) for h in small] == [("中", -5)]
    assert small[0]["rule"] == "近12个月经营现金流为负(-0.23亿, 净利润29.74亿)"
    assert SR._cashflow_hits(50.0, 100.0) == [] and SR._cashflow_hits(100.0, 100.0) == []
    assert SR._cashflow_hits(49.99, 100.0)[0]["level"] == "中"
    assert SR._cashflow_hits(0.0, 100.0)[0]["rule"].startswith("近12个月经营现金流仅为净利润的0%")
    assert SR._cashflow_hits(-10.0, 100.0)[0]["level"] == "高"
    assert SR._cashflow_hits(-9.99, 100.0)[0]["level"] == "中"
    assert SR._cashflow_hits(-0.01, 0.0)[0]["level"] == "高"          # 净利润为0: 净流出即高危
    assert SR._cashflow_hits(None, 1.0) == [] and SR._cashflow_hits(1.0, None) == []


def test_cashflow_rule_changes_grades():
    """评级合成: 原本全健康的五粮液 → B; 已是C(-15)的中航沈飞叠加现金流关注 → D;
    不带新字段的旧式指标行行为不变"""
    healthy = {"dedt_yi": 20.0, "netprofit_yoy": 50.0, "or_yoy": 20.0, "gpm": 60.0,
               "roe": 20.0, "debt_to_assets": 30.0, "goodwill_eq": None}
    wly = dict(healthy, ocf_ttm=15.05, ni_ttm=124.99)
    r = SR.run_rules({"rows": [dict(healthy, netprofit_yoy=95.0), wly]})
    assert (r["grade"], r["score"]) == ("B", -5)
    assert [h["rule"] for h in r["hits"]] == [
        "近12个月经营现金流仅为净利润的12%(15.05/124.99亿), 利润现金含量低"]
    shenfei = dict(healthy, dedt_yi=10.0, netprofit_yoy=-26.0, or_yoy=-34.19,
                   ocf_ttm=9.10, ni_ttm=31.76)
    r2 = SR.run_rules({"rows": [dict(healthy, netprofit_yoy=-58.37, or_yoy=-57.68), shenfei]})
    assert (r2["grade"], r2["score"]) == ("D", -20)
    assert [h["score"] for h in r2["hits"]] == [-10, -5, -5]
    assert SR.run_rules({"rows": [dict(healthy, netprofit_yoy=49.0), healthy]})["grade"] == "A"


def test_scan_stores_cashflow_rule(tmp_path, monkeypatch):
    """扫雷主流程: 规则评级与命中项里带上现金流规则; 缓存口径已升级(旧评级作废)"""
    import analyze_news as AN
    import ai_client
    db = tmp_path / "stock.db"
    _seed(db, ocf={"2025-12-31": 297.06, "2025-09-30": 282.47, "2026-09-30": 0.47},
          ni={"2025-09-30": 67.87, "2025-12-31": 93.17, "2026-09-30": 99.69})   # 五粮液
    monkeypatch.setattr(AN, "get_ai_config", lambda: {
        "enabled": True, "model": "m", "api_key": "", "base_url": "http://x"})
    monkeypatch.setattr(ai_client, "chat", lambda *a, **k: json.dumps(
        {"summary": "s", "points": [], "advice": ""}))
    r = SR.scan_stock("600519", db)
    assert (r["grade"], r["score"]) == ("B", -5)
    assert "经营现金流" in r["rules"][0]["rule"] and r["rules"][0]["level"] == "中"
    assert SR.SCAN_VERSION >= 4
    con = FS._conn(db)
    assert json.loads(con.execute("SELECT ai_json FROM stock_ai").fetchone()[0])["_v"] == SR.SCAN_VERSION
    con.close()
