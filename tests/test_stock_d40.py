# -*- coding: utf-8 -*-
"""D1-40 季报表全史(用户实测 11-26: 避雷针页"盈利·收入与利润"及其后几组图只能显示到
23-12-31, 手动缩放、左右拖动最多也只能到这里)

两层原因:
① 载荷 finance_full_payload 被统一截成最近12期(D1-35 时为了让营收与财务指标期数对齐) —
   页面六张图只剩下最近三年, 缩放条本身已经停在两端尽头;
② 库里四张季报表本来就只有最近 56~84 期: tushare 的 balancesheet/cashflow/fina_indicator
   单次最多返回100行, 同一报告期还常有"原始/更正后"两行, 拉取代码没有翻页 —
   茅台的财务指标只到2012-12、资产负债表只到2009-09, 更早的一段一直缺着。
生产库副本上重拉后: 茅台 104期(1998-12起), 中航沈飞 113期(1993-12起)。"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))

import fetch_stock as FS   # noqa: E402
import stock_risk as SR    # noqa: E402

CODE = "600519.SH"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "stock.db"
    monkeypatch.setattr(FS, "STOCK_DB", db)
    return db


def _quarters(n):
    """最近 n 个报告期, 旧→新, YYYYMMDD(截至 2026-09-30)"""
    out, y, q = [], 2026, 3
    while len(out) < n:
        out.append("%d%02d%02d" % (y, q * 3, 31 if q in (1, 4) else 30))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return list(reversed(out))


def _iso(p):
    return "%s-%s-%s" % (p[:4], p[4:6], p[6:])


def _fake_server(monkeypatch, rows, cap=100):
    """假接口: 行序同 tushare(报告期倒序), 每次最多返回 cap 行, 不理会请求的 limit。
    返回 (api, offset, limit, start_date, fields) 的调用记录"""
    calls = []

    def fake(api, fields="", **kw):
        off = kw.get("offset") or 0
        calls.append((api, off, kw.get("limit"), kw.get("start_date"), fields))
        return pd.DataFrame(rows[api][off:off + cap], columns=fields.split(","))
    monkeypatch.setattr(FS.T, "call", fake)
    return calls


def _rows(api, periods, both_versions=True):
    """每个报告期一到两行: 财务指标是更正行在前, 三大报表是原始行在前(与实测行序一致)"""
    flags = ("1", "0") if api == "fina_indicator" else ("0", "1")
    out = []
    for p in reversed(periods):
        for f in (flags if both_versions else ("1",)):
            out.append(dict(ts_code=CODE, end_date=p, ann_date="20260426", update_flag=f))
    return out


def _table_dates(env, table):
    con = FS._conn(env)
    rows = [r[0] for r in con.execute(
        f"SELECT end_date FROM {table} WHERE ts_code=? ORDER BY end_date", (CODE,))]
    con.close()
    return rows


# ── 翻页 ──

def test_statement_refresh_pages_through_the_100_row_cap(env, monkeypatch):
    """单次100行、每期两行 → 一次调用只够最近50期。翻页后每一期都在库里;
    返回值是报告期数(页面"数据更新"的逐项结果)而不是接口行数"""
    periods = _quarters(104)
    for api, fn, table, extra in (
            ("fina_indicator", FS.refresh_finance, "stock_finance", {"roe": 7.5}),
            ("balancesheet", FS.refresh_bs, "stock_bs", {"total_assets": 3e11}),
            ("income", FS.refresh_income, "stock_income", {"revenue": 3e10}),
            ("cashflow", FS.refresh_cashflow, "stock_cashflow", {"n_cashflow_act": 1.2e10})):
        rows = [dict(r, **extra) for r in _rows(api, periods)]
        calls = _fake_server(monkeypatch, {api: rows})
        assert fn(CODE, env) == 104, api
        assert [c[1] for c in calls] == [0, 100, 200], api        # 208行 = 100 + 100 + 8
        assert all(c[0] == api and c[2] == FS._PAGE_ROWS for c in calls)
        assert _table_dates(env, table) == [_iso(p) for p in periods], api
    assert _table_dates(env, "stock_finance")[0] == "2000-12-31"   # 原: 只到 2012 年前后


def test_statement_pull_follows_the_real_page_size(env, monkeypatch):
    """服务端单次上限低于请求值(50行)时按首页行数续翻; 总行数恰为页长整数倍时
    末尾多一次空页确认"""
    rows = _rows("balancesheet", _quarters(50))          # 50期 × 两行 = 100行
    calls = _fake_server(monkeypatch, {"balancesheet": [dict(r, total_assets=1.0) for r in rows]},
                         cap=50)
    assert FS.refresh_bs(CODE, env) == 50
    assert [c[1] for c in calls] == [0, 50, 100]
    assert len(_table_dates(env, "stock_bs")) == 50


def test_statement_refresh_failure_returns_minus_one(env, monkeypatch):
    def boom(api, fields="", **kw):
        raise RuntimeError("tushare balancesheet: [40203] 积分不足")
    monkeypatch.setattr(FS.T, "call", boom)
    assert FS.refresh_bs(CODE, env) == -1
    assert _table_dates(env, "stock_bs") == []


def test_refresh_finance_lookback_narrows_the_request_window(env, monkeypatch):
    calls = _fake_server(monkeypatch, {"fina_indicator": []})
    FS.refresh_finance(CODE, env, lookback=400)
    assert calls[0][3] not in (None, "19900000") and len(calls[0][3]) == 8


# ── 同一报告期只留一行 ──

def test_one_per_period_keeps_the_latest_announced_version():
    pick = lambda rows: [(FS._iso(r["end_date"]), r.get("v")) for r in FS._one_per_period(rows)]  # noqa: E731
    # 更正行在前(财务指标的行序)还是原始行在前(三大报表的行序), 留下的都是更正后的
    assert pick([dict(end_date="20240331", ann_date="20240426", update_flag="1", v=2),
                 dict(end_date="20240331", ann_date="20240426", update_flag="0", v=1)]) == [("2024-03-31", 2)]
    assert pick([dict(end_date="20240331", ann_date="20240426", update_flag="0", v=1),
                 dict(end_date="20240331", ann_date="20240426", update_flag="1", v=2)]) == [("2024-03-31", 2)]
    # 公告日更晚的优先于更正标记
    assert pick([dict(end_date="20231231", ann_date="20240403", update_flag="1", v=9),
                 dict(end_date="20231231", ann_date="20240830", update_flag="0", v=8)]) == [("2023-12-31", 8)]
    # 行里没有这两列(空值被 pandas 读成 nan)时每期仍只留一行, 先后同接口
    assert pick([dict(end_date="20260930.0", ann_date=float("nan"), update_flag=float("nan"), v=3),
                 dict(end_date="20260630", v=4),
                 dict(end_date="20260930.0", ann_date=float("nan"), update_flag=float("nan"), v=3)]
                ) == [("2026-09-30", 3), ("2026-06-30", 4)]


WULIANGYE_H1_2025 = [          # 利润表 2025-06-30, 接口原始三行(营收亿)
    dict(end_date="20250630", ann_date="20250828", f_ann_date="20250828", update_flag="0", v=527.7),
    dict(end_date="20250630", ann_date="20250828", f_ann_date="20250828", update_flag="1", v=527.7),
    dict(end_date="20250630", ann_date="20250828", f_ann_date="20260430", update_flag="1", v=235.1),
]


def test_one_per_period_takes_the_retroactively_adjusted_row():
    """同一报告期三行, ann_date 全相同: 第三行的实际公告日是 2026-04-30(2025年报发布时
    追溯调整)。必须取它 — 2025年报净利润93.2亿是调整后口径, 留原披露值的话9月累计净利润
    222亿比全年还大, 滚动12个月净利润算成负数。只比 ann_date 会取成同日更正行(527.7)"""
    assert [r["v"] for r in FS._one_per_period(WULIANGYE_H1_2025)] == [235.1]
    # 没有实际公告日这一列时退回按 ann_date; 该列是空值时同样
    no_f = [{k: v for k, v in r.items() if k != "f_ann_date"} for r in WULIANGYE_H1_2025]
    assert [r["v"] for r in FS._one_per_period(no_f)] == [527.7]
    nan_f = [dict(r, f_ann_date=float("nan")) for r in WULIANGYE_H1_2025]
    assert [r["v"] for r in FS._one_per_period(nan_f)] == [527.7]


def test_refresh_income_stores_the_adjusted_value(env, monkeypatch):
    """五粮液 2025 年三季报: 留调整后那一行, 滚动12个月净利润才是对的
    (年报 93.17 + 本期累计 99.69 − 上年同期 67.87 = 124.99 亿; 留原披露的 222.12 亿则算成 −29 亿)"""
    nine_m = [dict(end_date="20250930", ann_date="20251031", f_ann_date="20251031",
                   update_flag="1", n_income=222.1185393877e8),
              dict(end_date="20250930", ann_date="20251031", f_ann_date="20260430",
                   update_flag="1", n_income=67.8697305017e8)]
    _fake_server(monkeypatch, {"income": nine_m + [
        dict(end_date="20260930", ann_date="20261031", f_ann_date="20261031", update_flag="1",
             n_income=99.69e8),
        dict(end_date="20251231", ann_date="20260430", f_ann_date="20260430", update_flag="1",
             n_income=93.1711318468e8)]})
    assert FS.refresh_income(CODE, env) == 3
    con = FS._conn(env)
    inc = {r[0]: r[1] for r in con.execute("SELECT end_date, n_income FROM stock_income")}
    con.close()
    assert inc["2025-09-30"] == pytest.approx(67.87e8, rel=1e-4)
    assert SR._ttm(inc, "2026-09-30") == pytest.approx(124.99e8, rel=1e-4)


def test_statement_requests_carry_the_version_fields(env, monkeypatch):
    """取版本要用的列都得向接口要: 实际公告日(三大报表)、公告日、更正标记"""
    apis = ("income", "balancesheet", "cashflow", "fina_indicator")
    calls = _fake_server(monkeypatch, {a: _rows(a, _quarters(6)) for a in apis})
    for fn in (FS.refresh_income, FS.refresh_bs, FS.refresh_cashflow, FS.refresh_finance):
        fn(CODE, env)
    for api, _off, _limit, _start, fields in calls:
        cols = fields.split(",")
        assert "ann_date" in cols and "update_flag" in cols, api
        if api != "fina_indicator":                 # 财务指标接口没有实际公告日
            assert "f_ann_date" in cols, api


def test_refresh_income_stores_the_corrected_value(env, monkeypatch):
    periods = _quarters(30)
    rows = _rows("income", periods)
    old = periods[3]
    for r in rows:                               # 第4期做过更正: 营收由 100 亿改为 120 亿
        if r["end_date"] == old:
            r["revenue"] = 120e8 if r["update_flag"] == "1" else 100e8
            r["n_income"] = 10e8
            r["f_ann_date"] = r["ann_date"]
    for r in rows:
        r.setdefault("revenue", 50e8)
        r.setdefault("n_income", 5e8)
    _fake_server(monkeypatch, {"income": rows})
    assert FS.refresh_income(CODE, env) == 30
    p = FS.finance_full_payload(CODE, env)
    assert p["profit"]["revenue"][3] == 120.0 and p["dates"][3] == _iso(old)
    assert p["profit"]["revenue"][:3] == [50.0] * 3


# ── 财务全景载荷: 全史 ──

def test_full_payload_reaches_the_earliest_table(env):
    """四张表起点不同: 报告期取并集, 哪张表有数哪张表就有值, 没有的留空"""
    con = FS._conn(env)
    early, mid, late, latest = _quarters(104)[0], _quarters(60)[0], _quarters(30)[0], "2026-09-30"
    for p in _quarters(104):
        con.execute("INSERT INTO stock_income VALUES(?,?,?,?,?)", (CODE, _iso(p), 100e8, 100e8, 1e8))
        con.execute("INSERT INTO stock_bs VALUES('600519.SH',?,1e10,1e9,1e9,1e9,0,3e11,5e10)",
                    (_iso(p),))
    for p in _quarters(60):
        con.execute("INSERT INTO stock_finance(ts_code,end_date,profit_dedt,roe) VALUES(?,?,1e9,20.0)",
                    (CODE, _iso(p)))
    for p in _quarters(30):
        con.execute("INSERT INTO stock_cashflow VALUES(?,?,1.5e9)", (CODE, _iso(p)))
    con.commit()
    con.close()
    p = FS.finance_full_payload(CODE, env)
    assert len(p["dates"]) == 104 and p["dates"][0] == _iso(early) and p["dates"][-1] == latest
    assert p["profit"]["revenue"] == [100.0] * 104              # 利润表有数的每一期都有
    assert p["balance"]["assets"] == [3000.0] * 104
    assert p["profit"]["dedt"] == [None] * 44 + [10.0] * 60     # 财务指标自 mid 起
    assert p["cashflow"]["ocf"] == [None] * 74 + [15.0] * 30    # 现金流量表自 late 起
    assert _iso(mid) == p["dates"][44] and _iso(late) == p["dates"][74]
    # 扫雷口径: 最近 8 / 12 期
    assert FS.finance_full_payload(CODE, env, n_qua=8)["dates"] == p["dates"][-8:]
    assert FS.finance_full_payload(CODE, env, n_qua=12)["dates"] == p["dates"][-12:]


def test_scan_financial_table_is_still_the_last_8_periods(env):
    """扫雷 prompt 的财务表标称"近8季": 载荷全史之后扫雷这一路不受影响"""
    con = FS._conn(env)
    for p in _quarters(104):
        con.execute("INSERT INTO stock_finance(ts_code,end_date,profit_dedt,roe) VALUES(?,?,1e9,20.0)",
                    (CODE, _iso(p)))
        con.execute("INSERT INTO stock_income VALUES(?,?,?,?,?)", (CODE, _iso(p), 10e8, 10e8, 1e8))
        con.execute("INSERT INTO stock_cashflow VALUES(?,?,1.5e9)", (CODE, _iso(p)))
    con.commit()
    con.close()
    full = SR._build_scan_input(CODE, env)["full"]
    assert len(full["dates"]) == 8
    assert all(len(line.split("|")) == 9 for line in SR._fin_table(full))   # 指标名 + 8 个报告期


def test_full_api_returns_the_whole_history(server_app, env):
    con = FS._conn(env)
    for p in _quarters(104):
        con.execute("INSERT INTO stock_finance(ts_code,end_date,profit_dedt) VALUES(?,?,1e9)",
                    (CODE, _iso(p)))
    # 利润表/现金流量表各有一行: 不触发"四表不全时自动补拉"(那会去调真实接口)
    con.execute("INSERT INTO stock_income VALUES(?,?,?,?,?)", (CODE, "2026-09-30", 1e8, 1e8, 1e7))
    con.execute("INSERT INTO stock_cashflow VALUES(?,?,1e7)", (CODE, "2026-09-30"))
    con.commit()
    con.close()
    r = server_app["client"].get("/admin/api/stock/full?code=600519")
    assert r.status_code == 200
    assert len(r.get_json()["dates"]) == 104
