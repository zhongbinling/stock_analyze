# -*- coding: utf-8 -*-
"""D1-17 R1: 实时源解析纯函数(罐头报文, 零网络) + 探测脚本接线"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import probe_rt as P  # noqa: E402


def _qt_payload():
    f = ["1", "上证指数", "000001", "3299.62", "3288.88", "3293.60",
         "375213376", "187982", "187231", "3299.61"] + ["x"] * 20
    f += ["45678901"]            # idx30 后接 → 构造到idx30
    # 重排: 确保 idx30=时间 idx37=成交额(万)
    f = ["1", "上证指数", "000001", "3299.62", "3288.88", "3293.60",
         "375213376", "187982", "187231", "3299.61"] + ["x"] * 20 + \
        ["20261028150003", "x", "x", "x", "x", "x", "x", "45678901"]
    # 索引: 0..9已知, 10..29填充(20个), 30=ts, 31..36填充(6个), 37=amount
    assert len(f) >= 38
    return f


def test_parse_qt():
    payload = "~".join(_qt_payload())
    d = P.parse_qt(f'v_sh000001="{payload}";')
    assert d["code"] == "sh000001" and d["name"] == "上证指数"
    assert d["price"] == "3299.62" and d["prev_close"] == "3288.88"
    assert d["ts"] == "20261028150003" and d["amount_wan"] == "45678901"
    assert P.parse_qt("garbage") == {}
    assert P.parse_qt('v_x="1~2~3"') == {}            # 字段不足


def test_parse_qt_multi():
    one = "~".join(_qt_payload())
    d = P.parse_qt_multi(f'v_sh510300="{one}";\nv_sz159901="{one}";\n')
    assert d["n"] == 2 and d["name样例"] == "上证指数"
    assert P.parse_qt_multi("") == {}


def test_parse_sina():
    f = ["上证指数", "3293.60", "3288.88", "3299.62", "3301.00", "3290.00"] \
        + ["0"] * 26 + ["2026-10-28", "15:00:03", "00"]
    d = P.parse_sina('var hq_str_sh000001="' + ",".join(f) + '";')
    assert d["code"] == "sh000001" and d["price"] == "3299.62"
    assert d["date"] == "2026-10-28" and d["time"] == "15:00:03"
    assert P.parse_sina("403 Forbidden") == {}


def test_parse_em_quote():
    d = P.parse_em_quote({"data": {"f43": 3299.62, "f60": 3288.88,
                                   "f104": 2156, "f105": 2890, "f106": 123,
                                   "f86": 1769000034567}})
    assert d["price"] == 3299.62 and d["up"] == 2156 and d["down"] == 2890
    assert P.parse_em_quote({"data": {}}) == {}       # 无价=无效
    assert P.parse_em_quote({}) == {}


def test_parse_tx_minute():
    j = {"code": 0, "data": {"sh000001": {"data": {"data": [
        "0930 3293.60 123456", "0931 3294.00 234567"]}}}}
    d = P.parse_tx_minute(j)
    assert d["n"] == 2 and d["first"].startswith("0930") and d["last"].startswith("0931")
    assert P.parse_tx_minute({}) == {} and P.parse_tx_minute({"data": {}}) == {}


def test_parse_em_trends():
    j = {"rc": 0, "data": {"preClose": 3288.88, "trends": [
        "2026-10-28 09:30,3293.60,123456,4567.0",
        "2026-10-28 15:00,3299.62,3752133,45678.9"]}}
    d = P.parse_em_trends(j)
    assert d["n"] == 2 and d["pre_close"] == 3288.88
    assert d["last"].startswith("2026-10-28 15:00")
    assert P.parse_em_trends({}) == {}


def test_parse_em_breadth():
    """D1-17 R1: 涨跌家数 = ulist.np 沪深两市场合计"""
    j = {"data": {"diff": [
        {"f12": "000001", "f14": "上证指数", "f104": 1672, "f105": 599, "f106": 88},
        {"f12": "399001", "f14": "深证成指", "f104": 2200, "f105": 1201, "f106": 111}]}}
    d = P.parse_em_breadth(j)
    assert d["up"] == 3872 and d["down"] == 1800 and d["flat"] == 199
    assert d["markets"] == ["000001", "399001"]
    # stock/get 型(f104='-'→0)全零 → 无效
    assert P.parse_em_breadth({"data": {"diff": [{"f104": 0, "f105": 0, "f106": 100}]}}) == {}
    assert P.parse_em_breadth({}) == {}


def test_probe_script_wired():
    """探测脚本接线: 源清单/限流策略(东财不burst护IP)/burst计数"""
    src = (CORE / "probe_rt.py").read_text(encoding="utf-8")
    for marker in ("qt.gtimg.cn", "push2.eastmoney.com", "hq.sinajs.cn",
                   "minute/query", "trends2", "f104", "159915"):
        assert marker in src, f"缺源标记: {marker}"
    assert "n_qt" in src and "不burst" in src     # 东财只间隔探测的说明
