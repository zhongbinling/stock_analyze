# -*- coding: utf-8 -*-
"""D1-35 避雷针体检回归 — 对照生产 stock.db 与真实接口数据发现的问题

① 日线全史被接口单次6000行上限截断(池内5只老股日线始于2001年前后, 复权
   因子同步被截 → 早期adj_factor为空, 前复权价成原始价)
② 池日增upsert不更新开高低(盘中快照永不纠正)
③ 增减持方向: change_vol恒为正, 方向在in_de — 全库668条增减持事件全是"增持"
④ 事件只增不删(19组同日同股东新旧两行并存); 增持被当风险事件
⑤ 股东表户数沿用值被当真实值 → "+0.0% 筹码分散⚠"假预警
⑥ 财务全景69期而营收只有12期; 扫雷财务表73行多为None
⑦ 扫雷筹码栏取最早一期(2003年); 事件取最早12条; 依据存疑误标40/65
⑧ 读缓存的GET在无缓存时直接跑AI; 旧口径缓存不失效
⑨ 周频链/全量重拉漏利润表与现金流量表; 利润表缺失时页面不自动补拉
⑩ 页面: 快速切股时旧响应覆盖新股票; 切股后上一只的表格/图留在页面上; 股东户数
   大1万倍; 沿用户数算出"+0.0% 筹码分散"假预警; 未来事件堆在最后一根K线上;
   resize监听越点越多; 池chips第13只起点不到; "刚更新免确认"跨股票串用
⑪ 新口径重扫9只后暴露: 减持风险只看绝对量(≥50万股即中风险), 龙虎榜席位的小额
   卖出被AI写成"[高]机构集中大额减持加速"; prompt事件行只有标题没有占比;
   解禁小额显示"0.0亿股"; 解禁事件只增不删; 事件文案漏进nan/None
"""
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
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
    return db


def _seed_pool(env, code="600519.SH"):
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES(?,?,'','t','t')", (code, "测试"))
    con.commit()
    con.close()


def _events(env, code="600519.SH", ev_type=None):
    con = FS._conn(env)
    q = "SELECT ev_date, ev_type, title, risk FROM stock_events WHERE ts_code=?"
    args = [code]
    if ev_type:
        q += " AND ev_type=?"
        args.append(ev_type)
    rows = con.execute(q + " ORDER BY ev_date", args).fetchall()
    con.close()
    return rows


# ════════════ ① 全史分页 ════════════

