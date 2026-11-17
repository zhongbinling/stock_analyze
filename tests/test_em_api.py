# -*- coding: utf-8 -*-
"""em_api: 指数K线三重回退/腾讯分页/单位换算/板块资金流解析/涨停池统计"""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import em_api as E  # noqa: E402


# ── 小工具 ──

def test_lb_from_str():
    assert E._lb_from_str("首板") == 1
    assert E._lb_from_str("3连板") == 3
    assert E._lb_from_str("12连板") == 12
    assert E._lb_from_str("") == 1 and E._lb_from_str(None) == 1
    assert E._lb_from_str("x") == 1


def test_tx_code():
    assert E._tx_code("1.000001") == "sh000001"
    assert E._tx_code("0.399106") == "sz399106"


# ── 东财K线解析 ──

def test_em_kline_parse(monkeypatch):
    rows = ["2026-11-16,1.0,2.0,3.0,0.5,100,200,0.8,1.5,0.5,3.0",
            "2026-11-17,2.0,2.2,2.4,1.8,300,600,0.9,10.0,0.2,2.0"]
    monkeypatch.setattr(E, "_get", lambda url, p, **kw: {
        "data": {"klines": rows}})
    df = E._em_kline("1.000001", "20260101", "20261231")
    assert list(df.columns[:5]) == ["date", "open", "close", "high", "low"]
    assert df["close"].tolist() == [2.0, 2.2]          # 数值化
    assert df["volume"].tolist() == [100.0, 300.0]


def test_sector_kline_and_fflow(monkeypatch):
    monkeypatch.setattr(E, "_get", lambda url, p, **kw: {"data": {"klines": [
        "2026-11-16,1,2,3,0.5,100,200",
        "2026-11-17,2,2.2,2.4,1.8,300,600"]}})
    df = E.sector_kline("BK0001")
    assert df["close"].tolist() == [2.0, 2.2] and df["amount"].tolist() == [200.0, 600.0]
    # 资金流: p[0]=date p[1]=主力 p[4]=大单 p[5]=超大单
    monkeypatch.setattr(E, "_get", lambda url, p, **kw: {"data": {"klines": [
        "2026-11-16,-100,10,20,50,-150,-5,1,2,3,4,5,6,7,8"]}})
    ff = E.sector_fundflow("BK0001")
    assert ff.iloc[0]["main_net"] == -100.0
    assert ff.iloc[0]["large_net"] == 50.0 and ff.iloc[0]["super_net"] == -150.0


# ── 腾讯日K分页回退 ──

def test_tx_kline_pagination_and_units(monkeypatch):
    """分页回抓到beg; 无成交额列NaN; amp/pct_chg换算"""
    pages = [
        {"data": {"sh000001": {"qfqday": [
            [f"2026-01-0{d}", "10", "11", "12", "9", str(d * 100)] for d in (5, 6, 7)]}}},
        {"data": {"sh000001": {"qfqday": [
            ["2026-01-03", "9", "10", "11", "8", "50"],
            ["2026-01-04", "9.5", "10.5", "11", "9", "60"]]}}},
    ]
    monkeypatch.setattr(E, "_get", lambda url, p, **kw: pages.pop(0))
    df = E._tx_kline("sh000001", "20260104")
    assert df["date"].tolist() == ["2026-01-03", "2026-01-04", "2026-01-05",
                                   "2026-01-06", "2026-01-07"]   # 去重合并升序
    assert df["amount"].isna().all()                    # 腾讯源无成交额
    assert df["volume"].sum() == 50 + 60 + 500 + 600 + 700
    # pct_chg: 01-04 close10.5 / 01-03 close10 - 1 = 5%
    assert df["pct_chg"].iloc[1] == pytest.approx(5.0)


def test_tx_kline_day_key_fallback(monkeypatch):
    """无qfqday时回退day键; 页内最早日≤beg即停(静态mock需满足终止条件)"""
    monkeypatch.setattr(E, "_get", lambda url, p, **kw: {
        "data": {"sh000001": {"day": [["2026-01-05", "10", "11", "12", "9", "100"]]}}})
    df = E._tx_kline("sh000001", "20260105")
    assert len(df) == 1 and df["close"].iloc[0] == 11


# ── tushare第三源 ──

def test_ts_kline_units(monkeypatch):
    """tushare日线: amount千元→元, vol→volume"""
    import tushare_api as T
    monkeypatch.setattr(T, "index_daily", lambda code, beg, end: pd.DataFrame({
        "trade_date": ["20260105", "20260106"], "open": [10.0, 11.0],
        "close": [10.5, 11.5], "high": [11.0, 12.0], "low": [9.5, 10.5],
        "vol": [100.0, 200.0], "amount": [1000.0, 2000.0]}))
    df = E._ts_kline("1.000001", "20260101", "20261231")
    assert df["amount"].tolist() == [1.0e6, 2.0e6]      # 千元→元
    assert df["pct_chg"].iloc[1] == pytest.approx(11.5 / 10.5 * 100 - 100)


