# -*- coding: utf-8 -*-
"""MI-3 情绪图多指数 · 情绪图页面(docs/开发计划-情绪图多指数.md 第 5 节 MI-3)

① 页签载荷(_index_tab / _index_tabs / _fill_mval): 三个指数各一份与上证同构的数据; years 窗口、悬停明细只带
   近 3 年、没出分日子的原因、副图(本指数估值 / 该交易所两融 / 挂钩ETF / 板块主力净流入); 全A 估值四个页签
   共用上证那一份、按日期对齐(水位是"截至当日在历史里的排名", 各页签各算各的会对不上);
② 上证的数据在载荷顶层、原样不变(有关页签 / 无关页签两份载荷逐键相同);
③ 页签里的情绪分与库表 index_sentiment 同一套算法、同一个数;
④ 容错: 单个指数出错只少一个页签、指数表没有、配置里没写挂钩ETF、名字里带 </script>;
⑤ 页面脚本: 手写 ECharts 桩下的行为测试(tests/viewer_behavior.js) + 真实 ECharts 下的冒烟测试
   (tests/viewer_echarts_real.js, 用仓库自带的 echarts.min.js 在 node 里以服务端渲染模式跑生成出来的页面);
⑥ fetch_etf_flow.net_flow_series 的 codes 参数(默认 = 国家队 8 只, 不变)。
全部跑在临时库上(DB.DB_PATH / VW.DB_PATH / EF.MACRO_DB 指向 tmp_path)。"""
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import boards as BD                 # noqa: E402
import db as DB                     # noqa: E402
import fetch_etf as FE              # noqa: E402
import fetch_etf_flow as EF         # noqa: E402
import score as SC                  # noqa: E402
import score_index as SI            # noqa: E402
import viewer as VW                 # noqa: E402
from metrics import LABELS          # noqa: E402

