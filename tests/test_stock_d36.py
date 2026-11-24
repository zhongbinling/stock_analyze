# -*- coding: utf-8 -*-
"""D1-36 规则引擎门槛 + 本轮review修复回归

① 扣非净利同比两条规则按降幅分级 — 原"连续为负/由增转负"一律高危扣10分:
   茅台扣非净利同比-1.95%、-0.25%(持平级别)被判C级。生产库回测(自选池9只×
   历史各报告期共360个组合): 3个由C变B且均为微降, 大降幅的高危判定全部不变
② "连续为负"两期即可判 — 原要求存在第三期报告, 只有两期报告的次新股漏判
③ 扫雷财务表改为每行一个指标(原每行一个报告期10列, 模型看串列: 把"净利同比"一列
   当成"营收同比", 写出"茅台营收连续三期负增长"); 依据核对加查"数值安错指标"
④ 对提交15530bb的独立评审 + 自查发现的问题:
   AI失败后规则评级被藏起来 / 整数形式的数值被标存疑 / 末尾缺失复权因子时后复权
   断崖 / 解禁图小额显示0.0亿股 / 服务端单次上限更低时截断悄悄回来 / 增减持全量
   刷新漏2018年前 / 文本字段NaN落库 / "盘中快照"的错误前提 / 占比基数是流通股本
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import fetch_stock as FS   # noqa: E402
import stock_risk as SR    # noqa: E402

BASE = {"dedt_yi": 10.0, "or_yoy": 10.0, "gpm": 50.0, "roe": 10.0,
        "debt_to_assets": 40.0, "goodwill_eq": None}


def _m(*yoy, **over):
    """按给定的净利同比序列造指标行(其余字段取健康值, 可用over覆盖)"""
    rows = []
    for i, v in enumerate(yoy):
        r = dict(BASE)
        r.update(over)
        r["end_date"] = f"2026-{(i % 12) + 1:02d}-28"
        r["netprofit_yoy"] = v
        rows.append(r)
    return {"code": "X", "name": "t", "n": len(rows), "rows": rows}


def _yoy(r):
    return [(h["rule"], h["level"], h["score"]) for h in r["hits"] if "扣非净利同比" in h["rule"]]


# ════════════ ① 连续为负 ════════════

def test_mild_consecutive_decline_is_watch_not_high():
    """茅台实测: 扣非净利同比-1.95%、-0.25% — 原高危扣10分判C, 现关注扣5分判B"""
    r = SR.run_rules(_m(15.38, 11.56, 8.89, 6.25, -4.53, 1.47, -1.95, -0.25))
    assert _yoy(r) == [("扣非净利同比连续为负(-1.95%,-0.25%)", "中", -5)]
    assert (r["grade"], r["score"]) == ("B", -5)


def test_large_consecutive_decline_stays_high():
    """任一期降幅≥10%即高危: 中航高科/中航沈飞/河钢资源实测值不变;
    (-2%,-12%)正在恶化 — 按"两期平均"只有7%会被低估"""
    for prev, cur in ((-27.4368, -9.8234), (-58.372, -26.0047), (-149.244, -104.8276),
                      (-2.0, -12.0), (-12.0, -2.0)):
        r = SR.run_rules(_m(20.0, prev, cur))
        assert _yoy(r) == [(f"扣非净利同比连续为负({prev:.2f}%,{cur:.2f}%)", "高", -10)], (prev, cur)


def test_consecutive_decline_boundary():
    """门槛10个百分点(含等号); 西部矿业2023Q3实测(-6.10%,-7.67%)降为关注"""
    assert SR.run_rules(_m(20.0, -6.10, -7.67))["hits"][0]["level"] == "中"
    assert SR.run_rules(_m(20.0, -9.99, -3.0))["hits"][0]["level"] == "中"
    assert SR.run_rules(_m(20.0, -10.0, -3.0))["hits"][0]["level"] == "高"
    assert SR.run_rules(_m(20.0, -0.2, -0.1))["grade"] == "B"            # 仍算关注, 不是不命中


def test_consecutive_requires_only_two_reports():
    """两期报告皆负即命中 — 原要求存在第三期(与文档"需≥2期"不符), 次新股漏判"""
    assert _yoy(SR.run_rules(_m(-20.0, -25.0))) == [
        ("扣非净利同比连续为负(-20.00%,-25.00%)", "高", -10)]
    assert _yoy(SR.run_rules(_m(None, -20.0, -25.0)))[0][1] == "高"      # 第三期为空值不影响
    assert _yoy(SR.run_rules(_m(20.0, -5.0)))[0][0].startswith("扣非净利同比转负")   # 单期负不算连续


# ════════════ ② 由增转负 ════════════

def test_turn_negative_graded_by_drop():
    """回落幅度(上期-本期): ≥10高危 / ≥5关注 / 更小视为持平不命中(原一律高危)"""
    assert _yoy(SR.run_rules(_m(20.0, 1.4714, -1.9516))) == []          # 茅台2026Q2: 回落3.4
    assert _yoy(SR.run_rules(_m(20.0, 0.3, -0.2))) == []                # 原: 高危→C级
    r = SR.run_rules(_m(20.0, 3.0, -3.0))
    assert _yoy(r) == [("扣非净利同比转负(3.00%→-3.00%)", "中", -5)] and r["grade"] == "B"
    assert SR.run_rules(_m(20.0, 4.99, -5.0))["hits"][0]["level"] == "中"
    assert SR.run_rules(_m(20.0, 5.0, -5.0))["hits"][0]["level"] == "高"
    assert SR.run_rules(_m(20.0, 10.0, -5.0))["hits"][0]["level"] == "高"     # 原有行为不变
    assert SR.run_rules(_m(20.0, 0.5, -10.5))["hits"][0]["level"] == "高"     # 由平转大降不漏


def test_real_history_cases():
    """生产库回测里评级变化的3个(股票,报告期)组合; 之外的组合评级不变"""
    # 茅台 2026-06-30: 净利同比 +1.47%→-1.95%(原判"转负"高危) + 毛利率连降 → 原C(-15)
    gpm = [91.9312, 91.9736, 91.2993, 91.2934, 91.1796, 89.7592, 89.5552]
    yoy = [15.38, 11.56, 8.89, 6.25, -4.53, 1.47, -1.95]
    m = _m(*yoy)
    for row, g in zip(m["rows"], gpm):
        row["gpm"] = g
    r = SR.run_rules(m)
    assert [(h["rule"], h["level"]) for h in r["hits"]] == [("毛利率连续3期下滑", "中")]
    assert (r["grade"], r["score"]) == ("B", -5)
    # 西部矿业 2023-09-30: (-6.10%,-7.67%) 原C(-10)
    assert SR.run_rules(_m(96.0, -6.10, -7.67))["grade"] == "B"


def test_other_rules_and_ladder_untouched():
    """未涉及的规则、扣分制与评级档位不变(回归护栏)"""
    assert SR.run_rules(_m(15.0, 14.0, 13.0, 12.0)) == {"score": 0, "grade": "A", "hits": []}
    rev = SR.run_rules(_m(10.0, 10.0, 10.0, 10.0, or_yoy=-5.0))
    assert [(h["rule"], h["level"], h["score"]) for h in rev["hits"]] == [
        ("营收同比连续为负(-5.00%,-5.00%)", "中", -5)]
    assert SR.run_rules(_m(10.0, debt_to_assets=70.0))["grade"] == "A"
    assert SR.run_rules(_m(10.0, debt_to_assets=70.1))["grade"] == "B"
    assert SR.run_rules(_m(10.0, goodwill_eq=30.1))["grade"] == "C"
    assert SR.run_rules(_m(10.0, roe=-0.26, dedt_yi=-5.0, goodwill_eq=31.0))["grade"] == "D"


# ════════════ ③ 扫雷: 财务表横排 + "数值安错指标"核对 ════════════

_MAOTAI = {     # 茅台近8季真实数据(即prompt里喂给模型的)
    "dates": ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30",
              "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"],
    "revenue": [1708.99, 506.01, 893.89, 1284.54, 1688.38, 539.09, 907.03, 1309.71],
    "or_yoy": [15.712, 10.5415, 9.1032, 6.3569, -1.206, 6.538, 1.4699, 1.9595],
    "n_income": [893.35, 277.75, 469.87, 668.99, 853.1, 281.54, 460.33, 667.17],
    "net_yoy": [15.38, 11.5611, 8.8915, 6.2458, -4.5323, 1.4714, -1.9516, -0.2465],
    "gpm": [91.9312, 91.9736, 91.2993, 91.2934, 91.1796, 89.7592, 89.5552, 89.5811],
    "npm": [52.2734, 54.8895, 52.5641, 52.0801, 50.5279, 52.2245, 50.7516, 50.9405],
    "roe": [38.4283, 10.9255, 19.2486, 26.3688, 34.462, 10.5687, 17.9543, 24.995],
    "dta": [19.0448, 14.143, 14.7549, 12.8088, 16.4154, 12.1227, 15.1931, 11.9016],
    "ocf": [924.64, 88.09, 131.19, 381.97, 615.22, 269.1, 706.91, 840.59],
}
_VAL = {"dates": ["2026-11-21", "2026-11-23"], "pe": [22.3, 21.9], "pb": [6.74, 6.63]}
# 茅台实测输出: 数字全是"净利同比"一列的, 却说成"营收同比"(其营收同比为正)
_WRONG = ("营收同比从15.38%(2024Q4)→-4.53%(2025Q4)→-1.95%(2026H1)→-0.25%(2026Q3)，"
          "连续三期负增长")


def _full():
    return {"dates": list(_MAOTAI["dates"]),
            "profit": {k: list(_MAOTAI[k]) for k in ("revenue", "or_yoy", "n_income",
                                                      "net_yoy", "gpm", "npm", "roe")},
            "cashflow": {"ocf": list(_MAOTAI["ocf"])},
            "balance": {"gw_eq": [None] * 8, "dta": list(_MAOTAI["dta"])},
            "inst": {"dates": _MAOTAI["dates"][4:],
                     "ratio": [8.66, 7.34, 5.61, 5.76],
                     "holder_num": [255892, 243159, 296404, 278364]},
            "pledge_ratio": 0.15}


def _series():
    return SR._metric_series(_full(), _VAL)


def test_fin_table_is_metric_per_row():
    """财务表每行一个指标(原每行一个报告期、10列; 模型读趋势要竖着看, 实测看串列);
    营收同比原表里没有而规则引擎在用"""
    lines = SR._fin_table(_full())
    assert lines[0] == ("  报告期 | 2024-12-31 | 2025-03-31 | 2025-06-30 | 2025-09-30 | "
                        "2025-12-31 | 2026-03-31 | 2026-06-30 | 2026-09-30")
    assert "  营收同比(%) | 15.712 | 10.5415 | 9.1032 | 6.3569 | -1.206 | 6.538 | 1.4699 | 1.9595" in lines
    assert ("  净利同比(%) | 15.38 | 11.5611 | 8.8915 | 6.2458 | -4.5323 | 1.4714 | "
            "-1.9516 | -0.2465") in lines
    assert "  商誉占净资产(%) | -- | -- | -- | -- | -- | -- | -- | --" in lines
    assert len(lines) == 11 and all(ln.count(" | ") == 8 for ln in lines)   # 每行恰8个报告期
    assert not any("None" in ln or "nan" in ln for ln in lines)


def test_misattributed_numbers_flagged():
    """依据点名的指标一个数字都对不上, 而同一子句有≥2个数字属于另一个指标 → 存疑。
    这些数值都能在prompt里找到, 原口径(只查数值是否出现)拦不住"""
    series = _series()
    prompt_nums = SR._key_nums("\n".join(SR._fin_table(_full())))
    assert SR._grounded(_WRONG, prompt_nums) is True            # 原口径: 有据, 放行
    assert SR._misattributed(_WRONG, series) is True
    out = SR._validate_ai_points(
        {"points": [{"title": "营收增速连续转负", "evidence": _WRONG, "dimension": "财务"}]},
        {"rows": []}, " ".join(SR._fin_table(_full())), series)
    assert out["points"][0]["suspect"] is True
    # 同一句话改对指标名 → 不标
    right = _WRONG.replace("营收同比", "净利同比")
    assert SR._misattributed(right, series) is False
    assert SR._misattributed("营收同比从15.38%→-0.25%", series) is True   # 恰好2个数字(判定下限)


def test_misattribution_check_spares_legit_evidence():
    """真实报告里的正确依据(含模型自己算的推导值、一句话带两个指标)不得被误标"""
    series = _series()
    legit = [
        "净利同比：2025-12-31为-4.53%，2026-06-30为-1.95%，2026-09-30为-0.25%，连续三期负增长",
        "毛利率从2024-12-31的91.9312%降至2026-09-30的89.5811%，连续8季下行",
        "机构持仓近4期：8.66%→7.34%→5.61%→5.76%，累计下降2.9pct，降幅约33%",
        "股东户数近4期：25.6万→24.3万→29.6万→27.8万，中间一度升至29.6万",
        "PE_TTM 21.9倍对应净利同比-0.25%",
        "ROE从2024-12-31的38.4283%降至2026-09-30的24.995%，降幅约13.4pct",
        "营收同比从15.712%(2024Q4)降至1.9595%(2026Q3)，增速持续放缓",       # 这次是对的
        "净利率从52.2734%降至50.9405%，毛利率从91.9312%降至89.5811%",      # 一句两个指标
    ]
    bad = [e for e in legit if SR._misattributed(e, series)]
    assert not bad, bad
    # 别的股票的真实依据 + 各自的序列
    dx = {"经营现金流": [23.84, 35.15], "净利": [29.85, 53.15],
          "资产负债率": [56.2927, 58.8226, 63.9807, 63.6888, 64.5077, 65.0411]}
    assert not SR._misattributed(
        "2026Q3 OCF 35.15亿 vs 净利 53.15亿，OCF/净利仅0.66；2026H1 OCF 23.84 vs 净利 29.85，比值0.80", dx)
    assert not SR._misattributed("负债率：56.2927→58.8226→63.9807→63.6888→64.5077→65.0411", dx)
    xy = {"经营现金流": [46.37, 14.64, 16.16], "净利": [63.27, 114.67]}
    assert not SR._misattributed("同期2025Q3单季OCF达36.84亿", xy)         # 单个推导值: 不判


def test_scan_ai_flags_misattributed_point(monkeypatch):
    """主流程: 模型返回看串列的那条 → 该点被标存疑; 证据正确的点不标; prompt用横排表"""
    import ai_client
    cap = {}

    def fake(url, model, msgs, **kw):
        cap["prompt"] = msgs[0]["content"]
        return json.dumps({"summary": "x", "advice": "", "points": [
            {"title": "营收增速连续转负", "evidence": _WRONG, "severity": "中", "dimension": "财务"},
            {"title": "净利同比连续为负", "severity": "中", "dimension": "财务",
             "evidence": "净利同比-1.95%(2026H1)、-0.25%(2026Q3)，连续两期为负"}]})
    monkeypatch.setattr(ai_client, "chat", fake)
    full = _full()
    out = SR._scan_ai("600519.SH", {"name": "贵州茅台", "rows": []},
                      {"full": full, "val": _VAL, "events": [], "chg_1y": 3.2},
                      {"hits": [{"level": "中", "rule": "扣非净利同比连续为负(-1.95%,-0.25%)"}]},
                      {"base_url": "u", "model": "m", "api_key": "k"})
    assert [p["suspect"] for p in out["points"]] == [True, False]
    p = cap["prompt"]
    assert "每行一个指标" in p and "写明是哪个指标哪个报告期" in p
    assert "  净利同比(%) | 15.38 | 11.5611 | 8.8915 | 6.2458 | -4.5323 | 1.4714 | -1.9516 | -0.2465" in p


# ════════════ ④ 独立评审与自查的发现 ════════════

def _env_db(tmp_path, monkeypatch):
    db = tmp_path / "stock.db"
    monkeypatch.setattr(FS, "STOCK_DB", db)
    monkeypatch.setattr(SR, "_conn", lambda path=None: FS._conn(path))
    return db


def _stub_ai(monkeypatch, reply=None):
    import ai_client
    cap = {}

    def fake(url, model, msgs, **kw):
        cap["prompt"] = msgs[0]["content"]
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(ai_client, "chat", fake)
    return cap


def _seed_fin(db):
    """两季财务: 第二季净利同比-30% → 命中"由增转负"(高危) → 规则评级C"""
    con = FS._conn(db)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','贵州茅台','白酒','t','t')")
    con.executemany(
        "INSERT INTO stock_finance(ts_code,end_date,ann_date,profit_dedt,netprofit_yoy,"
        "or_yoy,grossprofit_margin,roe,debt_to_assets) VALUES(?,?,?,?,?,?,?,?,?)",
        [("600519.SH", "2026-06-30", "2026-08-15", 2e9, 5.0, 8.0, 91.0, 15.0, 30.0),
         ("600519.SH", "2026-09-30", "2026-10-28", 2.2e9, -30.0, 8.0, 91.0, 15.0, 30.0)])
    con.commit()
    con.close()


def test_ai_failure_keeps_rule_grade_readable(tmp_path, monkeypatch):
    """评审#1: AI没跑成(模型不可用/返回无法解析)时规则评级有效 — 页面只读接口仍要
    返回它并标 degraded。15530bb把它按"缓存无效"处理: 点[一键扫雷]当场能看到评级,
    一刷新就变成"尚无扫雷结果", 评级被藏了起来(改动前刷新还能看到)"""
    db = _env_db(tmp_path, monkeypatch)
    _seed_fin(db)
    import analyze_news as AN
    monkeypatch.setattr(AN, "get_ai_config", lambda: {
        "enabled": True, "model": "m", "api_key": "", "base_url": "http://x"})
    _stub_ai(monkeypatch, RuntimeError("ollama down"))
    r = SR.scan_stock("600519", db, force=True)
    assert r["ok"] and r["degraded"] is True and r["grade"] == "C"   # 规则评级照出
    again = SR.scan_stock("600519", db, cached_only=True)
    assert again["ok"] and again["cached"] and again["degraded"] and again["grade"] == "C"
    assert "一键扫雷" in again["msg"] and "失败" in again["ai"]["summary"]
    assert again["rules"] == r["rules"] and "_degraded" not in again["ai"]
    # 模型返回无法解析的内容: 同样是降级结果
    _stub_ai(monkeypatch, "这不是JSON")
    assert SR.scan_stock("600519", db, force=True)["degraded"] is True
    assert SR.scan_stock("600519", db, cached_only=True)["degraded"] is True
    # 再点[一键扫雷](非只读): 重跑AI; 成功后才是有效缓存
    _stub_ai(monkeypatch, json.dumps({"summary": "s", "advice": "a", "points": [
        {"title": "t", "evidence": "扣非净利同比-30.00%", "severity": "高", "dimension": "财务"}]}))
    r3 = SR.scan_stock("600519", db)
    assert r3["cached"] is False and "degraded" not in r3 and r3["ai"]["summary"] == "s"
    r4 = SR.scan_stock("600519", db, cached_only=True)
    assert r4["cached"] is True and "degraded" not in r4


def test_integer_form_numbers_not_flagged():
    """评审#2: 模型把数量写成整数("减持127万股", prompt里是"127.0万股")时, 原被当成
    "没有数值的定性依据", 不含财务指标名就标存疑 — 与15530bb要消除的误标同一类"""
    prompt = ("  - [mid]2026-10-29 某股东减持127.0万股(变动占比0.0877%)\n"
              "  - [high]2026-07-15 业绩预告: 首亏(2026-06-30报告期 预计变动-80.0%~-70.0%)\n"
              "  营收(亿) | 300.0 | 310.5\n  净利(亿) | 150.0 | 160.2\n")
    evidences = ["2026-10-29某股东减持127万股",               # 整数 + 日期(日期不计)
                 "营收300亿, 净利150亿",
                 "业绩预告首亏, 预计变动-80%~-70%",
                 "某股东减持999万股",                         # prompt里没有: 仍标存疑
                 "近一年无重大诉讼"]                           # 纯定性且无指标名: 原口径
    out = SR._validate_ai_points({"points": [{"evidence": e} for e in evidences]},
                                 {"rows": []}, prompt)
    assert [p["suspect"] for p in out["points"]] == [False, False, False, True, True]
    assert SR._int_nums("2026-11-18与2025年报、2026Q3: 减持12万股, 涨2%") == [12.0]


def test_kline_trailing_missing_factor_no_cliff(tmp_path, monkeypatch):
    """评审#3: 最新几天的复权因子还没到(日线先入库)时, 原按1.0处理 → 后复权在最后
    一根K线上断崖(前复权因归一到最新已知因子不受影响)"""
    db = _env_db(tmp_path, monkeypatch)
    con = FS._conn(db)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','x','','t','t')")
    for i, fac in enumerate([2.0, 2.0, None, None]):      # 收盘价 10/11/12/13
        con.execute("INSERT INTO stock_daily VALUES('600519.SH',?,10,10,10,?,100,1,?)",
                    ("2026-01-0%d" % (i + 1), 10.0 + i, fac))
    con.commit()
    con.close()
    assert FS.kline("600519", "hfq", path=db)["closes"] == [20.0, 22.0, 24.0, 26.0]   # 原: [20,22,12,13]
    assert FS.kline("600519", "qfq", path=db)["closes"] == [10.0, 11.0, 12.0, 13.0]


def test_float_payload_formats_small_unlocks(tmp_path, monkeypatch):
    """评审#5: 小额解禁在解禁图的提示与"未来90天"汇总里仍显示"0.0亿股"(15530bb只修了
    事件标题; 生产库1122条解禁里644条如此); 柱高也不再被舍入成0"""
    db = _env_db(tmp_path, monkeypatch)
    soon1 = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    soon2 = (dt.date.today() + dt.timedelta(days=60)).isoformat()
    con = FS._conn(db)
    con.execute("INSERT INTO stock_pool VALUES('002384.SZ','x','','t','t')")
    con.executemany("INSERT INTO stock_float VALUES(?,?,?,?,?,?)", [
        ("002384.SZ", None, "2020-01-01", 61000, 0.0061, "钱雪松"),
        ("002384.SZ", None, soon1, 106400, 0.0038, "张绍卓"),
        ("002384.SZ", None, soon2, 1.01e8, 5.49, "袁永刚")])
    con.commit()
    con.close()
    p = FS.float_payload("002384", db)
    d = {x["date"]: x for x in p["data"]}
    assert d["2020-01-01"]["share_txt"] == "6.10万股"
    assert d["2020-01-01"]["share_yi"] == 61000 / 1e8              # 原 round(…, 2) = 0.0
    assert d[soon1]["share_txt"] == "10.64万股" and d[soon2]["share_txt"] == "1.01亿股"
    assert p["soon"]["n"] == 2 and p["soon"]["total_txt"] == "1.01亿股"
    assert p["soon"]["max_ratio"] == 5.49


def test_pull_all_adapts_to_lower_server_cap(monkeypatch):
    """评审#6: 服务端单次上限低于请求值(积分档位不同)时首页就不足6000行 — 原按
    "不足请求值即最后一页"在第一页后停止, 截断悄悄回来"""
    row = {"trade_date": "20260101", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
           "vol": 1, "amount": 1, "adj_factor": 1.0}
    rows = [row] * 12000
    offs = []

    def fake(api, fields="", **kw):
        off = kw.get("offset") or 0
        offs.append(off)
        return pd.DataFrame(rows[off:off + 4000])       # 服务端实际每次只给4000行
    monkeypatch.setattr(FS.T, "call", fake)
    assert len(FS._pull_all("daily", "f", "600519.SH")) == 12000      # 原: 4000
    assert offs == [0, 4000, 8000, 12000]                              # 末尾多一次空页确认


def test_holdertrade_full_refresh_pulls_all_history(tmp_path, monkeypatch):
    """评审#7: 全量刷新的增减持写死自2018-01-01起, 而接口有更早的数据(茅台最早2005-12、
    五粮液2002-08) — 池内9只共148条真实股东的历史增减持没拉; 增量仍按窗口"""
    db = _env_db(tmp_path, monkeypatch)
    starts = []

    def fake(api, fields="", **kw):
        if api == "stk_holdertrade":
            starts.append(kw.get("start_date"))
            return pd.DataFrame([
                {"ts_code": "X", "ann_date": "20051221", "holder_type": "C", "in_de": "DE",
                 "holder_name": "中国贵州茅台酒厂有限责任公司", "change_vol": 5.0e6,
                 "change_ratio": 0.5}])
        return pd.DataFrame()
    monkeypatch.setattr(FS.T, "call", fake)
    con = FS._conn(db)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','x','','t','t')")
    con.commit()
    con.close()
    FS.refresh_events("600519", db)                               # 全量 → 自1990起
    ev = FS.events_payload("600519", db)["events"]
    assert ev[0]["date"] == "2005-12-21" and ev[0]["risk"] == "mid"
    FS.refresh_events("600519", db, lookback=30)
    assert starts == ["19900000", (dt.date.today() - dt.timedelta(days=30)).strftime("%Y%m%d")]


def test_nan_text_fields_do_not_leak(tmp_path, monkeypatch):
    """评审#8: 预告类型/股东名/复牌日为NaN时不落库成"nan"(pandas的NaN是truthy,
    `v or 默认值`挡不住)"""
    nan = float("nan")

    def fake(api, fields="", **kw):
        if api == "forecast":
            return pd.DataFrame([{"ts_code": "X", "ann_date": "20260715", "end_date": "20260630",
                                  "type": nan, "p_change_min": -80, "p_change_max": -70}])
        if api == "stk_holdertrade":
            return pd.DataFrame([{"ts_code": "X", "ann_date": "20261009", "holder_name": nan,
                                  "holder_type": "C", "in_de": "DE", "change_vol": 4.8e6,
                                  "change_ratio": 0.5}])
        if api == "suspend":
            return pd.DataFrame([{"ts_code": "X", "suspend_date": "20260105", "resume_date": nan}])
        if api == "share_float":
            return pd.DataFrame([{"ts_code": "X", "ann_date": None, "float_date": "20270426",
                                  "float_share": 106400, "float_ratio": 0.0038,
                                  "holder_name": nan}])
        return pd.DataFrame()
    monkeypatch.setattr(FS.T, "call", fake)
    db = _env_db(tmp_path, monkeypatch)
    FS.refresh_events("600519", db)
    FS.refresh_float("600519", db)
    ev = FS.events_payload("600519", db)["events"]
    text = " ".join(e["title"] + " " + (e["detail"] or "") for e in ev)
    assert "nan" not in text.lower() and "None" not in text
    titles = {e["title"]: e["detail"] for e in ev}
    assert titles["业绩预告: 预告"] == "2026-06-30报告期 预计变动-80.0%~-70.0%"
    assert titles["未知股东减持480.0万股"] == "变动占比0.5%"
    assert titles["停牌"] == "复牌日期未定"
    assert titles["未知股东解禁10.64万股(0.0038%)"] == "解禁股东: 未知股东"
    assert FS._text(None, "d") == FS._text(nan, "d") == FS._text(" nan ", "d") == "d"
    assert FS._text("  中国平安 ", "d") == "中国平安"