def _fake_paged(monkeypatch, n_rows=13000):
    """tushare替身: 严格按 limit/offset 切页(与真实接口的单次行数上限一致)"""
    calls = []
    base = dt.date(2000, 1, 1)
    allrows = [{"trade_date": (base + dt.timedelta(days=i)).strftime("%Y%m%d"),
                "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                "vol": 100, "amount": 1000, "adj_factor": 1.0 + i * 0.001}
               for i in range(n_rows)]

    def fake(api, fields="", **kw):
        if api == "stock_basic":
            return pd.DataFrame([{"ts_code": "600519.SH", "name": "茅台", "industry": "白酒"}])
        off = kw.get("offset") or 0
        calls.append((api, kw.get("limit"), off))
        return pd.DataFrame(allrows[off:off + FS._PAGE_ROWS])
    monkeypatch.setattr(FS.T, "has_token", lambda: True)
    monkeypatch.setattr(FS.T, "call", fake)
    return calls


def test_add_stock_full_history_pagination(env, monkeypatch):
    """全史分页: 超6000行继续翻页直到取完; 复权因子同口径分页 → 无空因子"""
    calls = _fake_paged(monkeypatch, 13000)
    r = FS.add_stock("600519", env)
    assert r["ok"], r
    assert calls == [("daily", 6000, 0), ("daily", 6000, 6000), ("daily", 6000, 12000),
                    ("adj_factor", 6000, 0), ("adj_factor", 6000, 6000),
                    ("adj_factor", 6000, 12000)]      # 第三页1000行<6000即停
    con = FS._conn(env)
    n, lo, hi = con.execute(
        "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM stock_daily").fetchone()
    nulls = con.execute("SELECT COUNT(*) FROM stock_daily WHERE adj_factor IS NULL").fetchone()[0]
    con.close()
    last = (dt.date(2000, 1, 1) + dt.timedelta(days=12999)).isoformat()
    assert (n, lo, hi) == (13000, "2000-01-01", last)
    assert nulls == 0                          # 旧版: 两边各截6000行, 末端几百行因子为空


def test_add_stock_short_history_single_call(env, monkeypatch):
    calls = _fake_paged(monkeypatch, 50)
    assert FS.add_stock("600519", env)["ok"]
    assert calls == [("daily", 6000, 0), ("adj_factor", 6000, 0)]   # 不足一页不翻页


# ════════════ ② 池日增纠正盘中快照 ════════════

def test_refresh_daily_corrects_intraday_snapshot(env, monkeypatch):
    """盘中点[池日增]写入的当日快照(高价/量是盘中的) → 之后的日增把开高低一并纠正。
    旧版upsert只更新close/vol/amount/复权因子, 当天高低价永久停在盘中值"""
    _seed_pool(env)
    con = FS._conn(env)
    con.execute("INSERT INTO stock_daily VALUES('600519.SH','2026-11-20',10,10.3,9.9,10.2,120,1300,1.0)")
    con.commit()
    con.close()
    final = pd.DataFrame([{"trade_date": "20261120", "open": 10.0, "high": 11.2,
                           "low": 9.6, "close": 10.9, "vol": 500, "amount": 5200,
                           "adj_factor": 1.0}])
    monkeypatch.setattr(FS.T, "has_token", lambda: True)
    monkeypatch.setattr(FS.T, "call", lambda api, fields="", **kw: final)
    assert FS.refresh_daily(7, env) == 1
    con = FS._conn(env)
    row = con.execute("SELECT open, high, low, close, vol FROM stock_daily").fetchone()
    con.close()
    assert row == (10.0, 11.2, 9.6, 10.9, 500.0)      # 全列纠正(旧版high停在10.3)


# ════════════ ① 复权因子缺失的前复权连续性 ════════════

def test_refresh_val_full_history_paginated(env, monkeypatch):
    """估值全量拉取分页 — daily_basic与daily同为6000行上限: 中航高科K线自1994-05,
    估值库却只从1999-11开始(恰6000行)。增量(lookback)仍单次调用不带翻页"""
    calls = _fake_paged(monkeypatch)
    _seed_pool(env)
    assert FS.refresh_val("600519", env) == 13000
    assert [c for c in calls if c[0] == "daily_basic"] == [
        ("daily_basic", 6000, 0), ("daily_basic", 6000, 6000), ("daily_basic", 6000, 12000)]
    con = FS._conn(env)
    assert con.execute("SELECT MIN(trade_date) FROM stock_val").fetchone()[0] == "2000-01-01"
    con.close()
    calls.clear()
    assert FS.refresh_val("600519", env, lookback=45) == 6000       # 替身固定按页长切片
    assert [c for c in calls if c[0] == "daily_basic"] == [("daily_basic", None, 0)]


def test_kline_missing_adj_factor_stays_continuous(env):
    """库里遗留的空复权因子行(历史上被截的那一段): 顺延取其后首个已知因子 —
    旧版按1.0处理, 这段前复权价直接成原始价(实测茅台2002-02-28为38.00,
    次日4.32)"""
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('600519.SH','茅台','','t','t')")
    for d, c, f in (("2026-01-05", 10.0, None), ("2026-01-06", 11.0, None),
                    ("2026-01-07", 24.0, 2.0), ("2026-01-08", 26.0, 4.0)):
        con.execute("INSERT INTO stock_daily VALUES('600519.SH',?,1,1,1,?,1,1,?)", (d, c, f))
    con.commit()
    con.close()
    k = FS.kline("600519", "qfq", env)
    assert k["closes"] == [5.0, 5.5, 12.0, 26.0]     # 旧版: [10.0, 11.0, 12.0, 26.0]
    assert FS.kline("600519", "none", env)["closes"] == [10.0, 11.0, 24.0, 26.0]


# ════════════ ③ 增减持方向(真实接口口径) ════════════

TRADE_ROWS = [
    {"ts_code": "X", "ann_date": "20261020", "holder_name": "大股东A公司", "holder_type": "G",
     "in_de": "DE", "change_vol": 8.0e6, "change_ratio": 2.5},     # 减持800万股 → 高
    {"ts_code": "X", "ann_date": "20261021", "holder_name": "某股东B", "holder_type": "P",
     "in_de": "DE", "change_vol": 1.0e6, "change_ratio": 0.2},     # 减持100万股 → 中
    {"ts_code": "X", "ann_date": "20261022", "holder_name": "北向资金C", "holder_type": "P",
     "in_de": "IN", "change_vol": 600000, "change_ratio": 0.3},    # 增持60.00万股 → 低
    {"ts_code": "X", "ann_date": "20261023", "holder_name": "机构专用", "holder_type": "P",
     "in_de": "DE", "change_vol": 5300, "change_ratio": 0.0004},   # 5300股 → 噪音滤除
    {"ts_code": "X", "ann_date": "20261024", "holder_name": "无方向字段", "holder_type": "G",
     "change_vol": -3.0e6, "change_ratio": -1.0},                  # 旧口径: 负号兜底 → 减持
]


def test_holdertrade_direction_from_in_de(env, monkeypatch):
    """change_vol恒为正, 方向在in_de — 旧版按vol<0判减持: 实测生产库668条全是"增持"。
    增持不属风险(low); 小额保留两位小数; 名称不截在括号中间"""
    def fake(api, fields="", **kw):
        if api == "stk_holdertrade":
            assert "in_de" in fields, "未请求方向字段"
            return pd.DataFrame(TRADE_ROWS)
        return pd.DataFrame()
    monkeypatch.setattr(FS.T, "call", fake)
    FS.refresh_events("600519", env)
    ev = {(e["title"], e["risk"]) for e in FS.events_payload("600519", env)["events"]}
    assert ("大股东A公司减持800.0万股", "high") in ev
    assert ("某股东B减持100.0万股", "mid") in ev
    assert ("北向资金C增持60.00万股", "low") in ev          # 增持=低风险
    assert ("无方向字段减持300.0万股", "high") in ev         # 无in_de的旧行: 负号兜底
    assert not any("机构专用" in t for t, _r in ev)         # 5300股噪音
    assert not any(t.endswith("(") for t, _r in ev)         # 名称不以半个括号结尾


def test_iso_normalizes_float_formatted_dates():
    """含空值的整数日期列被pandas整体读成浮点(20261107.0) — 归一入口不能原样放行
    (各日期列全走_iso; 漏掉的会以"20261107.0"落库, 之后的日期比较/未来解禁判断全失效)"""
    assert FS._iso(20261107.0) == "2026-11-07"
    assert FS._iso("20261107.0") == "2026-11-07"
    assert FS._iso(20261107) == "2026-11-07"
    assert FS._iso("2026-11-07") == "2026-11-07"
    assert FS._iso(None) == ""
    assert FS._iso(float("nan")) == "nan"          # 脏值由调用方按"无日期"处理


def test_holdertrade_long_name_not_cut_mid_parenthesis():
    assert FS._clean_holder_name("中信证券股份有限公司总部(非营业场所)") == \
        "中信证券股份有限公司总部(非营业场所)"
    assert FS._clean_holder_name("某" * 22 + "(有限合伙)") == "某" * 22            # 超长截断后不带半个括号
    assert FS._fmt_shares(123400) == "12.34万股"
    assert FS._fmt_shares(8.0e6) == "800.0万股"


# ════════════ ④ 事件旧行替换 ════════════

def _seed_stale(env):
    con = FS._conn(env)
    con.executemany("INSERT INTO stock_events VALUES(?,?,?,?,?,?)", [
        ("600519.SH", "2026-11-06", "holdertrade", "旧口径增持729304.0万股", "", "mid"),
        ("600519.SH", "2025-01-01", "holdertrade", "一年前的旧事件", "", "mid"),
        ("600519.SH", "2025-01-01", "dividend", "分红: 旧口径", "", "low"),
        ("600519.SH", "2020-01-01", "suspend", "停牌", "", "mid"),
    ])
    con.commit()
    con.close()


def test_refresh_events_replaces_stale_rows(env, monkeypatch):
    """全量刷新: 拉取成功的类型清旧行重写; 接口失败/返回空的类型不动(防抖动清库)"""
    _seed_stale(env)

    def fake(api, fields="", **kw):
        if api == "stk_holdertrade":
            return pd.DataFrame([TRADE_ROWS[0]])
        if api == "dividend":
            raise RuntimeError("接口限频")
        return pd.DataFrame()                    # forecast/suspend 返回空
    monkeypatch.setattr(FS.T, "call", fake)
    FS.refresh_events("600519", env)
    titles = {(r[1], r[2]) for r in _events(env)}
    assert ("holdertrade", "旧口径增持729304.0万股") not in titles   # 旧行清除
    assert ("holdertrade", "大股东A公司减持800.0万股") in titles
    assert ("holdertrade", "一年前的旧事件") not in titles          # 全量: 清该类型全部
    assert ("dividend", "分红: 旧口径") in titles                  # 接口失败 → 不动
    assert ("suspend", "停牌") in titles                           # 返回空 → 不动


def test_refresh_events_incremental_only_touches_window(env, monkeypatch):
    """增量刷新(夜间链lookback=30): 只替换窗口内的行, 更早的保留"""
    _seed_stale(env)
    monkeypatch.setattr(FS.T, "call", lambda api, fields="", **kw: pd.DataFrame())
    FS.refresh_events("600519", env, lookback=30)
    recent = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    titles = {r[2] for r in _events(env, ev_type="holdertrade")}
    if recent <= "2026-11-06":                 # 窗口覆盖该日时被清, 否则保留
        assert "旧口径增持729304.0万股" not in titles
    else:
        assert "旧口径增持729304.0万股" in titles
    assert "一年前的旧事件" in titles            # 窗口外不动
    all_titles = {r[2] for r in _events(env)}
    assert {"分红: 旧口径", "停牌"} <= all_titles  # 非窗口型返回空 → 不动


def test_refresh_events_skips_rows_without_date(env, monkeypatch):
    """无公告日/无停牌日的脏行不入库(原样落库后ev_date为NULL, 每次刷新重复累积)"""
    def fake(api, fields="", **kw):
        if api == "forecast":
            return pd.DataFrame([{"ts_code": "X", "ann_date": None, "end_date": "20251231",
                                 "type": "首亏", "p_change_min": -80, "p_change_max": -70}])
        if api == "suspend":
            return pd.DataFrame([{"ts_code": "X", "suspend_date": float("nan"),
                                 "resume_date": "20200310"}])
        return pd.DataFrame()
    monkeypatch.setattr(FS.T, "call", fake)
    assert FS.refresh_events("600519", env) == 0
    assert _events(env) == []


def test_refresh_events_text_without_null_leaks(env, monkeypatch):
    """空值不得漏进事件文案(事件表与扫雷prompt直接显示) — 对照生产库: 预告
    "预计变动nan%~nan%"82行 / 增减持"变动占比None%"4行 / 只送转"每10股派None元 送N股"9行"""
    nan = float("nan")

    def fake(api, fields="", **kw):
        if api == "forecast":
            return pd.DataFrame([
                {"ts_code": "X", "ann_date": "20161026", "end_date": "20161231", "type": "不确定",
                 "p_change_min": nan, "p_change_max": None},
                {"ts_code": "X", "ann_date": "20260715", "end_date": "20260630", "type": "首亏",
                 "p_change_min": -80, "p_change_max": -70},
                {"ts_code": "X", "ann_date": "20260716", "end_date": nan, "type": "预增",
                 "p_change_min": 30, "p_change_max": 40}])            # 无报告期: 脏行不入库
        if api == "dividend":
            return pd.DataFrame([
                {"ts_code": "X", "ann_date": "19960530", "end_date": "19951231",
                 "cash_div": nan, "stk_div": 0.2},
                {"ts_code": "X", "ann_date": "20260620", "end_date": "20251231",
                 "cash_div": 21.5, "stk_div": 0.0},
                {"ts_code": "X", "ann_date": "20260621", "end_date": "20251231",
                 "cash_div": 3.0, "stk_div": 1.0},
                {"ts_code": "X", "ann_date": "20260622", "end_date": None,
                 "cash_div": 1.0, "stk_div": nan}])
        if api == "stk_holdertrade":
            return pd.DataFrame([
                {"ts_code": "X", "ann_date": "20261009", "holder_name": "青海西矿资产管理有限公司",
                 "holder_type": "C", "in_de": "IN", "change_vol": 47660000, "change_ratio": None}])
        return pd.DataFrame()
    monkeypatch.setattr(FS.T, "call", fake)
    assert FS.refresh_events("600519", env) == 7
    ev = FS.events_payload("600519", env)["events"]
    leaked = [e for e in ev if "nan" in e["title"] + e["detail"] or "None" in e["title"] + e["detail"]]
    assert not leaked, leaked
    by = {(e["type"], e["title"]): e["detail"] for e in ev}
    assert len(ev) == 7 and not any("预增" in e["title"] for e in ev)   # 无报告期的预告不入库
    assert by[("业绩预告", "业绩预告: 不确定")] == "2016-12-31报告期 未给出变动幅度"
    assert by[("业绩预告", "业绩预告: 首亏")] == "2026-06-30报告期 预计变动-80.0%~-70.0%"
    assert by[("分红", "分红: 每10股送0.2股")] == "1995-12-31年度分配"
    assert by[("分红", "分红: 每10股派21.5元")] == "2025-12-31年度分配"
    assert by[("分红", "分红: 每10股派3.0元 送1.0股")] == "2025-12-31年度分配"   # 派息+送转格式不变
    assert by[("分红", "分红: 每10股派1.0元")] == "分配方案(报告期未注明)"
    assert by[("增减持", "青海西矿资产管理有限公司增持4766.0万股")] == "变动占比未披露"


# ════════════ ⑥ 财务全景窗口 ════════════

def test_finance_full_payload_limited_to_n_qua(env):
    """近N季: 原把利润表(近N季)与财务表全史期数直接并集 — 茅台实测69期而营收只有12期"""
    con = FS._conn(env)
    fin_dates = [f"{y}-{m}" for y in range(2019, 2027) for m in ("03-31", "06-30", "09-30", "12-31")][:30]
    for d in fin_dates:
        con.execute("INSERT INTO stock_finance(ts_code,end_date,profit_dedt,netprofit_yoy) "
                    "VALUES('600519.SH',?,1e9,5.0)", (d,))
    for d in ("2026-03-31", "2026-06-30", "2026-09-30"):
        con.execute("INSERT INTO stock_income VALUES('600519.SH',?,?,?,?)", (d, 300e8, 300e8, 150e8))
    con.commit()
    con.close()
    p = FS.finance_full_payload("600519", env)
    assert len(p["dates"]) == 12 and p["dates"][-1] == "2026-09-30"
    assert p["dates"][0] == "2023-12-31"         # 并集的最后12期
    assert sum(1 for v in p["profit"]["revenue"] if v is not None) == 3
    s = FS.finance_full_payload("600519", env, n_qua=8)       # 扫雷口径
    assert len(s["dates"]) == 8 and len(s["profit"]["revenue"]) == 8


# ════════════ ⑦ 扫雷输入 ════════════

def _stub_ai(monkeypatch, reply=None):
    import ai_client
    cap = {}

    def fake(url, model, msgs, **kw):
        cap["prompt"] = msgs[0]["content"]
        cap["kw"] = kw
        if isinstance(reply, Exception):
            raise reply
        return reply or json.dumps({"summary": "x", "points": [], "advice": ""})
    monkeypatch.setattr(ai_client, "chat", fake)
    return cap


def _inp(ratio, hnum, events=None):
    dates = ["2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
    full = {"dates": dates,
            "profit": {"revenue": [300.0] * 4, "n_income": [150.0] * 4, "dedt": [150.0] * 4,
                       "net_yoy": [8.0, 9.0, -1.5, -0.2], "gpm": [91.0] * 4,
                       "npm": [50.0] * 4, "roe": [10.0, 20.0, 28.0, 35.0], "dv_ttm": 3.6},
            "cashflow": {"ocf": [120.0] * 4},
            "balance": {"gw_eq": [None] * 4, "dta": [20.0] * 4},
            "inst": {"dates": ["2003-12-31", "2026-03-31", "2026-06-30", "2026-09-30"],
                     "ratio": ratio, "holder_num": hnum},
            "pledge_ratio": 0.15}
    val = {"dates": ["2026-09-30"], "pe": [21.9], "pb": [6.63]}
    return {"full": full, "val": val, "events": events or [], "chg_1y": 3.2}


def _m():
    rows = [{"end_date": f"2025-{m}-28", "dedt_yi": 5.0, "netprofit_yoy": 20.0, "or_yoy": 5.0,
             "gpm": 50.0, "roe": 5.0, "debt_to_assets": 40.0, "goodwill_eq": None}
            for m in (3, 6)]
    return {"name": "茅台", "rows": rows}


def test_scan_ai_chip_uses_latest_period_with_trend(monkeypatch):
    """筹码栏取最新一期+近4期走势 — 旧版取[0]=最早一期(茅台2003-12-31:
    机构4.66%/户数3.2万, 实为5.76%/27.8万), AI据此写"机构覆盖度不足、筹码分散" """
    cap = _stub_ai(monkeypatch)
    inp = _inp(ratio=[4.66, 5.1, 5.4, 5.76], hnum=[3.2e4, 20.1e4, 24.3e4, 27.8e4])
    SR._scan_ai("600519.SH", _m(), inp, {"hits": []},
                {"base_url": "u", "model": "m", "api_key": "k"})
    p = cap["prompt"]
    assert "机构持仓合计5.76%(近4期 4.66→5.10→5.40→5.76)" in p
    assert "股东户数27.8万(近4期 3.2→20.1→24.3→27.8)" in p
    assert "机构持仓合计4.66%" not in p and "股东户数3.2万" not in p
    assert "不要引入其他来源的数字" in p
    # 财务表(10列, 与表头一致): 空值写'--'(无商誉公司原样写成"None"); 表头只出现一次(原小节标题重复一遍)
    assert "None" not in p and "nan" not in p
    assert "  2026-09-30 | 300.0 | 150.0 | -0.2 | 91.0 | 50.0 | 35.0 | 20.0 | 120.0 | --" in p
    assert p.count("报告期|营收|净利|净利同比") == 1


def test_build_scan_input_takes_most_recent_events(env):
    """近一年风险事件多于12条时取最近的12条(序列升序, 原[:12]是最早的12条)"""
    _seed_pool(env)
    today = dt.date.today()
    con = FS._conn(env)
    for i in range(20):                         # 370天前 → 今天, 每19天一条
        d = (today - dt.timedelta(days=370 - i * 19)).isoformat()
        con.execute("INSERT INTO stock_events VALUES('600519.SH',?,'forecast',?,'','high')",
                    (d, f"事件{i:02d}"))
    con.commit()
    con.close()
    evs = SR._build_scan_input("600519", env)["events"]
    year_ago = (today - dt.timedelta(days=365)).isoformat()
    expect = [e for e in FS.events_payload("600519", env)["events"]
              if e["date"] >= year_ago and e["risk"] != "low"][-12:]
    assert [e["title"] for e in evs] == [e["title"] for e in expect]
    assert len(evs) == 12 and evs[-1]["title"].startswith("事件19")


def test_validate_against_prompt_numbers():
    """依据核对prompt原文: 筹码/估值/事件维度不再因不含财务指标名被误标存疑;
    凭空出现的数值仍拦; 四舍五入过的数值算有据"""
    m = _m()
    prompt = ("机构持仓合计5.76%(近4期 4.66→5.10→5.40→5.76) 质押率0.15% 股东户数27.8万 "
              "PE_TTM 21.9 PB 6.63 股息率3.6152% 近一年股价3.2% "
              "2026-09-30 | 300.0 | 150.0 | -0.2465 | 89.5811 "
              "- [mid]2026-10-29 某股东减持127.0万股")
    ai = {"points": [
        {"title": "筹码", "evidence": "机构持仓合计仅5.76%, 股东户数27.8万", "dimension": "筹码"},
        {"title": "估值", "evidence": "PE_TTM 21.9倍、PB 6.63倍", "dimension": "估值"},
        {"title": "事件", "evidence": "2026-10-29 减持127.0万股", "dimension": "事件"},
        {"title": "财务四舍五入", "evidence": "毛利率89.58%, 同比-0.25%", "dimension": "财务"},
        {"title": "编造", "evidence": "北向持股9.87%, 较上期-3.21pp", "dimension": "筹码"},
    ]}
    out = SR._validate_ai_points(ai, m, prompt)
    sus = [p["suspect"] for p in out["points"]]
    assert sus == [False, False, False, False, True]
    # 不传prompt: 维持原口径(财务指标名/财务行精确数值), 旧调用方行为不变
    out2 = SR._validate_ai_points({"points": [
        {"evidence": "扣非净利同比=-45.2%"}, {"evidence": " imaginary_ratio=99"}]}, m)
    assert out2["points"][0]["suspect"] is False and out2["points"][1]["suspect"] is True


# ════════════ ⑧ 扫雷缓存: 只读接口 / 口径版本 ════════════

def _seed_scan_fin(env, ann="2026-10-28"):
    _seed_pool(env)
    con = FS._conn(env)
    con.executemany("INSERT INTO stock_finance(ts_code,end_date,ann_date,profit_dedt,"
                    "netprofit_yoy,or_yoy,grossprofit_margin,roe,debt_to_assets) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [("600519.SH", "2026-06-30", "2026-08-15", 2e9, 5.0, 8.0, 91.0, 15.0, 30.0),
                     ("600519.SH", "2026-09-30", ann, 2.2e9, 6.0, 9.0, 91.0, 15.0, 30.0)])
    con.commit()
    con.close()


def test_finance_ai_readonly_does_not_run_ai(env, monkeypatch):
    """GET读缓存: 无缓存时返回nocache, 不调用AI — 旧版页面每加载一只没扫过的
    股票(或财务数据已更新)就静默跑一遍AI扫雷, 占用本地模型1~3分钟"""
    import analyze_news as AN
    _seed_scan_fin(env)
    monkeypatch.setattr(AN, "get_ai_config",
                        lambda: {"enabled": True, "model": "m", "api_key": "",
                                 "base_url": "http://x"})
    cap = _stub_ai(monkeypatch)
    r = SR.scan_stock("600519", env, cached_only=True)
    assert r["ok"] is False and r["nocache"] is True and "一键扫雷" in r["msg"]
    assert "prompt" not in cap                       # AI没被调用
    # 数据不足: 仍返回incomplete(前端据此提示先[数据更新])
    assert SR.scan_stock("000001", env, cached_only=True)["incomplete"] is True


def test_scan_cache_versioned_and_failure_retried(env, monkeypatch):
    """结果带口径版本: 旧口径缓存(无_v)视为过期; AI失败的降级结果不算有效缓存"""
    import analyze_news as AN
    _seed_scan_fin(env)
    monkeypatch.setattr(AN, "get_ai_config",
                        lambda: {"enabled": True, "model": "m", "api_key": "",
                                 "base_url": "http://x"})
    reply = json.dumps({"summary": "s", "points": [
        {"title": "t", "evidence": "扣非净利同比=6.0%", "severity": "低",
         "dimension": "财务"}], "advice": "a"})
    _stub_ai(monkeypatch, reply)
    r = SR.scan_stock("600519", env)
    assert r["ok"] and r["cached"] is False and "_v" not in r["ai"]   # 版本标记不出接口
    r2 = SR.scan_stock("600519", env, cached_only=True)
    assert r2["cached"] is True and r2["ai"]["summary"] == "s"
    con = FS._conn(env)
    assert json.loads(con.execute("SELECT ai_json FROM stock_ai").fetchone()[0])["_v"] == SR.SCAN_VERSION
    # 模拟旧口径缓存(升级前的存量结果: 基于最早一期筹码数据得出的结论)
    con.execute("UPDATE stock_ai SET ai_json=?", (reply,))
    con.commit()
    con.close()
    r3 = SR.scan_stock("600519", env, cached_only=True)
    assert r3["ok"] is False and r3["nocache"] is True  # 旧口径 → 需重新扫雷
    # AI失败: 降级结果入库但不带版本标记 → 下次不算有效缓存(可重试)
    _stub_ai(monkeypatch, RuntimeError("ollama down"))
    r4 = SR.scan_stock("600519", env, force=True)
    assert r4["ok"] and "失败" in r4["ai"]["summary"]
    assert SR.scan_stock("600519", env, cached_only=True)["nocache"] is True
    # 规则-only(AI未启用)的缓存有效
    monkeypatch.setattr(AN, "get_ai_config",
                        lambda: {"enabled": False, "model": "", "api_key": "", "base_url": ""})
    SR.scan_stock("600519", env, force=True)
    assert SR.scan_stock("600519", env, cached_only=True)["cached"] is True


def test_rules_messages_rounded():
    rows = [{"end_date": "2025-09-30", "dedt_yi": 5.0, "netprofit_yoy": 1.9516, "or_yoy": 1.2,
             "gpm": 50.0, "roe": 5.0, "debt_to_assets": 71.234, "goodwill_eq": None},
            {"end_date": "2025-12-31", "dedt_yi": 5.0, "netprofit_yoy": 1.4699, "or_yoy": 1.3,
             "gpm": 50.0, "roe": 5.0, "debt_to_assets": 71.234, "goodwill_eq": None},
            {"end_date": "2026-03-31", "dedt_yi": 5.0, "netprofit_yoy": -1.9516, "or_yoy": -2.4,
             "gpm": 50.0, "roe": 5.0, "debt_to_assets": 71.234, "goodwill_eq": None},
            {"end_date": "2026-06-30", "dedt_yi": 5.0, "netprofit_yoy": -0.2465, "or_yoy": -3.1,
             "gpm": 50.0, "roe": 5.0, "debt_to_assets": 71.234, "goodwill_eq": None}]
    rules = [h["rule"] for h in SR.run_rules({"rows": rows})["hits"]]
    assert "扣非净利同比连续为负(-1.95%,-0.25%)" in rules        # 旧: (-1.9516%,-0.2465%)
    assert "资产负债率71.2%>70%" in rules
    assert "营收同比连续为负(-2.40%,-3.10%)" in rules



# ════════════ ⑨ B1自动补拉 ════════════

def test_full_module_autofetch_when_income_missing(server_app, monkeypatch, tmp_path):
    """财务/资产负债有数据但利润表/现金流量表缺失 → 自动补拉B1四表;
    6小时内不重复拉; 两表齐全不拉。原只在四表全无时触发 — 而周频链与[全量
    重拉]当时都不含这两张表, 没点过[数据更新]的股票永远不会自动补上"""
    import fetch_stock as FS
    from server import routes_admin as RA
    db = tmp_path / "stock.db"
    monkeypatch.setattr(FS, "STOCK_DB", db)
    monkeypatch.setattr(RA, "_b1_autofetch", {})
    dates = {"ok": True, "dates": ["2026-09-30"]}

    def payload(code, n_qua=12, path=None):
        con = FS._conn()
        rows = con.execute("SELECT COUNT(*) FROM stock_finance").fetchone()[0]
        con.close()
        return dates if rows else {"ok": True, "dates": []}   # 财务表出期数(与真实payload一致)
    monkeypatch.setattr(FS, "finance_full_payload", payload)
    calls = []
    for fn in ("refresh_income", "refresh_cashflow", "refresh_finance", "refresh_bs"):
        monkeypatch.setattr(FS, fn, lambda c, path=None, _n=fn: calls.append(_n) or 1)
    c = server_app["client"]
    c.get("/admin/api/stock/full?code=600519")
    assert len(calls) == 4                              # 四表全无 → 拉一轮
    # 已有财务/资产负债数据(期数非空)但利润表/现金流量表仍缺 → 仍需补拉
    con = FS._conn()
    con.execute("INSERT INTO stock_finance(ts_code,end_date) VALUES('600519.SH','2026-09-30')")
    con.commit()
    con.close()
    monkeypatch.setattr(RA, "_b1_autofetch", {})
    calls.clear()
    c.get("/admin/api/stock/full?code=600519")
    assert calls == ["refresh_income", "refresh_cashflow", "refresh_finance", "refresh_bs"]
    calls.clear()
    c.get("/admin/api/stock/full?code=600519")         # 6小时负缓存内不重复拉
    assert calls == []
    con = FS._conn()                                   # 两表补齐 → 不再拉
    con.execute("INSERT INTO stock_income VALUES('600519.SH','2026-09-30',1,1,1)")
    con.execute("INSERT INTO stock_cashflow VALUES('600519.SH','2026-09-30',1)")
    con.commit()
    con.close()
    c.get("/admin/api/stock/full?code=600519")
    assert calls == []


# ════════════ ⑩ 风险分级与prompt事件行(重扫实测暴露) ════════════

def test_holdertrade_risk_by_share_ratio(env, monkeypatch):
    """减持按占总股本比例分级: ≥1%高 / ≥0.1%中 / 更低不算风险事件; 占比未知沿用中。
    生产库占比<0.1%的减持98%是龙虎榜席位成交 — 修正前新口径重扫把紫金矿业占比
    0.01%~0.04%的席位卖出写成"[高]机构11月集中大额减持加速" """
    assert FS._holdertrade_risk(False, 5.0) == "low"             # 增持不属风险
    assert FS._holdertrade_risk(True, 1.0) == "high"
    assert FS._holdertrade_risk(True, 0.9999) == "mid"
    assert FS._holdertrade_risk(True, 0.1) == "mid"
    assert FS._holdertrade_risk(True, 0.0999) == "low"
    assert FS._holdertrade_risk(True, None) == "mid"             # 占比未披露: 按绝对量口径

    def fake(api, fields="", **kw):
        if api == "stk_holdertrade":
            return pd.DataFrame([
                {"ts_code": "X", "ann_date": "20261118",
                 "holder_name": "中国国际金融股份有限公司上海分公司", "holder_type": "P",
                 "in_de": "DE", "change_vol": 7324000, "change_ratio": 0.0356},
                {"ts_code": "X", "ann_date": "20260910", "holder_name": "某股东",
                 "holder_type": "C", "in_de": "DE", "change_vol": 1054000,
                 "change_ratio": 0.1678}])
        return pd.DataFrame()
    monkeypatch.setattr(FS.T, "call", fake)
    FS.refresh_events("600519", env)
    ev = {(e["title"], e["risk"]) for e in FS.events_payload("600519", env)["events"]}
    assert ("中国国际金融股份有限公司上海分公司减持732.4万股", "low") in ev   # 量大但占比小
    assert ("某股东减持105.4万股", "mid") in ev                                 # 量小但占比够


def test_refresh_float_replaces_stale_events_and_formats_small(env, monkeypatch):
    """解禁事件: 小额不再显示"0.0亿股"(生产库"钱雪松解禁0.0亿股(0.0061%)");
    拉到行则替换旧事件(标题格式变更后旧行不残留); 同(解禁日,股东)多行只出一条;
    缺解禁日的脏行不入库"""
    con = FS._conn(env)
    con.execute("INSERT INTO stock_events VALUES('600519.SH','2026-06-28','float',"
                "'袁永刚解禁1.01亿股(5.49%)','','high')")
    con.execute("INSERT INTO stock_events VALUES('600519.SH','2026-01-01','dividend',"
                "'分红: 旧口径','','low')")
    con.commit()
    con.close()
    soon = (dt.date.today() + dt.timedelta(days=100)).strftime("%Y%m%d")
    row = {"ts_code": "600519.SH", "ann_date": "20260101", "float_date": soon,
           "float_share": 61000, "float_ratio": 0.0061, "holder_name": "钱雪松"}
    df = pd.DataFrame([
        row, dict(row),                                             # 同(解禁日,股东)重复行
        {"ts_code": "600519.SH", "ann_date": "20260101", "float_date": float("nan"),
         "float_share": 5e7, "float_ratio": 6.0, "holder_name": "脏行"},
        {"ts_code": "600519.SH", "ann_date": "20260101", "float_date": "20300628",
         "float_share": 1.01e8, "float_ratio": 5.49, "holder_name": "袁永刚"}])
    monkeypatch.setattr(FS.T, "call", lambda *a, **kw: df)
    assert FS.refresh_float("600519", env) == 2
    ev = [(e["date"], e["title"], e["risk"]) for e in FS.events_payload("600519", env)["events"]]
    assert (dt.date.today() + dt.timedelta(days=100)).isoformat() in [e[0] for e in ev]
    assert (FS._iso(soon), "钱雪松解禁6.10万股(0.0061%)", "low") in ev
    assert ("2030-06-28", "袁永刚解禁1.01亿股(5.49%)", "high") in ev
    assert ("2026-06-28", "袁永刚解禁1.01亿股(5.49%)", "high") not in ev   # 旧位置/旧行被替换
    assert not any("脏行" in t for _d, t, _r in ev)
    assert len(ev) == 3 and ("2026-01-01", "分红: 旧口径", "low") in ev    # 其他类型不受影响
    assert FS._fmt_float_shares(None) == "--" and FS._fmt_float_shares(1e7) == "0.10亿股"


def test_scan_prompt_event_lines_carry_detail(monkeypatch):
    """事件行带上明细: 只有标题时模型把"减持732万股"一律当大额, 看不到它只占总股本0.036%"""
    cap = _stub_ai(monkeypatch)
    inp = _inp([5.1], [2e5], events=[
        {"date": "2026-11-18", "risk": "mid", "type": "增减持",
         "title": "中国国际金融股份有限公司上海分公司减持732.4万股", "detail": "变动占比0.0356%"},
        {"date": "2026-07-15", "risk": "high", "type": "业绩预告",
         "title": "业绩预告: 首亏", "detail": "2026-06-30报告期 预计变动-80.0%~-70.0%"},
        {"date": "2028-06-28", "risk": "high", "type": "解禁",
         "title": "袁永刚解禁1.01亿股(5.49%)", "detail": "解禁股东: 袁永刚"},
        {"date": "2026-01-01", "risk": "mid", "type": "停复牌", "title": "停牌", "detail": ""}])
    SR._scan_ai("600519.SH", _m(), inp, {"hits": []},
                {"base_url": "u", "model": "m", "api_key": "k"})
    p = cap["prompt"]
    assert "  - [mid]2026-11-18 中国国际金融股份有限公司上海分公司减持732.4万股(变动占比0.0356%)" in p
    assert "  - [high]2026-07-15 业绩预告: 首亏(2026-06-30报告期 预计变动-80.0%~-70.0%)" in p
    assert "  - [high]2028-06-28 袁永刚解禁1.01亿股(5.49%)\n" in p     # 解禁占比已在标题里
    assert "  - [mid]2026-01-01 停牌\n" in p                            # 无明细: 原样
    assert "解禁股东" not in p
    assert "减持看占总股本比例" in p


# ════════════ ⑪ 页面行为 ════════════

def test_stock_page_renders(env, server_app, js_syntax):
    """页面经真实Flask渲染: 200; 渲染后的脚本仍过语法门禁(只检查模板文件的门禁
    发现不了Jinja表达式渲染后破坏JS的情况); 自选池服务端渲染全部chips
    (原只渲染前12只+点不动的"…", 加载第一只股票之前其余的点不到)"""
    con = FS._conn()
    con.executemany("INSERT OR REPLACE INTO stock_pool VALUES(?,?,'','t','t')",
                    [("600519.SH", "贵州茅台")] + [("%06d.SZ" % i, "股票%d" % i)
                                                for i in range(14)])
    con.commit()
    con.close()
    r = server_app["client"].get("/admin/stock")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="chartK"' in html and "loadSeq" in html
    js_syntax(html)
    assert 'data-code="600519.SH">贵州茅台</a>' in html
    # 页面脚本里renderPool的模板串也含一处(后面跟${…}), 不计入
    chips = (html.count('class="btn small" data-code="')
             - html.count('class="btn small" data-code="${'))
    assert chips == 15 and ">…</span>" not in html


def test_stock_db_isolated_by_default():
    """框架守卫自检(11-24事故: 上面的渲染测试最初漏带env, FS._conn()连到生产库,
    一条INSERT往生产自选池塞了14只假股票): 不带real_stock_db标记的用例, 个股库
    一律指向临时库; 只有显式标记的用例才连生产库(下面那条只比对路径, 不读写)"""
    prod = (FS.BASE / "data" / "stock.db").resolve()
    assert prod.exists(), "生产个股库路径变了 — 守卫的比较基准需同步"
    assert FS.STOCK_DB.resolve() != prod
    con = FS._conn()                               # 不带path的连接也落不到生产库
    try:
        assert Path(con.execute("PRAGMA database_list").fetchone()[2]).resolve() != prod
    finally:
        con.close()


@pytest.mark.real_stock_db
def test_marker_opts_back_into_real_stock_db():
    assert FS.STOCK_DB.resolve() == (FS.BASE / "data" / "stock.db").resolve()


def test_stock_page_behavior():
    """页面脚本放进node沙箱实跑(tests/stock_page_behavior.js): 驱动真实的
    load/renderXxx, fetch应答顺序由测试控制 — 切股竞态、切股清面板、户数单位与
    环比、未来事件不上图、池展开、免确认按股票记。用例对修复前的页面14/16失败
    (每条复现一个老问题), 只有"同一只股票重载"两条防过度修正的通过"""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node不可用")
    r = subprocess.run(
        [node, str(REPO / "tests" / "stock_page_behavior.js"),
         str(REPO / "server" / "templates" / "admin" / "stock.html")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, "页面行为测试失败:\n" + out[-2500:]
    assert "passed 16" in out, out[-600:]
