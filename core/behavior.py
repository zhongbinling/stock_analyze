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
状态划分: 120日动量m → 带阈值划分: 牛=σ(k·(m−带)), 熊=σ(k·(−m−带)), 震荡=1−牛−熊(截断归一)。
回测: c≥entry持有多头, c≤exit空仓(滞回); 信号收盘后才算得出 → 下一交易日收盘成交(无前视)。
"""
import bisect
import math
import sqlite3
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

SENT_DB = BASE / "data" / "sentiment.db"
_PRIMARY = "1.000001"          # 上证指数在 index_daily 中的代码

# (key, 名称, 默认权重, 说明)
FACTOR_DEFS = [
    ("sentiment", "情绪热度", -30, "情绪分expanding分位; 高热=贪婪(历史IC为负→默认反向权重)"),
    ("pe_pct", "全A PE分位", -25, "全A整体法PE历史分位; 低估值=买入区(默认负权)"),
    ("buffett", "巴菲特指标分位", -15, "总市值/GDP历史分位; 低=便宜(默认负权)"),
    ("margin", "两融余额分位", 15, "三所两融余额合计分位; 杠杆资金入场=顺势"),
    ("fflow", "主力资金流", 25, "SW一级主力净流入5日均分位; 主力买入=顺势"),
    ("breadth", "板块温度宽度", 10, "BK板块温度均值分位; 普涨=顺势"),
    # D1-31b: 用户追加 — 牛熊比(反向) + 国家队ETF净申购(份额口径非成交量推断)
    ("bb_ratio", "牛熊比(牛-熊)", -10, "120日动量牛熊概率差×100(tanh); 极度熊市=历史底部区(默认反向小权重)"),
    ("nt_flow", "国家队ETF净申购", 15, "8只国家队宽基ETF日净申购额(份额差×收盘价)5日均分位; 大额申购=护盘/长线入场(2006起全史)"),
]
DEFAULT_ENTRY = 30          # 行为指数≥entry → 买入
DEFAULT_EXIT = -30          # ≤exit → 空仓
# 状态划分(D1-38): 120日动量m 相对"带"判三态 — 带内=震荡, 上穿=牛, 下穿=熊。
# REGIME_BAND 带的半宽: 上证5196个交易日里约半数落在±10%以内(±5%占30%, ±15%占68%),
# 取10%让三种状态都有足够样本。REGIME_K 带边缘的过渡陡度(k=20时, 动量在带缘
# ±5个百分点内从"基本是震荡"过渡到"基本是牛/熊")。
# 此前: 牛=σ(k·m)、熊=σ(−k·m) — 两者恒和为1, 震荡=1−牛−熊≤0 永远是0
# (生产库5316个交易日里震荡概率最大值0.0, 页面常年显示"震荡 0.0%"): 三态只有两态
REGIME_BAND = 0.10
REGIME_K = 20.0
# 牛熊比(图上的橙线, 也是组合里的 bb_ratio 因子): tanh(BB_K·m/2)×100 — 保持原公式。
# D1-38: 曾改成新三态模型下的"牛−熊概率差" — 它在带边缘很陡, 曲线成了贴着±100的方波;
# 而因子只用到历史分位(对单调变换不变), 换公式没有任何收益, 反而让默认行为指数
# 在27个交易日偏移超过5个点。它与牛−熊概率同号、同在 m=0 过零。
BB_K = 6.0


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def _regime_probs(m):
    """120日动量 m → (牛, 熊, 震荡), 三者之和为1; 关于 m=0 对称、随 m 单调"""
    bull = _sigmoid(REGIME_K * (m - REGIME_BAND))
    bear = _sigmoid(REGIME_K * (-m - REGIME_BAND))
    rng = max(0.0, 1.0 - bull - bear)
    tot = bull + bear + rng
    return bull / tot, bear / tot, rng / tot


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
    """各因子原始序列 {key: [(date, value)]} — 巴菲特经macro模块(TTM口径)。
    D1-38: 库或源表不存在时该数据源返回空序列(原直接抛错, 整个计算接口500 —
    新装机、库还没建全时打开回测页只看到一句 HTTP 500)"""
    p = path or SENT_DB
    out = {}
    try:
        con = sqlite3.connect(f"file:{Path(p).as_posix()}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        con = None

    def q(sql):
        try:
            return con.execute(sql).fetchall()
        except sqlite3.Error:
            return []
    if con is not None:
        out["sentiment"] = q(
            "SELECT date, score FROM sentiment WHERE score IS NOT NULL ORDER BY date")
        out["pe_pct_raw"] = q(
            "SELECT date, alla_pe FROM mkt_val_daily WHERE alla_pe IS NOT NULL ORDER BY date")
        out["margin"] = q(
            "SELECT date, SUM(rzrqye) FROM margin_daily WHERE rzrqye IS NOT NULL "
            "GROUP BY date ORDER BY date")
        # 主力净流入: L1按日合计的5日均值
        raw_ff = q(
            "SELECT date, SUM(main_net) FROM sw_fflow WHERE level='L1' "
            "GROUP BY date ORDER BY date")
        con.close()
    else:
        raw_ff = []
    ff5, buf = [], []
    vals = []
    for i, (d, v) in enumerate(raw_ff):
        vals.append(v or 0.0)
        if i >= 4:
            ff5.append((d, sum(vals[-5:]) / 5))
    out["fflow"] = ff5
    # 板块温度宽度: BK板块日均分
    out["breadth"] = _bk_mean(p)
    # D1-31b: 国家队ETF净申购(份额差×收盘价, 2006起全史) — 5日均平滑
    try:
        import fetch_etf_flow as _EF
        nf = _EF.net_flow_series(path=p, n_days=6000)   # [{date, net_yi}]
        raw_nt = [(x["date"], x["net_yi"]) for x in nf]
        out["nt_daily"] = raw_nt          # 按日净申购: 累计线用(D1-38)
        vals = []
        nt5 = []
        for i, (d, v) in enumerate(raw_nt):
            vals.append(v or 0.0)
            if i >= 4:
                nt5.append((d, round(sum(vals[-5:]) / 5, 2)))
        out["nt_flow"] = nt5
    except Exception:                                        # noqa: BLE001
        out["nt_flow"] = []
        out["nt_daily"] = []
    # 巴菲特: 复用macro的series(date, ratio) — GDP TTM口径
    try:
        sys.path.insert(0, str(BASE.parent))
        from server import macro as MC
        out["buffett_raw"] = [(x["date"], x["ratio"]) for x in MC.buffett(p)["series"]]
    except Exception:                                   # noqa: BLE001
        out["buffett_raw"] = []
    return out


def _bk_mean(path):
    """板块温度宽度: BK板块日均分; 库或表不存在 → 空序列"""
    try:
        con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            return con.execute(
                "SELECT date, AVG(score) FROM sector_scores WHERE code GLOB 'BK*' "
                "AND score IS NOT NULL GROUP BY date ORDER BY date").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


_RAW_MAP = {"pe_pct": "pe_pct_raw", "buffett": "buffett_raw", "bb_ratio": "bb_raw"}

# 因子原始数据进程内缓存(拉取1~11s, 随磁盘缓存与并发写入波动)。
# 是否有效 = 同一个库 且 各数据源的最新日期/行号都没变 且 未超过 _FACT_TTL。
# D1-38: 原只看"距上次计算是否满10分钟" — 更新链刚写完新的一天, 10分钟内仍给旧数据;
# 且缓存不区分库文件(算出的路径没被用上), 不同库的调用方会互相拿到对方的数据。
# (不能用库文件时间戳判断: 库里还有行情/新闻等与本页无关的表在随时写)
# pull_latest 写库后显式置空。
_fact_cache = {"at": 0.0, "data": None, "key": None, "sig": None}
_FACT_TTL = 600.0
_fact_lock = threading.Lock()
# 各数据源的变化探测: 有索引的取最新日期, 没有索引的取最大行号(只追加的表, 都在1毫秒内)
_SIG_SQL = (
    "SELECT MAX(date) FROM sentiment",
    "SELECT MAX(date) FROM mkt_val_daily",
    "SELECT MAX(date) FROM margin_daily",
    "SELECT MAX(rowid) FROM sw_fflow",
    "SELECT MAX(rowid) FROM sector_scores",
    "SELECT MAX(trade_date) FROM etf_share",
    "SELECT MAX(rowid) FROM etf_daily",
    "SELECT MAX(date) FROM index_daily WHERE code=?",
)


def _data_signature(p: Path) -> tuple:
    """各数据源的最新日期/行号; 表不存在记None(与"还没有数据"等价)"""
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return ()
    try:
        out = []
        for sql in _SIG_SQL:
            try:
                out.append(con.execute(sql, (_PRIMARY,) if "?" in sql else ()).fetchone()[0])
            except sqlite3.Error:
                out.append(None)
        return tuple(out)
    finally:
        con.close()


def fetch_factors(path=None):
    p = Path(path or SENT_DB)
    key, sig = str(p.resolve()), _data_signature(p)
    with _fact_lock:                        # 并发冷启动只算一次, 其余等缓存
        c = _fact_cache
        if (c["data"] is not None and c.get("key") == key and c.get("sig") == sig
                and time.time() - c["at"] < _FACT_TTL):
            return c["data"]
        data = _fetch_factors_impl(path)
        c.update(at=time.time(), data=data, key=key, sig=sig)
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


def _momentum(hist):
    """120日动量(需至少121个收盘价, 且窗口两端都有值)"""
    if len(hist) < 121 or hist[-1] is None or hist[-121] is None:
        return None
    return hist[-1] / hist[-121] - 1.0


def regime(dates, closes):
    """牛/熊/震荡概率: 120日动量带阈值双sigmoid + 余量截断归一(见REGIME_BAND处注释)。
    返回 {bull:[], range:[], bear:[]} — 与dates对齐(None在前120日)"""
    bull, rng, bear = [], [], []
    hist = []
    for d, c in zip(dates, closes):
        hist.append(c)
        m = _momentum(hist) if c is not None else None
        if m is None:
            bull.append(None); rng.append(None); bear.append(None)
            continue
        pb, pr, pm = _regime_probs(m)
        bull.append(round(pb * 100, 1))
        bear.append(round(pr * 100, 1))
        rng.append(round(pm * 100, 1))
    return {"bull": bull, "range": rng, "bear": bear}


def backtest(comp_dates, comp_vals, bench, entry=DEFAULT_ENTRY, exit_=DEFAULT_EXIT):
    """阈值买卖回测。bench: [(date, close)] — 信号在当日收盘后才算得出, 下一交易日收盘成交。

    D1-38 review: 原实现在"信号日收盘"就成交 — 信号用的是当日收盘后的数据(情绪分盘后
    生成, 两融/ETF份额要到次日才发布), 那个收盘价是买不到的, 等于多赚了"信号日收盘→
    次日收盘"这一段。生产数据实测影响: 仅主力资金流 37.6%→-17.7%, 仅国家队净申购
    7.1%→-32.6%(换手越快的信号被高估越多); 慢信号(牛熊比)几乎不受影响。
    返回 {dates, equity, bench_equity, pos, metrics, n_days}; pos=当日收益所用的仓位"""
    asof = {}
    for d, v in zip(comp_dates, comp_vals):
        if v is not None:
            asof[d] = v
    pos = 0
    pending = None                  # 已由信号决定、待下一交易日收盘执行的仓位
    rows = []                       # (date, pos, bench_ret)
    prev_c = None
    for d, c in bench:
        if c is None:                      # 基准当天没有收盘价: 这一天不计收益(净值顺延到下一个有价日)
            r = None
        else:
            r = (c / prev_c - 1.0) if prev_c else None
        rows.append((d, pos, r))    # 当日收益按此前已生效的仓位结算
        if pending is not None:     # 上一交易日的信号在今日收盘执行, 次日起计入收益
            pos, pending = pending, None
        v = asof.get(d)
        if v is not None:
            if v >= entry:
                pending = 1
            elif v <= exit_:
                pending = 0
        if c is not None:
            prev_c = c

    eq = be = 1.0
    dates, eqs, bes, poss = [], [], [], []
    rets = []
    seg_open = None                  # 当前持仓段入场时的策略净值(未舍入)
    wins = segs = 0
    for d, p, r in rows:
        if r is not None:
            if p == 1 and seg_open is None:
                seg_open = eq       # 入场=这段第一个持仓日的上一天收盘
            eq *= 1 + (r if p == 1 else 0.0)
            be *= 1 + r
            rets.append(r if p == 1 else 0.0)
            if p == 0 and seg_open is not None:
                segs += 1           # 持仓段平仓: 段内净值上涨为胜
                wins += eq > seg_open
                seg_open = None
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
    n = len(rets)
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
    """上证与510300的(日期, 收盘)序列; 库或表不存在时该序列为空(两者互不影响:
    510300表缺失时回测回落到上证, 不能连上证也一起丢掉)"""
    try:
        con = sqlite3.connect(f"file:{Path(path or SENT_DB).as_posix()}?mode=ro",
                              uri=True, timeout=30)
    except sqlite3.Error:
        return [], []

    def q(sql):
        try:
            return con.execute(sql).fetchall()
        except sqlite3.Error:
            return []
    try:
        idx = q("SELECT date, close FROM index_daily WHERE code='1.000001' ORDER BY date")
        etf = q("SELECT date, close FROM etf_daily WHERE code='510300.SH' ORDER BY date")
    finally:
        con.close()
    return idx, etf


def _bb_series(idx):
    """牛熊比: tanh(BB_K·m/2)×100 (m=120日动量), −100深熊~+100强牛; 与regime用同一个动量,
    同在 m=0 过零(见 BB_K 处注释: 不用新模型下的牛−熊概率差)"""
    out, hist = [], []
    for d, c in idx:
        hist.append(c)
        m = _momentum(hist) if c is not None else None
        out.append((d, None if m is None else round(math.tanh(BB_K * m / 2) * 100, 1)))
    return out


_pull_state = {"at": 0.0}


def pull_latest(path=None, min_gap_s: float = 600.0) -> dict:
    """按需拉取份额最新数据(回测页加载/点[刷新数据]时调用)。

    份额源 fund_share: D日数据 D+1 上午才发布(实测 D+1 04:05 未出、08:52 已有),
    而库只在 16:35 更新链时刷新 → 中间大半天回测页末端是空柱(11-24 04:00
    用户实测 11-23 为"-" — 源端当时确实还没出, 但发布后页面也要等到晚上)。
    份额落后于最新交易日时主动拉一次(8次接口调用), 进程内节流 min_gap_s;
    接口失败/未发布时沿用库内数据。
    返回 {pulled, stale, rows, advanced, latest}: rows=接口写回的行数(30日窗口
    每次都是整窗upsert, 无新交易日也非零); advanced=份额是否推进到新的一天。"""
    import time as _t
    p = Path(path or SENT_DB)

    def _last():
        con = None
        try:
            con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=30)
            a = con.execute("SELECT MAX(trade_date) FROM etf_share").fetchone()[0]
            b = con.execute("SELECT MAX(date) FROM index_daily WHERE code=?",
                            (_PRIMARY,)).fetchone()[0]
            return a or "", (b or "").replace("-", "")
        except sqlite3.Error:                  # 库/表不存在 → 无从判断落后
            return "", ""
        finally:
            if con is not None:
                con.close()

    last, last_idx = _last()
    if not last_idx or last >= last_idx:
        return {"pulled": False, "stale": False, "rows": 0,
                "advanced": False, "latest": last or None}
    now = _t.time()
    if now - _pull_state["at"] < min_gap_s:
        return {"pulled": False, "stale": True, "rows": 0,
                "advanced": False, "latest": last or None}
    _pull_state["at"] = now
    rows = 0
    try:
        import fetch_etf_flow as EF
        rows = EF.refresh(path=p)
    except Exception as e:  # noqa: BLE001
        print(f"[behavior] 份额按需拉取失败(沿用库内数据): {e}")
    with _fact_lock:                               # 与并发的因子重算互斥
        _fact_cache.update(at=0.0, data=None)      # 因子缓存失效, 下次计算重载
    last2, _ = _last()
    return {"pulled": True, "stale": last2 < last_idx,
            "rows": rows if isinstance(rows, int) and rows > 0 else 0,
            "advanced": last2 > last, "latest": last2 or None}


def payload(weights=None, entry=DEFAULT_ENTRY, exit_=DEFAULT_EXIT, path=None):
    """API载荷: 因子组合+状态概率+回测(对照510300, 上证兜底)+上证叠加
    D1-31b: 牛熊比因子+regime线、国家队ETF动向(日净申购+累计持仓proxy)
    D1-38: 累计线按日净申购累加; 国家队图的上证叠加从该图首日归一;
    factor_joins=各因子开始有数据的日期(组合按当日可得因子加权, 早期只有部分因子参与)"""
    weights = {k: w for k, w in (weights or {k: dw for k, _, dw, _ in FACTOR_DEFS}).items()
               if isinstance(w, (int, float))}
    factors_raw = dict(fetch_factors(path))       # 浅拷贝: 缓存对象是各请求共享的, 不在它上面写
    idx, etf = _index_close(path)
    factors_raw["bb_raw"] = _bb_series(idx)          # 牛熊比由上证派生
    dates, comp = composite(factors_raw, weights)
    bench, bench_name = (etf, "510300ETF") if len(etf) > 500 else (idx, "上证指数")
    bt = backtest(dates, comp, bench, entry, exit_)
    reg = regime([d for d, _ in idx], [c for _, c in idx])
    bb = _bb_series(idx)
    # 国家队动向: 柱=日净申购5日均(亿), 线=累计净申购(持仓proxy)。
    # D1-38: 累计线按"日净申购"累加 — 原累加的是5日均值: 最近4天各只被计入1/5~4/5
    # (5日均的累加与按日累加只差首尾各4天, 所以此前没被发现)。
    # 日净申购序列本身的缺陷(非交易日的份额行被当成比较基准等)见 fetch_etf_flow.net_flow_series
    nt_daily = factors_raw.get("nt_daily") or factors_raw.get("nt_flow") or []
    ma5_by_date = dict(factors_raw.get("nt_flow") or [])
    nt_dates, nt_ma5, nt_cum = [], [], []
    cum = 0.0
    for d, v in nt_daily:
        nt_dates.append(d)
        nt_ma5.append(ma5_by_date.get(d))
        cum += v or 0.0
        nt_cum.append(round(cum, 1))
    last_nt = nt_dates[-1] if nt_dates else None
    if last_nt:
        # D1-32: 份额源T+1 — 轴延伸至最新交易日(与状态/组合图对齐):
        # 无数据日柱缺省(无净申购记录), 累计线维持走平(持仓不变语义)
        for d, _c in idx:
            if d > last_nt:
                nt_dates.append(d)
                nt_ma5.append(None)
                nt_cum.append(nt_cum[-1] if nt_cum else None)
    # 上证叠加归一化: 行为指数图以组合首日=100; 国家队图以该图自己的首日=100
    # (原共用前者: 国家队数据晚于组合起步, 叠加线一上来就不是100)
    dset = set(dates)
    overlay, nt_overlay = [], []
    base = nt_base = None
    for d, c in idx:
        if c is None or not c:
            continue                    # 没有收盘价的日子不参与归一
        if d in dset:
            if base is None:
                base = c
            overlay.append((d, round(c / base * 100, 2)))
        if nt_dates and d >= nt_dates[0]:
            if nt_base is None:
                nt_base = c
            nt_overlay.append((d, round(c / nt_base * 100, 2)))
    factors_now = {}
    joins = []
    have = {}                       # 所选因子 → (名称, 有数据的日期集合): 判断最新读数是否完整
    first = dates[0] if dates else ""
    for key, name, dw, desc in FACTOR_DEFS:
        raw_key = _RAW_MAP.get(key, key)
        series = factors_raw.get(raw_key) or []
        if key == "bb_ratio":
            series = [(d, v) for d, v in bb if v is not None]
        pct_series = _expanding_pct(series)
        factors_now[key] = {"name": name, "weight": weights.get(key, 0),
                            "pct": pct_series[-1][1] if pct_series else None,  # as-of最新
                            "latest": series[-1][0] if series else None}  # 数据截至
        start = series[0][0] if series else None
        if weights.get(key) and start and start > first:
            joins.append({"date": start, "key": key, "name": name})
        if weights.get(key) and pct_series:
            have[key] = (name, {d for d, _v in pct_series})
    joins.sort(key=lambda x: x["date"])
    # D1-38(独立评审): 最新交易日的读数是不是"暂定值"。组合按当日有数据的因子归一 —
    # 两融、ETF份额这类次日才入库的数据, 在入库前最新一天只有部分因子参与, 读数与
    # 历史口径(也就是回测用的口径)不可比: 默认权重下两者中位相差约12点, 约2成的
    # 交易日"是否越过买卖线"不同。给出缺哪些因子、以及最近一个读数完整的日期
    provisional = None
    if dates and have:
        missing = [nm for _k, (nm, ds) in have.items() if dates[-1] not in ds]
        if missing:
            need = [ds for _nm, ds in have.values()]
            done = next((i for i in range(len(dates) - 1, max(-1, len(dates) - 120), -1)
                         if all(dates[i] in ds for ds in need)), None)
            provisional = {"missing": missing, "n_factors": len(have),
                           "complete_date": dates[done] if done is not None else None,
                           "complete_value": comp[done] if done is not None else None}
    return {"dates": dates, "composite": comp,
            "regime": {"dates": [d for d, _ in idx],
                       "closes": [c for _, c in idx],
                       "bb": [v for _, v in bb], **reg},
            "nt": {"dates": nt_dates, "ma5": nt_ma5, "cum": nt_cum,
                   "data_date": last_nt},       # 有真实份额数据的最后一天(末端为T+1延伸)
            "overlay_index": {"dates": [d for d, _ in overlay],
                              "norm": [v for _, v in overlay]},
            "nt_overlay": {"dates": [d for d, _ in nt_overlay],
                           "norm": [v for _, v in nt_overlay]},
            "factor_joins": joins,
            "provisional": provisional,       # 非 None: 最新读数缺因子(暂定值), 见上
            "backtest": {**bt, "bench_name": bench_name},
            "factors_now": factors_now, "entry": entry, "exit": exit_,
            "params": {"band": REGIME_BAND}}