CFG = yaml.safe_load((CORE / "config.yaml").read_text(encoding="utf-8"))
CODES = ["0.399001", "0.399006", "1.000688"]
ETFS = {"159903.SZ": (1.234, 5.0e5), "159915.SZ": (2.5, 3.0e6), "588000.SH": (1.1, 8.0e5), "510050.SH": (3.0, 1.5e5)}
N = 820                                              # 约 3 年 3 个月的交易日: 够看"悬停明细只带近 3 年"
EMPTY_K = [None, None, None, None]                   # K 线窗口外(与上证一致; 裸 null 会让 ECharts 抛错)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时行情库: 上证 / 深综 / 三个指数的日线、全市场涨停结构、四个板块的日表、各副图的表"""
    path = tmp_path / "sentiment.db"
    monkeypatch.setattr(DB, "DB_PATH", path)
    monkeypatch.setattr(VW, "DB_PATH", path)
    monkeypatch.setattr(VW, "BASE", tmp_path)         # 宏观事件库按模块目录找: 别读生产的
    monkeypatch.setattr(EF, "MACRO_DB", path)
    monkeypatch.delenv("SENTIMENT_OVERRIDE", raising=False)
    rng = np.random.default_rng(3)
    dates = pd.bdate_range(end="2026-11-26", periods=N)
    iso = [d.strftime("%Y-%m-%d") for d in dates]

    def walk(s0):
        close = s0 * np.exp(np.cumsum(rng.normal(0, 0.012, N)))
        open_ = close * (1 + rng.normal(0, 0.004, N))
        return open_, np.maximum(open_, close) * 1.004, np.minimum(open_, close) * 0.996, close

    con = DB.connect()
    o, h, l, c = walk(3000)
    con.executemany("INSERT INTO index_daily VALUES(?,?,?,?,?,?,?,?,?)",
                    [("1.000001", d, a, b, e, f, 2e8, 2e12, 0.0) for d, a, b, e, f in zip(iso, o, h, l, c)])
    con.executemany("INSERT INTO index_daily VALUES(?,?,?,?,?,?,?,?,?)",
                    [("0.399106", d, None, None, None, 2000.0, 1.2e8, None, 0.0) for d in iso])
    zt = rng.poisson(60, N)
    prev = np.concatenate([[zt[0]], zt[:-1]])
    con.executemany(
        "INSERT INTO mood_raw(date,zt_count,dt_count,zb_count,zb_rate,lb_max,lb2_count,jl_rate,yzt_count) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [(d, int(a), int(b), int(e), float(e) / (a + e) * 100, int(m), int(lb), float(lb) / (p or 1) * 100, int(p))
         for d, a, b, e, m, lb, p in zip(iso, zt, rng.poisson(6, N), rng.poisson(12, N), rng.poisson(1, N) + 1,
                                         rng.poisson(5, N), prev)])
    con.execute(BD.SCHEMA)
    for code, s0 in [("0.399001", 10000), ("0.399006", 2200), ("1.000688", 1100)]:
        o, h, l, c = walk(s0)
        amt = (200 + 400 * rng.random(N)) * 1e8          # 元
        con.executemany("INSERT INTO index_daily VALUES(?,?,?,?,?,?,?,?,?)",
                        [(code, d, a, b, e, f, None, v, 0.0) for d, a, b, e, f, v in zip(iso, o, h, l, c, amt)])
    con.executemany(
        "INSERT INTO board_daily(board,date,strong,weak,fade,streak_max,streak2,prev_strong,zt,dt,mf_net) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(b, d, int(s), int(w), int(f), int(m), int(s2), int(ps), int(z), int(dt_), float(i) * 1e6)
         for b in ("SH", "SZ", "CYB", "KCB") for i, (d, s, w, f, m, s2, ps, z, dt_) in enumerate(zip(
             iso, rng.poisson(40, N), rng.poisson(20, N), rng.poisson(15, N), rng.poisson(1, N) + 1,
             rng.poisson(4, N), np.concatenate([[0], rng.poisson(40, N)[:-1]]), rng.poisson(9, N), rng.poisson(3, N)))])
    con.executescript("""
      CREATE TABLE index_val_daily(code TEXT, date TEXT, pe REAL, pb REAL, name TEXT, PRIMARY KEY(code, date));
      CREATE TABLE etf_daily(code TEXT, date TEXT, close REAL, amount REAL, name TEXT, vol REAL, PRIMARY KEY(code, date));
      CREATE TABLE mkt_val_daily(date TEXT PRIMARY KEY, alla_pe REAL, alla_pb REAL, pe50 REAL, pb50 REAL,
        pe300 REAL, pb300 REAL, pe500 REAL, pb500 REAL, pe1000 REAL, pb1000 REAL);
    """)
    con.close()
    con = EF.ensure_db(path)                            # etf_share(份额)表
    last300 = iso[-300:]
    for ts, name in [("399001.SZ", "深证成指"), ("399006.SZ", "创业板指"), ("000688.SH", "科创50")]:
        con.executemany("INSERT INTO index_val_daily VALUES(?,?,?,?,?)", [(ts, d, 12.3456, 1.2345, name) for d in last300])
    con.executemany("INSERT INTO margin_daily(date,rzye,rzrqye,sse_rzrqye,szse_rzrqye) VALUES(?,?,?,?,?)",
                    [(d, 1.8e12, 1.8e12, 9.5e11, 8.5e11) for d in last300])
    for code, (close, amount) in ETFS.items():
        con.executemany("INSERT INTO etf_daily VALUES(?,?,?,?,?,?)",
                        [(code, d, close, amount, code, 1.0) for d in iso])
        con.executemany("INSERT INTO etf_share VALUES(?,?,?)",
                        [(code, d.replace("-", ""), 1e6 + i * 1e5) for i, d in enumerate(iso)])   # 每天多 10 亿份
    con.executemany("INSERT INTO mkt_val_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    # 全A PE 用一条起伏的序列: 单调序列上"截至当日的历史分位"恒为 1, 看不出日期轴不同带来的差别
                    [(d, 15.0 + ((i * 37) % 23) / 10, 1.5, 10.0, 1.2, 12.0, 1.4, 25.0, 2.0, 30.0, 2.5)
                     for i, d in enumerate(last300)])
    con.commit()
    con.close()
    return {"path": path, "iso": iso, "dates": dates}


def _tab(payload, code):
    return next(t for t in payload["tabs"] if t["key"] == code)


def _first_on_or_after(dates, cutoff):
    return next(i for i, d in enumerate(dates) if d >= cutoff)


def _is_window_start(env, years):
    return _first_on_or_after(env["dates"], env["dates"][-1] - pd.DateOffset(years=years))


# ════════════ ① 页签载荷 ════════════

def test_three_tabs_with_same_shape_as_shanghai(env):
    """config index.extra 的每个指数一个页签; 键与上证同构(页面脚本按同一套代码出图)"""
    P = VW.build_payload(CFG)
    assert [t["key"] for t in P["tabs"]] == CODES
    assert [t["name"] for t in P["tabs"]] == ["深证成指", "创业板指", "科创50"]
    assert [t["board"] for t in P["tabs"]] == ["深市", "创业板", "科创板"]
    i3 = _is_window_start(env, VW.MI3_DETAIL_YEARS)
    for t in P["tabs"]:
        assert t["n"] == N and t["dates"] == env["iso"]
        assert t["w0"] == 0 and t["d0"] == i3                 # 不限年: 窗口从第一天起; 悬停明细只带近 3 年
        for k in ("k", "vol", "pct", "score", "sc", "zt", "dt", "zb", "jl", "lim"):
            assert len(t[k]) == N, (t["key"], k)
        assert set(t["raw"]) == set(t["cs"]) == set(t["labels"]) == set(t["weights"]) == set(CFG["weights"])
        assert all(len(t["raw"][k]) == N - i3 == len(t["cs"][k]) for k in t["raw"])   # 明细只存 d0 起的一段
        assert all(isinstance(x, list) and len(x) == 4 for x in t["k"])
        o, c, l, h = t["k"][-1]
        assert [o, c, l, h] == [round(v, 2) for v in (o, c, l, h)] and l <= min(o, c) <= max(o, c) <= h
    assert P["stages"] == ["极度冰点", "冰点/恐慌", "偏冷/退潮", "中性偏弱", "中性", "中性偏强", "偏热/发酵",
                           "狂热/高潮", "极度狂热"]
    assert P["window"] == CFG["window"]