def test_index_kline_fallback_chain(monkeypatch):
    """东财异常→腾讯空→tushare兜底; 全灭→(空df, none)"""
    monkeypatch.setattr(E, "_em_kline",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("WAF")))
    monkeypatch.setattr(E, "_tx_kline", lambda *a: pd.DataFrame())
    monkeypatch.setattr(E, "_ts_kline", lambda *a: pd.DataFrame(
        {"date": ["2026-01-05"], "close": [10.0]}))
    df, src = E.index_kline("1.000001")
    assert src == "tushare" and len(df) == 1
    monkeypatch.setattr(E, "_ts_kline", lambda *a: pd.DataFrame())
    df2, src2 = E.index_kline("1.000001", beg="20000101")
    assert src2 == "none" and df2.empty


# ── 涨停专题池 ──

def _ths_body(rate=None, pages=1):
    up = {"today": {"num": 45, "rate": rate,
                    "history_num": None if rate is not None else 50,
                    "open_num": None if rate is not None else 10},
          "yesterday": {"num": 40}}
    info = [{"name": "A", "high_days": "3连板"},
            {"name": "B", "high_days": "首板"},
            {"name": "C", "high_days": "5连板"}]
    return {"status_code": 0, "data": {
        "limit_up_count": up, "limit_down_count": {"today": {"num": 3}},
        "info": info, "page": {"count": pages}}}


def test_ths_pool_stats_rate_paths(monkeypatch):
    monkeypatch.setattr(E, "_get", lambda url, p, **kw: _ths_body(rate=0.85))
    st = E.ths_pool_stats("20261116")
    assert st["zt_count"] == 45 and st["dt_count"] == 3
    assert st["zb_rate"] == 15.0                       # rate路径: (1-0.85)*100
    assert st["lb_max"] == 5 and st["lb2_count"] == 2  # 3连板/5连板≥2板
    assert st["jl_rate"] == round(2 / 40 * 100, 1)     # 2板+/昨日涨停40
    # history_num/open_num路径: 炸板率=10/50
    monkeypatch.setattr(E, "_get", lambda url, p, **kw: _ths_body())
    st2 = E.ths_pool_stats("20261116")
    assert st2["zb_rate"] == 20.0


def test_ths_pool_stats_non_trading_day(monkeypatch):
    """up无today → {}(非交易日/越界)"""
    monkeypatch.setattr(E, "_get", lambda url, p, **kw: {
        "status_code": 0, "data": {"limit_up_count": {}, "info": []}})
    assert E.ths_pool_stats("20261115") == {}


def test_em_pool_stats(monkeypatch):
    pools = {"zt": [{"lbc": 1}, {"lbc": 4}],
             "dt": [{"x": 1}],
             "zb": [{"x": 1}, {"x": 2}, {"x": 3}],
             "prev": [{"zdp": 2.0}, {"zdp": 4.0}, {"zdp": None}]}
    monkeypatch.setattr(E, "topic_pool", lambda kind, d: pools[kind])
    st = E.em_pool_stats("20261116")
    assert st["zt_count"] == 2 and st["dt_count"] == 1 and st["zb_count"] == 3
    assert st["zb_rate"] == round(3 / 5 * 100, 1)       # 炸板/(涨停+炸板)
    assert st["lb_max"] == 4 and st["lb2_count"] == 1
    assert st["prev_zt_chg"] == 3.0                     # 昨日涨停今日表现均值(空值剔除)


def test_topic_pool_validation(monkeypatch):
    with pytest.raises(ValueError):
        E.topic_pool("bad", "20261116")


# ── 板块清单分页+host切换 ──

def test_sector_list_pagination(monkeypatch):
    calls = []

    def fake_get(url, params, **kw):
        calls.append(url)
        pn = int(params["pn"])
        if pn == 1:                           # 第1页主host只回1条(total=3)
            return {"data": {"total": 3, "diff": [{"f12": "BK1"}]}}
        if "80.push2" not in url:             # 主host第2页被限流 → 切镜像
            raise RuntimeError("WAF")
        return {"data": {"total": 3, "diff": [{"f12": "BK2"}, {"f12": "BK3"}]}}
    monkeypatch.setattr(E, "_get", fake_get)
    out = E.sector_list("industry")
    assert [x["f12"] for x in out] == ["BK1", "BK2", "BK3"]
    assert any("80.push2" in u for u in calls)          # 限流切镜像host
