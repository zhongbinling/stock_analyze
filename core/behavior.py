# -*- coding: utf-8 -*-
"""历史回测引擎: 市场状态划分(牛/熊/震荡概率) + 行为指数(因子组合-100~100)
+ 阈值买卖回测(对照510300/上证)。

因子(各自expanding分位→[-100,100], 只用当日及历史无前视):
  sentiment 情绪热度  sentiment.score         高热=贪婪(反向属性, 默认负权)
  pe_pct    全A PE分位 mkt_val_daily.alla_pe  低估值=买(默认负权)
  buffett   巴菲特分位 mv_total/GDP           低=买(默认负权)
  margin    两融余额分位 rzrqye(三所合计)      杠杆资金入场=顺势(默认正权)
  fflow     主力资金流 SW L1主力净流入5日均    主力买入=顺势(默认正权)
  breadth   板块温度宽度 BK板块均分            普涨=顺势(默认正权)

组合: 当日可得因子的加权和(权重归一化, 缺失因子吸收 — 与评分引擎同构)。
状态划分: 120日动量s → p_bull=σ(k·s), p_bear=σ(-k·s), p_震荡=1-两者(截断归一)。
回测: c≥entry持有多头, c≤exit空仓(滞回), 次日收盘执行(无前视)。
"""
import bisect
import math
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

SENT_DB = BASE / "data" / "sentiment.db"

# (key, 名称, 默认权重, 说明)
FACTOR_DEFS = [
    ("sentiment", "情绪热度", -30, "情绪分expanding分位; 高热=贪婪(历史IC为负→默认反向权重)"),
    ("pe_pct", "全A PE分位", -25, "全A整体法PE历史分位; 低估值=买入区(默认负权)"),
    ("buffett", "巴菲特指标分位", -15, "总市值/GDP历史分位; 低=便宜(默认负权)"),
    ("margin", "两融余额分位", 15, "三所两融余额合计分位; 杠杆资金入场=顺势"),
    ("fflow", "主力资金流", 25, "SW一级主力净流入5日均分位; 主力买入=顺势"),
    ("breadth", "板块温度宽度", 10, "BK板块温度均值分位; 普涨=顺势"),
]
DEFAULT_ENTRY = 30          # 行为指数≥entry → 买入
DEFAULT_EXIT = -30          # ≤exit → 空仓
REGIME_K = 6.0              # 状态划分sigmoid斜率(120日动量)


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def _expanding_pct(pairs):
    """[(date, value)] → [(date, pct(-100~100))]; 只用截至当日的数据(bisect无前视)"""
    out, seen = [], []
    for d, v in pairs:
        if v is None:
            continue
        bisect.insort(seen, v)
        out.append((d, round((bisect.bisect_right(seen, v) / len(seen)) * 200 - 100, 1)))
    return out


def _fetch_factors_impl(path=None):
    """各因子原始序列 {key: [(date, value)]} — 巴菲特经macro模块(TTM口径)"""
    p = path or SENT_DB
    con = sqlite3.connect(f"file:{Path(p).as_posix()}?mode=ro", uri=True, timeout=30)
    out = {}
    out["sentiment"] = con.execute(
        "SELECT date, score FROM sentiment WHERE score IS NOT NULL ORDER BY date"
    ).fetchall()
    out["pe_pct_raw"] = con.execute(
        "SELECT date, alla_pe FROM mkt_val_daily WHERE alla_pe IS NOT NULL ORDER BY date"
    ).fetchall()
    out["margin"] = con.execute(
        "SELECT date, SUM(rzrqye) FROM margin_daily WHERE rzrqye IS NOT NULL "
        "GROUP BY date ORDER BY date").fetchall()
    # 主力净流入: L1按日合计的5日均值
    raw_ff = con.execute(
        "SELECT date, SUM(main_net) FROM sw_fflow WHERE level='L1' "
        "GROUP BY date ORDER BY date").fetchall()
    con.close()
    ff5, buf = [], []
    vals = []
    for i, (d, v) in enumerate(raw_ff):
        vals.append(v or 0.0)
        if i >= 4:
            ff5.append((d, sum(vals[-5:]) / 5))
    out["fflow"] = ff5
    # 板块温度宽度: BK板块日均分
    out["breadth"] = _bk_mean(p)
    # 巴菲特: 复用macro的series(date, ratio) — GDP TTM口径
    try:
        sys.path.insert(0, str(BASE.parent))
        from server import macro as MC
        out["buffett_raw"] = [(x["date"], x["ratio"]) for x in MC.buffett(p)["series"]]
    except Exception:                                   # noqa: BLE001
        out["buffett_raw"] = []
    return out