def test_tab_component_labels_follow_the_index_definitions(env):
    """指数页签右侧明细表的分量名: 结构 / 量能两组换成指数口径的说法(不是"两市成交量"), 量价分量与上证同名"""
    t = _tab(VW.build_payload(CFG), "0.399001")
    assert t["labels"] == {
        "zt_count": "强势家数", "dt_count": "弱势家数", "zb_rate": "冲高回落率", "lb_max": "连续强势高度",
        "jl_rate": "接力率", "vol_total": "指数成交额", "vr": "量比趋势5/20",
        **{k: LABELS[k] for k in ("rsi", "chg", "gap", "kr", "bias20", "bias60", "close", "amp")}}
    assert all(len(name) <= 8 for name in t["labels"].values())    # 表格宽度有限


def test_tab_scores_start_after_one_window_and_stage_codes_match(env):
    """情绪分满一个分位窗口(250 个交易日)才有; 档位号与分数对得上; 没出分的日子及原因"""
    P = VW.build_payload(CFG)
    stages = P["stages"]
    for t in P["tabs"]:
        assert t["first"] == CFG["window"]                      # 板块日表从头就有 → 第 251 个交易日起出分
        assert all(s is None for s in t["score"][:t["first"]])
        scored = [s for s in t["score"] if s is not None]
        assert len(scored) == N - t["first"] and t["notes"] == {}   # 夹具数据齐全: 没有缺分的日子
        assert -100 <= min(scored) and max(scored) <= 100
        for s, c in zip(t["score"], t["sc"]):
            assert (c is None) == (s is None)
            if s is not None:
                assert stages[c] == SC.stage_label(s, CFG["thresholds"])


def test_detail_only_last_three_years_and_years_window(env):
    """悬停明细(raw / cs)只带最近 3 年; years 规则下窗口外的 K 线与上证一样是四空数组, 窗口起点记在 w0"""
    P = VW.build_payload(CFG)
    t = _tab(P, "0.399001")
    i3 = _is_window_start(env, VW.MI3_DETAIL_YEARS)
    assert t["d0"] == i3 > 0 and t["w0"] == 0
    assert len(t["raw"]["rsi"]) == N - i3 and len(t["cs"]["rsi"]) == N - i3
    assert isinstance(t["k"][0], list) and t["score"][0] is None       # 不限年: K 线全史
    P2 = VW.build_payload(CFG, years=2)
    t2 = _tab(P2, "0.399001")
    i2 = _is_window_start(env, 2)
    assert t2["w0"] == i2 and t2["d0"] == i2                # years 窗口比 3 年窄: 明细按窗口
    assert t2["k"][:i2] == [EMPTY_K] * i2 and isinstance(t2["k"][i2], list) and len(t2["k"][i2]) == 4
    assert all(x is None for x in t2["vol"][:i2]) and all(x is None for x in t2["score"][:max(i2, t2["first"])])
    assert len(t2["raw"]["rsi"]) == N - i2


def test_tab_aux_series(env):
    """副图: 本指数估值 / 该交易所两融 / 挂钩ETF成交额与净流入 / 板块主力净流入, 都对齐本页签的日期轴"""
    con = sqlite3.connect(str(env["path"]))
    amt = con.execute("SELECT amount FROM index_daily WHERE code='0.399001' ORDER BY date DESC LIMIT 1").fetchone()[0]
    con.close()
    P = VW.build_payload(CFG)
    a, b, c = (_tab(P, k) for k in CODES)
    assert a["aux"]["val"]["399001.SZ"]["n"] == "深证成指"
    assert a["aux"]["val"]["399001.SZ"]["pe"][-30:] == [12.35] * 30 and a["aux"]["val"]["399001.SZ"]["pb"][-1] == 1.23
    assert a["aux"]["val"]["399001.SZ"]["pe"][0] is None               # 估值表只有最近 300 天
    assert set(c["aux"]["val"]) == {"000688.SH"}
    assert a["vol"][-1] == pytest.approx(amt / 1e8, abs=0.006)         # 成交额: 元 → 亿, 取两位小数
    # 两融: 科创板在上海 → 沪市; 深证成指、创业板指 → 深市
    assert a["aux"]["margin"][-1] == 8500.0 and b["aux"]["margin"][-1] == 8500.0 and c["aux"]["margin"][-1] == 9500.0
    assert a["aux"]["margin"][0] is None
    # 挂钩 ETF(config index.extra 的 etf): 成交额 千元→亿; 净流入 = 份额差 × 收盘价, 对齐到日期轴
    for t, code in ((a, "159903.SZ"), (b, "159915.SZ"), (c, "588000.SH")):
        close, amount = ETFS[code]
        aux = t["aux"]
        assert list(aux["etf"]) == [code] and aux["etf"][code]["amt"][-1] == round(amount / 1e5, 2)
        assert aux["etf_flow"] == [None] + [round(1e5 * close / 1e4, 2)] * (N - 1)   # 每天多 10 亿份
        assert len(aux["mf"]) == N and aux["mf"][0] == 0.0 and aux["mf"][-1] == round((N - 1) * 1e6 / 1e8, 2)
    assert "510050.SH" not in a["aux"]["etf"]                         # 510050 不属于任何指数页签


def test_mval_is_the_shanghai_copy_aligned_by_date(env):
    """全A 估值与水位四个页签共用上证那一份(aux["mval"]), 按日期对到各页签的日期轴上; 载荷里只带一份
    (日期轴是最长页签后缀的页签只记偏移), 且只带页签用得到的几条"""
    P = VW.build_payload(CFG)
    host = max(P["tabs"], key=lambda t: t["n"])
    assert host["key"] == CODES[0] and "mval" in host["aux"]
    assert set(host["aux"]["mval"]) == {"alla_pe", "alla_pb", "pe300", "alla_pe_pct", "alla_pe_pct5",
                                        "alla_pb_pct", "alla_pb_pct5", "pe300_pct", "pe300_pct5"}
    for t in P["tabs"][1:]:
        assert "mval" not in t["aux"] and t["aux"]["mval_off"] == host["n"] - t["n"] == 0
    pos = {d: i for i, d in enumerate(P["dates"])}
    for t in P["tabs"]:
        mv = t["aux"].get("mval") or {k: v[t["aux"]["mval_off"]:] for k, v in host["aux"]["mval"].items()}
        for k, arr in mv.items():
            assert arr == [P["mval"][k][pos[d]] for d in t["dates"]], (t["key"], k)


def test_mval_stays_consistent_when_a_tab_axis_is_not_a_suffix(env):
    """某个指数中间缺了几天(日期轴不再是别的页签的后缀): 它自己带一份, 数值仍与上证按日期逐值相同 —
    各页签各算各的话, 水位会从各自日期轴的第一天起累计, 与上证对不上"""
    con = sqlite3.connect(str(env["path"]))
    con.execute("DELETE FROM index_daily WHERE code='0.399006' AND date BETWEEN ? AND ?",
                (env["iso"][600], env["iso"][604]))                    # 缺在全A估值有数的区间里
    con.commit()
    P = VW.build_payload(CFG)
    b = _tab(P, "0.399006")
    assert b["n"] == N - 5 and "mval" in b["aux"] and "mval_off" not in b["aux"]
    pos = {d: i for i, d in enumerate(P["dates"])}
    for k, arr in b["aux"]["mval"].items():
        assert arr == [P["mval"][k][pos[d]] for d in b["dates"]], k
    alone = VW._mval_series(b["dates"], con)["alla_pe_pct"]              # 只按它自己的日期轴算: 历史里少了 5 天
    con.close()
    shared = b["aux"]["mval"]["alla_pe_pct"]
    assert sum(1 for j in range(610, N - 5) if alone[j] != shared[j]) > 100
    assert P["tabs"][0]["aux"].get("mval") and P["tabs"][2]["aux"]["mval_off"] == 0     # 其余页签照旧共用


def test_fill_mval_unit():
    base = ["d1", "d2", "d3", "d4", "d5"]
    mval = {"alla_pe": [1, 2, 3, 4, 5], "alla_pe_pct": [.2, .4, .6, .8, 1.0], "alla_pb": [9] * 5,
            "alla_pb_pct5": [.5] * 5, "pe300": [3] * 5, "pe50": [7] * 5, "pb1000": [2] * 5}
    tabs = [{"n": 5, "dates": list(base), "aux": {}},
            {"n": 3, "dates": ["d2", "d4", "d5"], "aux": {}},
            {"n": 3, "dates": ["d3", "d4", "d5"], "aux": {}},
            {"n": 2, "dates": ["d0", "d2"], "aux": {}}]
    VW._fill_mval(tabs, base, mval)
    assert tabs[0]["aux"]["mval"] == {"alla_pe": [1, 2, 3, 4, 5], "alla_pe_pct": [.2, .4, .6, .8, 1.0],
                                      "alla_pb": [9] * 5, "alla_pb_pct5": [.5] * 5, "pe300": [3] * 5}
    assert tabs[1]["aux"]["mval"]["alla_pe"] == [2, 4, 5] and "mval_off" not in tabs[1]["aux"]   # 不是后缀: 自己带
    assert "mval" not in tabs[2]["aux"] and tabs[2]["aux"]["mval_off"] == 2                      # 是后缀: 记偏移
    assert tabs[3]["aux"]["mval"]["alla_pe"] == [None, 2]                                         # 上证没有的日子: 空
    empty = [{"n": 1, "dates": ["d1"], "aux": {}}]
    VW._fill_mval([], base, mval)
    VW._fill_mval(empty, base, {})
    assert empty[0]["aux"] == {}