def _bk_mean(path):
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        return con.execute(
            "SELECT date, AVG(score) FROM sector_scores WHERE code GLOB 'BK*' "
            "AND score IS NOT NULL GROUP BY date ORDER BY date").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


_RAW_MAP = {"pe_pct": "pe_pct_raw", "buffett": "buffett_raw"}

# 因子原始数据进程内缓存(拉取~5s, 主要耗在sw_fflow 186万行GROUP BY);
# 权重/阈值交互计算复用缓存(只读数据, TTL内新鲜度足够)
_fact_cache = {"at": 0.0, "data": None}
_FACT_TTL = 600.0


def fetch_factors(path=None):
    import time as _t
    p_str = str(path or SENT_DB)
    if _fact_cache["data"] is not None and _t.time() - _fact_cache["at"] < _FACT_TTL:
        return _fact_cache["data"]
    data = _fetch_factors_impl(path)
    _fact_cache.update(at=_t.time(), data=data)
    return data


def composite(factors_raw, weights):
    """因子组合 → (dates, values): 各因子expanding分位后按日加权(缺失归一化吸收)"""
    pct = {}
    for key, w in weights.items():
        if not w:
            continue
        raw_key = _RAW_MAP.get(key, key)
        series = factors_raw.get(raw_key) or []
        if series:
            pct[key] = {d: v for d, v in _expanding_pct(series)}
    all_dates = sorted({d for m in pct.values() for d in m})
    out = []
    for d in all_dates:
        s = ww = 0.0
        for key, m in pct.items():
            v = m.get(d)
            if v is not None:
                s += weights[key] * v
                ww += abs(weights[key])
        out.append(round(s / ww, 1) if ww else None)
    return all_dates, out


def regime(dates, closes):
    """牛/熊/震荡概率: 120日动量双sigmoid + 余量截断归一。
    返回 {bull:[], range:[], bear:[]} — 与dates对齐(None在前120日)"""
    bull, rng, bear = [], [], []
    hist = []
    for d, c in zip(dates, closes):
        hist.append(c)
        if c is None or len(hist) < 121:
            bull.append(None); rng.append(None); bear.append(None)
            continue
        m = hist[-1] / hist[-121] - 1.0
        pb, pr = _sigmoid(REGIME_K * m), _sigmoid(-REGIME_K * m)
        pm = max(0.0, 1.0 - pb - pr)
        tot = pb + pr + pm
        bull.append(round(pb / tot * 100, 1))
        bear.append(round(pr / tot * 100, 1))
        rng.append(round(pm / tot * 100, 1))
    return {"bull": bull, "range": rng, "bear": bear}