def test_mval_percentiles_match_an_independent_calculation(tmp_path, monkeypatch):
    """全A 估值水位(自算): 某日的全史分位 = 截至当日的全部历史值里 ≤ 当日值的占比, 满 60 个样本才出;
    近 5 年口径同理(这里全在 5 年内, 与全史相同)。期望值在测试里用最朴素的数数法算"""
    p = tmp_path / "s.db"
    con = sqlite3.connect(str(p))
    con.execute("""CREATE TABLE mkt_val_daily(date TEXT PRIMARY KEY, alla_pe REAL, alla_pb REAL, pe50 REAL, pb50 REAL,
        pe300 REAL, pb300 REAL, pe500 REAL, pb500 REAL, pe1000 REAL, pb1000 REAL)""")
    dates = list(pd.bdate_range("2026-07-01", periods=70).strftime("%Y-%m-%d"))
    pe = [(j * 37) % 71 + 1.0 for j in range(70)]                      # 互不相同的伪随机序列
    con.executemany("INSERT INTO mkt_val_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [(d, v, 1.5, None, None, None, None, None, None, None, None) for d, v in zip(dates, pe)])
    con.commit()
    con.close()
    monkeypatch.setattr(VW, "DB_PATH", p)
    mv = VW._read_aux(dates)["mval"]
    assert len(mv) == 16                                            # 10 条估值 + 6 条水位
    exp_pe = [None if j < 59 else round(sum(1 for x in pe[:j + 1] if x <= pe[j]) / (j + 1), 3) for j in range(70)]
    exp_flat = [None if j < 59 else 1.0 for j in range(70)]         # 常数序列: 每一天都是自己历史里的最大值
    assert mv["alla_pe"] == [float(v) for v in pe] and mv["alla_pb"] == [1.5] * 70 and mv["pe300"] == [None] * 70
    assert mv["alla_pe_pct"] == exp_pe and mv["alla_pe_pct5"] == exp_pe
    assert mv["alla_pb_pct"] == exp_flat and mv["pe300_pct"] == [None] * 70 and mv["pe300_pct5"] == [None] * 70
    assert mv["pe50"] == [None] * 70 and "pe50_pct" not in mv        # 只有这三条有水位


# ════════════ ② 上证的数据原样不变 ════════════

def test_shanghai_payload_identical_with_and_without_tabs(env, monkeypatch):
    with_tabs, with_years = VW.build_payload(CFG), VW.build_payload(CFG, years=2)
    monkeypatch.setattr(VW, "_index_tabs", lambda cfg, years=None: [])
    without, without_years = VW.build_payload(CFG), VW.build_payload(CFG, years=2)
    for a, b in ((with_tabs, without), (with_years, without_years)):
        assert set(a) == set(b) and b["tabs"] == [] and len(a["tabs"]) == 3
        for k in b:
            if k != "tabs":
                assert a[k] == b[k], k
    assert without["n"] == N and len(without["dates"]) == N
    assert set(without) >= {                                   # 改动前就有的各键都还在、含义不变
        "dates", "k", "vol", "pct", "score", "v1", "stage", "zt", "dt", "zb", "jl", "raw", "cs", "labels", "weights",
        "n", "sub_starts", "val", "margin", "etf", "mval", "events", "etf_flow"}
    assert isinstance(without["k"][0], list) and without_years["k"][0] == EMPTY_K   # 窗口外的 K 线与上证同一种写法


def test_build_payload_can_skip_index_tabs(env, monkeypatch):
    """tabs=False: 只算上证页签的数据, 不连带算三个指数页签(简报只要全A 估值)"""
    monkeypatch.setattr(VW, "_index_tabs", lambda *a, **k: pytest.fail("不该去算指数页签"))
    P = VW.build_payload(CFG, tabs=False)
    assert not ({"tabs", "stages", "window"} & set(P)) and P["n"] == N and len(P["mval"]["alla_pe"]) == N


# ════════════ ③ 页签里的情绪分 = 库表里的情绪分 ════════════

def test_tab_scores_equal_index_sentiment_table(env):
    """页签里的情绪分现算(跟着生成规则的参数走), 与更新链写进 index_sentiment 的是同一个数"""
    SI.run(cfg=CFG)
    P = VW.build_payload(CFG)
    con = sqlite3.connect(str(env["path"]))
    for code in CODES:
        rows = con.execute("SELECT date, score, stage FROM index_sentiment WHERE index_code=? ORDER BY date",
                           (code,)).fetchall()
        t = _tab(P, code)
        got = [(d, s, sc) for d, s, sc in zip(t["dates"], t["score"], t["sc"]) if s is not None]
        assert len(rows) == N - CFG["window"] > 0
        assert [(d, s) for d, s, _ in rows] == [(d, s) for d, s, _ in got]
        assert [st for _, _, st in rows] == [P["stages"][sc] for _, _, sc in got]
    con.close()


def test_tab_scores_follow_rule_weights(env, monkeypatch):
    """生成规则里的自定义权重(SENTIMENT_OVERRIDE 指向的文件, 叠加在默认权重上)对指数页签同样生效"""
    over = env["path"].parent / "over.yaml"
    over.write_text(yaml.safe_dump({"weights": {"rsi": 0.3, "zt_count": 0.5}}), encoding="utf-8")
    monkeypatch.setenv("SENTIMENT_OVERRIDE", str(over))
    cfg = SC.load_cfg()
    assert cfg["weights"]["rsi"] == 0.3 and cfg["weights"]["amp"] == CFG["weights"]["amp"]
    P = VW.build_payload(cfg)
    base = VW.build_payload(CFG)
    for t, t0 in zip(P["tabs"], base["tabs"]):
        assert t["weights"] == cfg["weights"]
        diff = sum(1 for a, b in zip(t["score"][-60:], t0["score"][-60:]) if a is None or b is None or abs(a - b) > 0.5)
        assert diff > 20, t["key"]


# ════════════ ④ 容错 ════════════

def test_one_failing_index_only_drops_its_tab(env, monkeypatch, capsys):
    real = VW._index_tab
    specs = CFG["index"]["extra"]

    def flaky(cfg, sp, con, years, SI_):
        if sp["code"] == specs[1]["code"]:
            raise RuntimeError("库打不开")
        return real(cfg, sp, con, years, SI_)
    monkeypatch.setattr(VW, "_index_tab", flaky)
    P = VW.build_payload(CFG)
    assert [t["key"] for t in P["tabs"]] == [specs[0]["code"], specs[2]["code"]]
    assert "创业板指 跳过: RuntimeError: 库打不开" in capsys.readouterr().out
    assert P["n"] == N and "mval" in P["tabs"][0]["aux"] and P["tabs"][1]["aux"]["mval_off"] == 0


def test_no_index_tables_still_builds_shanghai_tab_only(env, monkeypatch, capsys):
    con = sqlite3.connect(str(env["path"]))
    con.execute("DELETE FROM index_daily WHERE code IN ('0.399001','0.399006','1.000688')")
    con.commit()
    con.close()
    P = VW.build_payload(CFG)
    assert P["tabs"] == [] and P["n"] == N
    assert "页签 深证成指 跳过: RuntimeError: 没有该指数的日线" in capsys.readouterr().out
    monkeypatch.setattr(VW, "DB_PATH", env["path"].parent / "none.db")
    assert VW._index_tabs(CFG) == []                                   # 行情库不存在
    assert VW._index_tabs(dict(CFG, index={})) == []                  # 配置里没有 extra


def test_aux_sources_fail_independently(env, monkeypatch, capsys):
    """每一块副图各自容错: 估值表没了 → 估值不出; ETF 净流入算不出来 → 成交额与净流入一起不出;
    其余副图(两融 / 主力净流入 / 全A估值)与情绪分不受影响"""
    con = sqlite3.connect(str(env["path"]))
    con.execute("DROP TABLE index_val_daily")
    con.commit()
    con.close()

    def boom(*a, **k):
        raise RuntimeError("接口不通")
    monkeypatch.setattr(EF, "net_flow_series", boom)
    P = VW.build_payload(CFG)
    out = capsys.readouterr().out
    assert [t["key"] for t in P["tabs"]] == CODES
    for t in P["tabs"]:
        assert t["aux"]["val"] == {}
        assert t["aux"]["etf"] == {} and t["aux"]["etf_flow"] == []
        assert len(t["aux"]["margin"]) == N and len(t["aux"]["mf"]) == N
        assert t["first"] == CFG["window"] and t["notes"] == {}
    assert "mval" in P["tabs"][0]["aux"]                              # 全A 估值来自上证那份, 不受影响
    assert out.count("估值副图跳过: OperationalError: no such table: index_val_daily") == 3
    assert out.count("ETF 副图跳过: RuntimeError: 接口不通") == 3


def test_no_etf_in_config_or_not_fetched(env, monkeypatch):
    cfg = json.loads(json.dumps(CFG))
    del cfg["index"]["extra"][0]["etf"]
    cfg["index"]["extra"][1]["etf"] = "999999.SZ"                     # 不在抓取清单里
    tabs = VW._index_tabs(cfg)
    assert tabs[0]["aux"]["etf"] == {} and tabs[0]["aux"]["etf_flow"] == []
    assert tabs[1]["aux"]["etf"] == {} and tabs[1]["aux"]["etf_flow"] == []
    assert list(tabs[2]["aux"]["etf"]) == ["588000.SH"]


def test_script_tag_in_payload_is_escaped_in_html(env, monkeypatch):
    """载荷里的字符串带 </script>(指数名是人写在配置里的; 事件正文是抓来的网页): 载荷本身原样,
    写进页面时转成 \\u003c — 否则浏览器在那里提前关闭脚本块, 整页空白(2026-11-10 实测)"""
    cfg = json.loads(json.dumps(CFG))
    cfg["index"]["extra"][0]["name"] = "</script><b>x</b>"
    P = VW.build_payload(cfg)
    assert P["tabs"][0]["name"] == "</script><b>x</b>"
    monkeypatch.setattr(VW, "get_echarts_inline", lambda: "/*echarts*/")
    monkeypatch.setattr(VW, "_read_events", lambda level_min=None: [
        {"date": env["iso"][-1], "level": 5, "title": "t", "type": "", "source": "",
         "content": '广告<div><script>x</script></div>'}])
    out = VW.generate_html(cfg, out=env["path"].parent / "v.html")
    text = out.read_text(encoding="utf-8")
    assert text.count("</script>") == 3                               # 关闭标签只有页面自己那几个
    assert "\\u003c/script>\\u003cb>x\\u003c/b>" in text                       # 名字里的每个 < 都写成了 \u003c
    assert "广告\\u003cdiv>\\u003cscript>x\\u003c/script>\\u003c/div>" in text
    m = re.search(r"const BASE = (\{.*?\});\n", text, re.S)
    back = json.loads(m.group(1))                                     # 页面脚本读到的值与载荷相同
    assert back["tabs"][0]["name"] == "</script><b>x</b>" and back["events"][0]["content"].endswith("</div>")


def test_generate_html_contains_tabs_and_shanghai(env, monkeypatch):
    monkeypatch.setattr(VW, "get_echarts_inline", lambda: "/*echarts*/")
    out = VW.generate_html(CFG, out=env["path"].parent / "v.html")
    text = out.read_text(encoding="utf-8")
    m = re.search(r"const BASE = (\{.*?\});\n", text, re.S)
    P = json.loads(m.group(1))
    assert [t["key"] for t in P["tabs"]] == CODES and P["n"] == N
    for frag in ('id="tabs"', 'id="cmp"', 'id="overlayBox"', 'id="ckMf"', "function activate(", "function mount(",
                 "function buildBase(", "function overlaySeries(", "const BASE = ", "sentTabs_v1", "sentOverlay_v1"):
        assert frag in text, frag
    # 模板里的占位符必须全部被替换: 11-27 实测有一个颜色占位符少写了结尾两个字符, 替换不到,
    # 页面上所有页签的阴线 / 跌停柱全画成红色(非法颜色值被 ECharts 忽略, 沿用上一根的颜色)
    left = re.search(r".{0,30}__[A-Z][A-Z]*_.{0,30}", text)
    assert left is None, "页面里残留模板占位符: " + (left.group(0) if left else "")
    for key, hexv in VW.C.items():                                  # 页面里用到的是真正的颜色值
        assert hexv in text, key
    script = text[text.index("const C = "):text.index("const TAB_STORE")]
    for key in ("blue", "red", "aqua", "yellow", "ink", "ink2", "muted", "grid", "axis"):
        assert f'{key}: "{VW.C[key]}"' in script, key              # 页面脚本里的颜色常量


def test_small_helpers():
    assert VW._r(None, 2) is None and VW._r(float("nan"), 2) is None and VW._r(float("inf"), 0) is None
    assert VW._r("x", 2) is None
    assert VW._r(3.14159, 2) == 3.14 and VW._r(3.5, 0) == 4 and VW._r(-2.5, 0) == -2 and isinstance(VW._r(7.0, 0), int)
    assert VW._r(45.0, 0) == 45
    d = list(pd.bdate_range("2024-01-01", periods=10))
    assert VW._window_start(d, None) == 0
    assert VW._window_start(d, 0) == 0
    assert VW._window_start(d, 1) == 0                                # 不满一年 → 从头
    long = list(pd.bdate_range("2020-01-01", "2026-11-26"))
    i = VW._window_start(long, 3)
    assert long[i - 1] < long[-1] - pd.DateOffset(years=3) <= long[i]
    table = VW._stage_table(CFG)
    assert table == ["极度冰点", "冰点/恐慌", "偏冷/退潮", "中性偏弱", "中性", "中性偏强", "偏热/发酵",
                     "狂热/高潮", "极度狂热"]
    for s in (-100, -80, -60, -30.3, -30, -10, 0, 10, 30, 60, 80, 100):
        assert table[sum(1 for t in sorted(CFG["thresholds"]) if s > t)] == SC.stage_label(s, CFG["thresholds"])
    assert VW._stage_table({"thresholds": [-1, 1]})[1] == SC.stage_label(0, [-1, 1])


def test_config_lists_linked_etf_in_fetch_list():
    for sp in CFG["index"]["extra"]:
        assert sp["etf"] in FE.ETF_ALL, sp
    assert [sp["etf"] for sp in CFG["index"]["extra"]] == ["159903.SZ", "159915.SZ", "588000.SH"]
    assert "159903.SZ" not in FE.ETF_INDEXES and "159903.SZ" not in EF.ETF_CODES   # 国家队口径仍是 8 只


# ════════════ ⑤ 页面脚本 ════════════

def _node(js, *args):
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用")
    r = subprocess.run([node, str(REPO / "tests" / js), *[str(a) for a in args]],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    return r, (r.stdout or "") + (r.stderr or "")


def test_viewer_page_behavior(tmp_path):
    """页面脚本放进 node 沙箱实跑(tests/viewer_behavior.js, 手写的 ECharts 桩): 上证页签与改动前逐字一致
    (悬停框 / 右侧面板 / 没出分提示三条守卫拿改动前的模板跑也通过)、指数页签取数、缩放窗口按日期保持且
    来回切换不漂移、叠加、对比条、页签记忆、副图开关、事件标注、没出分与窗口外日子的提示。
    用例对没有页签的旧页面 32 条里 29 条失败。"""
    tpl = tmp_path / "viewer.html"
    tpl.write_text(VW.HTML, encoding="utf-8")
    r, out = _node("viewer_behavior.js", tpl)
    assert r.returncode == 0, "页面行为测试失败:\n" + out[-2500:]
    js = (REPO / "tests" / "viewer_behavior.js").read_text(encoding="utf-8")
    n_cases = len(re.findall(r"^test\(", js, flags=re.M))
    assert n_cases >= 32 and f"ALL TESTS PASS passed {n_cases}" in out, out[-600:]


def test_viewer_page_on_real_echarts(env, tmp_path, monkeypatch):
    """生成出来的页面 × 仓库自带的 ECharts(服务端渲染模式, tests/viewer_echarts_real.js): 手写的桩不校验
    传进去的配置, "真实 ECharts 会抛错 / 画错"的问题只有这里能抓到 —
    颜色占位符没替换(阴线全红)、K 线序列里混进裸 null(点页签整张图停住)、切页签后右侧面板的事件窗口不对。
    用两条规则各生成一张: 全历史(产线规则)与近两年("测试·两年"规则); 创业板指少 300 天, 页签日期轴长短不同"""
    if not (CORE / "vendor" / "echarts.min.js").exists():
        pytest.skip("ECharts 本地缓存不存在(core/vendor/echarts.min.js)")
    iso, n = env["iso"], N
    con = sqlite3.connect(str(env["path"]))
    con.execute("DELETE FROM index_daily WHERE code='0.399006' AND date < ?", (iso[300],))
    con.commit()
    con.close()
    monkeypatch.setattr(VW, "_read_events", lambda level_min=None: [
        {"date": iso[n - 211], "level": 3, "title": "MI3-EVENT-OUT", "type": "", "source": "", "content": ""},
        {"date": iso[n - 200], "level": 3, "title": "MI3-EVENT-IN", "type": "", "source": "", "content": ""}])
    monkeypatch.setattr(VW, "get_echarts_inline", lambda: "")         # 页面脚本测试自己加载仓库里的 ECharts
    full = VW.generate_html(CFG, out=tmp_path / "full.html")
    two = VW.generate_html(CFG, years=2, out=tmp_path / "y2.html")
    r, out = _node("viewer_echarts_real.js", full, two)
    assert r.returncode == 0, "真实 ECharts 下的页面测试失败:\n" + out[-3000:]
    assert len(re.findall(r"(?m)^ok   ", out)) == 10 and "ALL TESTS PASS passed 10" in out, out[-600:]


# ════════════ ⑥ ETF 净流入的 codes 参数 ════════════

def test_net_flow_series_single_etf_matches_its_share_of_the_default(env):
    """net_flow_series(codes=(一只,)) = 默认(国家队口径)序列里这一只ETF的那一部分; 默认行为不变"""
    full = EF.net_flow_series(n_days=6000)
    one = EF.net_flow_series(n_days=6000, codes=("159915.SZ",))
    by_date = {x["date"]: x["flows"]["159915.SZ"] for x in full}
    assert len(full) == N - 1 and len(one) == N - 1
    assert [x["date"] for x in one] == [x["date"] for x in full]
    assert all(x["net_yi"] == by_date[x["date"]] for x in one)
    assert one[0]["flows"] == {"159915.SZ": one[0]["net_yi"]}
    close, _ = ETFS["159915.SZ"]
    assert one[0]["net_yi"] == round(1e5 * close / 1e4, 2)           # 每天多 10 亿份 × 收盘价
    assert set(full[0]["flows"]) == set(ETFS) - {"159903.SZ"}          # 默认: 国家队口径里的 3 只(深成指ETF 不在其中)
    assert EF.net_flow_series(n_days=6000, codes=("999999.SZ",)) == []
    assert EF.net_flow_series(n_days=6000, codes=()) == full           # 空清单 = 默认