def backtest(comp_dates, comp_vals, bench, entry=DEFAULT_ENTRY, exit_=DEFAULT_EXIT):
    """阈值买卖回测。bench: [(date, close)] — 信号收盘生成次日执行(无前视)。
    返回 {dates, equity, bench_equity, pos, metrics, n_days}"""
    asof = {}
    for d, v in zip(comp_dates, comp_vals):
        if v is not None:
            asof[d] = v
    pos = 0
    rows = []                       # (date, pos_prev, bench_ret)
    prev_c = None
    for d, c in bench:
        r = (c / prev_c - 1.0) if prev_c else None
        rows.append((d, pos, r))
        v = asof.get(d)             # 收盘后决策, 次日生效
        if v is not None:
            if v >= entry:
                pos = 1
            elif v <= exit_:
                pos = 0
        prev_c = c
    eq = be = 1.0
    dates, eqs, bes, poss = [], [], [], []
    rets = []
    for d, p, r in rows:
        if r is not None:
            sr = r if p == 1 else 0.0
            eq *= 1 + sr
            be *= 1 + r
            rets.append(sr)
        dates.append(d)
        eqs.append(round(eq, 4))
        bes.append(round(be, 4))
        poss.append(p)

    def _cagr(v, n):
        return round((v ** (244.0 / n) - 1) * 100, 1) if n > 0 and v > 0 else None

    peak, mdd = 1.0, 0.0
    for v in eqs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    # 持仓段胜率: pos从0→1的次日入场, 1→0次日离场, 段内基准收益>0为胜
    wins = segs = 0
    seg_start = None
    bmap = {d: c for d, c in bench}
    for i in range(1, len(dates)):
        if poss[i] == 1 and poss[i - 1] == 0:
            seg_start = dates[i]
        if poss[i] == 0 and poss[i - 1] == 1 and seg_start:
            a, b = bmap.get(seg_start), bmap.get(dates[i])
            if a and b:
                segs += 1
                wins += (b / a - 1) > 0
            seg_start = None
    n = len([1 for _, _, r in rows if r is not None])
    mu = sum(rets) / len(rets) if rets else 0
    sd = (sum((x - mu) ** 2 for x in rets) / len(rets)) ** 0.5 if rets else 0
    metrics = {
        "total_ret": round((eq - 1) * 100, 1), "bench_ret": round((be - 1) * 100, 1),
        "cagr": _cagr(eq, n), "bench_cagr": _cagr(be, n),
        "max_dd": round(mdd * 100, 1),
        "win_rate": round(wins / segs * 100, 1) if segs else None,
        "n_trades": segs,
        "sharpe": round(mu / sd * (244 ** 0.5), 2) if sd else None,
        "n_days": n,
    }
    return {"dates": dates, "equity": eqs, "bench_equity": bes, "pos": poss,
            "metrics": metrics}


def _index_close(path=None):
    con = sqlite3.connect(f"file:{Path(path or SENT_DB).as_posix()}?mode=ro",
                          uri=True, timeout=30)
    try:
        idx = con.execute("SELECT date, close FROM index_daily WHERE code='1.000001' "
                          "ORDER BY date").fetchall()
        etf = con.execute("SELECT date, close FROM etf_daily WHERE code='510300.SH' "
                          "ORDER BY date").fetchall()
    finally:
        con.close()
    return idx, etf


def payload(weights=None, entry=DEFAULT_ENTRY, exit_=DEFAULT_EXIT, path=None):
    """API载荷: 因子组合+状态概率+回测(对照510300, 上证兜底)+上证叠加"""
    weights = {k: w for k, w in (weights or {k: dw for k, _, dw, _ in FACTOR_DEFS}).items()
               if isinstance(w, (int, float))}
    factors_raw = fetch_factors(path)
    dates, comp = composite(factors_raw, weights)
    idx, etf = _index_close(path)
    bench, bench_name = (etf, "510300ETF") if len(etf) > 500 else (idx, "上证指数")
    bt = backtest(dates, comp, bench, entry, exit_)
    reg = regime([d for d, _ in idx], [c for _, c in idx])
    # 上证叠加归一化(与组合共轴期间, 首日=100)
    dset = set(dates)
    overlay = []
    base = None
    for d, c in idx:
        if d in dset:
            if base is None:
                base = c
            overlay.append((d, round(c / base * 100, 2)))
    factors_now = {}
    for key, name, dw, desc in FACTOR_DEFS:
        raw_key = _RAW_MAP.get(key, key)
        series = factors_raw.get(raw_key) or []
        pct_series = _expanding_pct(series)
        factors_now[key] = {"name": name, "weight": weights.get(key, 0),
                            "pct": pct_series[-1][1] if pct_series else None}  # as-of最新
    return {"dates": dates, "composite": comp,
            "regime": {"dates": [d for d, _ in idx],
                       "closes": [c for _, c in idx], **reg},
            "overlay_index": {"dates": [d for d, _ in overlay],
                              "norm": [v for _, v in overlay]},
            "backtest": {**bt, "bench_name": bench_name},
            "factors_now": factors_now, "entry": entry, "exit": exit_}
