# -*- coding: utf-8 -*-
"""D1-38 历史回测页 review 回归 — 对照生产 sentiment.db 与页面实跑发现的问题

① 市场状态"震荡"恒为0: 牛=σ(k·m)、熊=σ(−k·m) 恒和为1, 震荡=1−牛−熊≤0
   (生产库5316个交易日震荡概率最大值0.0, 页面常年"震荡 0.0%") → 带阈值三态划分
② 国家队累计线累加的是5日均值: 末端2511亿 vs 按日累计3188亿 — 最近4天各只计入1/5~4/5,
   恰在国家队大额买入时最不准
③ 国家队图的上证叠加线共用行为指数图的归一序列: 起点早两年, 一上来就是115而不是100
④ 行为指数不标因子加入日期: 2005年平均1.7个因子参与, 2019年起才是8个
⑤ 因子缓存: 不区分库文件; 只按10分钟过期(更新链写完仍给旧数据); 并发冷启动重复算
⑥ 接口不校验: 只传未知因子/权重全0 → 空组合; 买入≤卖出 → 无意义结果; 保存策略不校验
⑦ 页面: 连续计算时先发后到的旧结果覆盖新结果; 不校验就发请求; 同名策略静默覆盖;
   策略列表看不到构成; 空数据显示成 undefined
⑧ 份额序列取价对每个交易日单查一次(5000个交易日近3秒)
⑨ (独立评审) 国家队日净申购原有缺陷: 按"上一条有份额记录的日期"整体配对 — 份额表里的
   非交易日行(季末报告日、周六)常只有一两只ETF, 它成了下一个交易日的基准, 其余ETF
   当天的申赎整段丢失(2026-01-19 显示-24.8亿, 实为-379亿); 个别ETF缺收盘价那天的
   变化也丢。累计 3188.3亿 → 2640.3亿, 133天净额不对。情绪看板的国家队副图同源
⑩ (独立评审) 最新读数缺两融/份额时是暂定值(与最终值中位差11.7点, 21%的交易日是否
   过线不同), 页面直接写作"当前"; 校验失败的提示被在途请求冲掉; 策略列表没加载时
   同名保存静默覆盖; 阈值传 Infinity → 500; 源表缺失/指数收盘价为空 → 500
"""
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import behavior as BH        # noqa: E402
import fetch_etf_flow as EF  # noqa: E402

HTML = REPO / "server" / "templates" / "admin" / "backtest.html"
HARNESS = REPO / "tests" / "backtest_page_behavior.js"


def _dates(n, start="2024-01-01"):
    import datetime as dt
    d0 = dt.date.fromisoformat(start)
    return [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]


# ════════════ ① 市场状态三态 ════════════

def test_regime_flat_market_is_range_not_half_bull_half_bear():
    """横盘: 震荡概率应为主。原公式下横盘=牛50%/熊50%/震荡0%"""
    dates = _dates(400)
    closes = [3000 + 60 * ((-1) ** i) * (0.3 + 0.7 * abs(__import__("math").sin(i / 9)))
              for i in range(400)]                        # 在±2%内来回, 无趋势
    r = BH.regime(dates, closes)
    tail = slice(130, None)
    assert min(r["range"][tail]) > 50, "横盘时震荡概率应过半"
    assert max(r["bull"][tail]) < 20 and max(r["bear"][tail]) < 20
    for b, g, e in zip(r["bull"][tail], r["range"][tail], r["bear"][tail]):
        assert abs(b + g + e - 100) < 0.15


def test_regime_trends_and_symmetry():
    """单边涨→牛, 单边跌→熊; 关于零动量对称; 买卖阈值带由 REGIME_BAND 决定"""
    dates = _dates(400)
    up = [100 * 1.003 ** i for i in range(400)]           # 120日动量 ≈ +43%
    dn = [100 * 1.003 ** -i for i in range(400)]          # 120日动量 ≈ −30%
    ru, rd = BH.regime(dates, up), BH.regime(dates, dn)
    assert ru["bull"][-1] > 99 and ru["range"][-1] < 1 and ru["bear"][-1] < 0.1
    assert rd["bear"][-1] > 95 and rd["range"][-1] < 5 and rd["bull"][-1] < 1
    assert ru["bull"][-1] > rd["bear"][-1]                # 涨得更猛: 牛概率更高(不是关于涨幅对称)
    # 关于零动量对称: +43% 的牛概率 = −43% 的熊概率
    for m in (0.05, 0.1, 0.17, 0.43, 0.9):
        assert BH._regime_probs(m)[0] == pytest.approx(BH._regime_probs(-m)[1])
    # 带内(动量≈0): 震荡最高; 出带: 对应状态概率升到一半以上
    p0, pm, pp = BH._regime_probs(0.0), BH._regime_probs(-BH.REGIME_BAND), BH._regime_probs(BH.REGIME_BAND)
    assert p0[2] > 0.7 and abs(p0[0] - p0[1]) < 1e-9
    assert pp[0] == pytest.approx(0.5, abs=1e-9) and pm[1] == pytest.approx(0.5, abs=1e-9)
    for m in (-0.9, -0.3, -0.1, 0.0, 0.02, 0.1, 0.25, 1.2):   # 任何动量下三态非负且和为1
        b, e, g = BH._regime_probs(m)
        assert min(b, e, g) >= 0 and b + e + g == pytest.approx(1.0)


def test_bb_keeps_original_formula_and_agrees_in_sign_with_regime():
    """牛熊比保持原公式 tanh(BB_K·m/2)×100(图上的橙线, 也是 bb_ratio 因子):
    与三态用同一个120日动量、同在零动量过零、与"牛−熊"同号。
    (D1-38: 曾改成新模型下的牛−熊概率差 — 带边缘太陡, 曲线成了贴着±100的方波;
    因子只用历史分位, 换公式没有收益, 反而让默认行为指数在27天偏移超过5个点)"""
    import math
    dates = _dates(500)
    closes = [3000 * math.exp(0.12 * math.sin(i / 37)) * (1 + 0.004 * math.sin(i * 1.7))
              for i in range(500)]
    r = BH.regime(dates, closes)
    bb = BH._bb_series(list(zip(dates, closes)))
    assert bb[119][1] is None and bb[120][1] is not None              # 满121个收盘价才有值
    for i in range(121, 500):
        m = closes[i] / closes[i - 120] - 1
        assert bb[i][1] == pytest.approx(round(math.tanh(BH.BB_K * m / 2) * 100, 1))
        assert (bb[i][1] > 0) == (r["bull"][i] > r["bear"][i]) or bb[i][1] == 0
    up = BH._bb_series([(f"d{i}", 100.0 * 1.003 ** i) for i in range(150)])
    dn = BH._bb_series([(f"d{i}", 3000.0 * 0.997 ** i) for i in range(150)])
    assert up[-1][1] > 50 and dn[-1][1] < -50 and up[0][1] is None
    # 平滑: 动量每变动1个百分点, 牛熊比变动不超过10(方波版在带边缘会远超这个数)
    ms = [-0.3 + 0.01 * k for k in range(61)]
    vals = [math.tanh(BH.BB_K * m / 2) * 100 for m in ms]
    assert max(abs(b - a) for a, b in zip(vals, vals[1:])) < 10
    for m in (0.1, 0.2, 0.3, 0.57, 1.0):                              # 强牛市各档仍能分出高下
        v = math.tanh(BH.BB_K * m / 2) * 100
        assert v < 99.9, m


# ════════════ ②③④ payload ════════════

def _stub(monkeypatch, raw, idx, etf=None):
    monkeypatch.setattr(BH, "fetch_factors", lambda path=None: dict(raw))
    monkeypatch.setattr(BH, "_index_close", lambda path=None: (idx, etf or []))


def test_nt_cumulative_sums_daily_flows(monkeypatch):
    """累计线=按日净申购逐日累加。生产实测: 原累加5日均值, 末端2511亿/真实3188亿"""
    dates = _dates(13)
    vals = [1, 1, 1, 1, 1, 1, 1, 1, 1, 159.21, 334.37, 670.2, 137.01]     # 末4天大额申购
    daily = list(zip(dates, vals))
    ma5 = [(dates[i], round(sum(vals[i - 4:i + 1]) / 5, 2)) for i in range(4, len(vals))]
    _stub(monkeypatch, {"sentiment": [(d, 1.0) for d in dates], "nt_daily": daily, "nt_flow": ma5},
          [(d, 3000.0 + i) for i, d in enumerate(dates)])
    nt = BH.payload({"sentiment": -30})["nt"]
    assert nt["cum"] == [pytest.approx(sum(vals[:i + 1]), abs=0.05) for i in range(len(vals))]
    assert nt["cum"][-1] == pytest.approx(sum(vals), abs=0.01)          # 1315.78
    assert nt["ma5"][:4] == [None, None, None, None] and nt["ma5"][4] == 1.0
    # 原口径(累加5日均值): 最近4天(旧→新)各只被计入4/5,3/5,2/5,1/5, 最早4天不计 —
    # 与生产上少计的677.2亿(真实累计3188.3 − 页面2511.1)是同一算式
    lost = nt["cum"][-1] - sum(v for _d, v in ma5)
    assert lost == pytest.approx(159.21 * 0.2 + 334.37 * 0.4 + 670.2 * 0.6 + 137.01 * 0.8
                                 + (1 + 2 + 3 + 4) * 0.2, abs=0.1)


def test_nt_falls_back_to_ma_series_without_daily(monkeypatch):
    """没有按日序列(只有5日均)时沿用旧口径, 不报错"""
    d = _dates(6)
    _stub(monkeypatch, {"sentiment": [(x, 1.0) for x in d], "nt_flow": [(d[4], 50.0)]},
          [(x, 3000.0) for x in d])
    nt = BH.payload({"sentiment": -30})["nt"]
    assert nt["ma5"] == [50.0, None] and nt["cum"] == [50.0, 50.0] and nt["data_date"] == d[4]


def test_nt_overlay_normalized_on_its_own_first_day(monkeypatch):
    """国家队图的上证叠加从该图自己的首日=100起; 行为指数图仍从组合首日=100起"""
    idx = [("2005-01-04", 1000.0), ("2005-07-01", 1200.0), ("2006-04-04", 1380.0), ("2006-04-05", 1400.0)]
    raw = {"sentiment": [("2005-01-04", 1.0), ("2006-04-04", 2.0)],
           "nt_daily": [("2006-04-04", 5.0)], "nt_flow": [("2006-04-04", 5.0)]}
    _stub(monkeypatch, raw, idx)
    r = BH.payload({"sentiment": -30})
    assert r["overlay_index"]["dates"][0] == "2005-01-04" and r["overlay_index"]["norm"][0] == 100.0
    assert r["nt_overlay"]["dates"] == ["2006-04-04", "2006-04-05"]
    assert r["nt_overlay"]["norm"][0] == 100.0 and r["nt_overlay"]["norm"][1] == pytest.approx(101.45, abs=0.01)


def test_payload_does_not_write_into_the_shared_cache(tmp_path, monkeypatch):
    """因子缓存对象被各请求共享: payload 需要的派生键(bb_raw)不能写到缓存上"""
    d = _dates(10)
    raw = {"sentiment": [(x, 1.0) for x in d]}
    db = tmp_path / "x.db"
    _sig_db(db)
    monkeypatch.setattr(BH, "_fact_cache", {"at": 0.0, "data": None, "key": None, "sig": None})
    monkeypatch.setattr(BH, "_fetch_factors_impl", lambda path=None: dict(raw))
    monkeypatch.setattr(BH, "_index_close", lambda path=None: ([(x, 3000.0) for x in d], []))
    BH.payload({"sentiment": -30}, path=db)
    cached = BH._fact_cache["data"]
    assert cached is not None and "bb_raw" not in cached and "sentiment" in cached
    BH.payload({"sentiment": -30}, path=db)               # 第二次走缓存
    assert "bb_raw" not in BH._fact_cache["data"] and BH._fact_cache["data"] is cached


def test_factor_joins_mark_when_each_factor_starts(monkeypatch):
    """组合按当日有数据的因子加权: 晚于组合起点才有数据的因子给出加入日期"""
    d = _dates(10)
    idx = [(x, 3000.0) for x in d]
    raw = {"sentiment": [(x, 1.0) for x in d[2:]], "pe_pct_raw": [(x, 10.0) for x in d[5:]],
           "margin": [(x, 1.0) for x in d[0:6]]}           # margin 早于组合起点
    _stub(monkeypatch, raw, idx)
    both = BH.payload({"sentiment": -30, "pe_pct": -25, "margin": 15})
    # 组合起点是其中最早有数据的因子(两融 d[0]); 之后才开始有数据的因子按日期列出
    assert both["factor_joins"] == [{"date": d[2], "key": "sentiment", "name": "情绪热度"},
                                    {"date": d[5], "key": "pe_pct", "name": "全A PE分位"}]
    assert both["dates"][0] == d[0] and both["composite"][0] is not None
    assert BH.payload({"sentiment": -30})["factor_joins"] == []     # 只有它一个: 它就是起点
    assert BH.payload({"sentiment": -30, "pe_pct": 0})["factor_joins"] == []   # 权重0=没选
    assert both["params"] == {"band": BH.REGIME_BAND}


# ════════════ ⑤ 因子缓存 ════════════

def _sig_db(path, *sqls):
    con = sqlite3.connect(str(path))
    for t in ("sentiment", "mkt_val_daily", "margin_daily", "sw_fflow", "sector_scores", "etf_daily",
              "index_daily", "unrelated"):
        con.execute(f"CREATE TABLE IF NOT EXISTS {t}(date TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS etf_share(trade_date TEXT)")
    for sql in sqls:
        con.execute(sql)
    con.commit()
    con.close()


def _cache_setup(monkeypatch, tmp_path):
    built = []
    monkeypatch.setattr(BH, "_fact_cache", {"at": 0.0, "data": None, "key": None, "sig": None})
    monkeypatch.setattr(BH, "_fetch_factors_impl",
                        lambda path=None: built.append(Path(path).name) or {"src": Path(path).name})
    return built


def test_factor_cache_follows_source_data_not_the_clock(tmp_path, monkeypatch):
    """数据源没变就复用(无关的表怎么写都不影响); 任一数据源推进 → 立即重算(不等10分钟)"""
    built = _cache_setup(monkeypatch, tmp_path)
    db = tmp_path / "s.db"
    _sig_db(db, "INSERT INTO sentiment VALUES('2026-11-20')")
    assert BH.fetch_factors(db)["src"] == "s.db" and BH.fetch_factors(db)["src"] == "s.db"
    assert built == ["s.db"]
    _sig_db(db, "INSERT INTO unrelated VALUES('2026-11-25')")     # 库里无关的表被写
    BH.fetch_factors(db)
    assert built == ["s.db"], "无关的表被写不应导致重算"
    _sig_db(db, "INSERT INTO sentiment VALUES('2026-11-24')")     # 更新链写入了新的一天
    BH.fetch_factors(db)
    assert built == ["s.db", "s.db"], "数据源推进后应立即重算"
    BH._fact_cache["at"] -= BH._FACT_TTL + 1                      # 超过上限仍重算(兜住同日改写)
    BH.fetch_factors(db)
    assert len(built) == 3


def test_factor_cache_is_per_database_file(tmp_path, monkeypatch):
    """缓存区分库文件 — 原算出了路径却没用上, 不同库的调用方互相拿到对方的数据"""
    built = _cache_setup(monkeypatch, tmp_path)
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _sig_db(a)
    _sig_db(b)
    assert BH.fetch_factors(a)["src"] == "a.db"
    assert BH.fetch_factors(b)["src"] == "b.db", "拿到了另一个库的缓存"
    assert BH.fetch_factors(b)["src"] == "b.db" and BH.fetch_factors(a)["src"] == "a.db"
    assert built == ["a.db", "b.db", "a.db"]


def test_factor_cache_builds_once_under_concurrency(tmp_path, monkeypatch):
    built = _cache_setup(monkeypatch, tmp_path)
    db = tmp_path / "s.db"
    _sig_db(db)

    def slow(path=None):
        built.append(1)
        time.sleep(0.25)
        return {"x": 1}
    monkeypatch.setattr(BH, "_fetch_factors_impl", slow)
    out = []
    th = [threading.Thread(target=lambda: out.append(BH.fetch_factors(db))) for _ in range(8)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    assert len(built) == 1 and len(out) == 8


def test_data_signature_cheap_and_missing_tables_ok(tmp_path):
    db = tmp_path / "none.db"                          # 库不存在: 不抛错
    assert BH._data_signature(db) == ()
    db = tmp_path / "s.db"
    _sig_db(db)
    assert len(BH._data_signature(db)) == len(BH._SIG_SQL)
    t = time.time()
    for _ in range(50):
        BH._data_signature(db)
    assert time.time() - t < 0.5


# ════════════ ⑨ 回测的成交时点 ════════════

def test_backtest_cannot_trade_at_the_signal_days_close():
    """信号日收盘后才算得出(情绪分盘后生成, 两融/份额次日发布) → 那个收盘价买不到。
    时间线: d2收盘后出信号 → d3收盘成交 → d4起的收益归策略"""
    d = [f"2026-01-{i:02d}" for i in range(1, 9)]
    comp = [None, 50, 50, 50, 50, 50, 50, 50]                     # d2起一直是买入信号
    jump_after_fill = [100, 100, 100, 120, 120, 120, 120, 120]    # d3收盘→d4收盘 +20%(成交之后)
    jump_before_fill = [100, 100, 120, 120, 120, 120, 120, 120]   # d2收盘→d3收盘 +20%(成交之前)
    r = BH.backtest(d, comp, list(zip(d, jump_after_fill)), 30, -30)
    assert r["metrics"]["total_ret"] == 20.0 and r["pos"] == [0, 0, 0, 1, 1, 1, 1, 1]
    r = BH.backtest(d, comp, list(zip(d, jump_before_fill)), 30, -30)
    assert r["metrics"]["total_ret"] == 0.0, "信号日次日的上涨发生在成交之前, 不应算作策略收益"
    assert r["pos"] == [0, 0, 0, 1, 1, 1, 1, 1] and r["equity"] == [1.0] * 8


def test_backtest_exit_waits_one_day_and_win_rate_from_equity():
    """卖出同样等一天: 卖出信号出现后, 下一交易日收盘才卖 — 那一天的涨跌仍归策略;
    胜率按持仓段的净值涨跌算(原算法的入场价、出场价各错位一天)"""
    d = [f"2026-01-{i:02d}" for i in range(1, 14)]
    comp = [None, 50, None, None, -50, None, None, 50, None, None, -50, None, None]
    #        d2买→d3收盘入            d5卖→d6收盘出   d8买→d9收盘入           d11卖→d12收盘出
    closes = [100, 100, 100, 110, 110, 111, 111, 111, 111, 121, 122, 105, 105]
    #        第一段持仓 d4~d6: 100→111 为胜; 第二段持仓 d10~d12: 111→122 后在卖出日跌回105, 为负
    r = BH.backtest(d, comp, list(zip(d, closes)), 30, -30)
    assert r["pos"] == [0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 1, 0]
    m = r["metrics"]
    assert m["n_trades"] == 2 and m["win_rate"] == 50.0
    assert m["total_ret"] == round((111 / 100 * 105 / 111 - 1) * 100, 1)       # 5.0
    assert r["equity"][5] == round(111 / 100, 4) and r["equity"][-1] == round(1.05, 4)


def test_backtest_holds_when_composite_has_no_value():
    """组合没有数值的日子不产生信号(仓位保持); 未平仓的持仓段不计入交易次数"""
    d = [f"2026-01-{i:02d}" for i in range(1, 7)]
    closes = [100, 100, 101, 103, 106, 110]
    r = BH.backtest(d, [None, 50, None, None, None, None], list(zip(d, closes)), 30, -30)
    assert r["pos"] == [0, 0, 0, 1, 1, 1]                     # d2信号 → d3收盘成交(101) → d4起持仓
    assert r["metrics"]["total_ret"] == round((110 / 101 - 1) * 100, 1)
    assert r["metrics"]["n_trades"] == 0 and r["metrics"]["win_rate"] is None


# ════════════ ⑩ 独立评审发现 ════════════

def test_payload_marks_provisional_latest_reading(monkeypatch):
    """最新交易日缺所选因子的数据(两融、ETF份额次日才入库) → 读数是暂定值: 给出缺哪些、
    以及最近一个完整读数。实测当晚读数与数据到齐后中位相差约12点、2成交易日是否越过
    买卖线不同 — 页面原来把它直接写成"当前: X" """
    d = _dates(10)
    raw = {"sentiment": [(x, 1.0) for x in d], "margin": [(x, 1.0) for x in d[:-2]],
           "pe_pct_raw": [(x, 1.0) for x in d[:-1]]}          # 两融缺最近2天, PE缺最近1天
    _stub(monkeypatch, raw, [(x, 3000.0) for x in d])
    r = BH.payload({"sentiment": -30, "margin": 15, "pe_pct": -25})
    pv = r["provisional"]
    assert pv["missing"] == ["全A PE分位", "两融余额分位"] and pv["n_factors"] == 3
    assert pv["complete_date"] == d[-3] and pv["complete_value"] == r["composite"][-3]   # 01-09 起两融就没有了
    assert r["composite"][-1] is not None                  # 暂定值仍然给出
    # 所选因子的数据都到齐 → 不是暂定值
    assert BH.payload({"sentiment": -30})["provisional"] is None
    assert BH.payload({"sentiment": -30, "margin": 15})["provisional"]["missing"] == ["两融余额分位"]
    # 完全没有数据的所选因子不参与判断(它不是"还没到", 而是"没有")。回看120天都凑不齐
    # 一天完整读数 → complete_date 为 None
    assert BH.payload({"sentiment": -30, "margin": 15, "pe_pct": -25, "buffett": -15})[
        "provisional"]["complete_date"] == d[-3]
    _stub(monkeypatch, dict(raw, margin=[(x, 1.0) for x in d[:2]],
                            pe_pct_raw=[(x, 1.0) for x in d[2:4]]), [(x, 3000.0) for x in d])
    assert BH.payload({"sentiment": -30, "margin": 15, "pe_pct": -25})["provisional"] == {
        "missing": ["全A PE分位", "两融余额分位"], "n_factors": 3,
        "complete_date": None, "complete_value": None}


def test_payload_survives_missing_tables_and_null_closes(tmp_path, monkeypatch):
    """库不存在/源表不存在/指数收盘价有空值: 相应数据源为空, 整个计算不抛错(原直接500)"""
    db = tmp_path / "fresh.db"                             # 什么表都没有
    BH._fact_cache.update(at=0.0, data=None)
    r = BH.payload(path=db)
    assert r["dates"] == [] and r["composite"] == [] and r["regime"]["dates"] == []
    assert r["nt"]["dates"] == [] and r["backtest"]["metrics"]["n_days"] == 0
    con = sqlite3.connect(str(db))                         # 只建了部分表、且指数收盘价有空值
    con.execute("CREATE TABLE sentiment(date TEXT, score REAL)")
    con.executemany("INSERT INTO sentiment VALUES(?,?)", [(x, 1.0) for x in _dates(6)])
    con.execute("CREATE TABLE index_daily(code TEXT, date TEXT, close REAL)")
    con.execute("CREATE TABLE mkt_val_daily(date TEXT PRIMARY KEY, alla_pe REAL)")
    con.executemany("INSERT INTO index_daily VALUES('1.000001',?,?)",
                    [(x, None if i in (0, 3) else 3000.0) for i, x in enumerate(_dates(6))])
    con.commit()
    con.close()
    BH._fact_cache.update(at=0.0, data=None)
    r = BH.payload(path=db)
    assert r["dates"] == _dates(6) and len(r["composite"]) == 6
    assert r["regime"]["bull"] == [None] * 6 and r["regime"]["bb"] == [None] * 6
    assert BH._momentum([1.0] * 120 + [None]) is None and BH._momentum([None] + [1.0] * 120) is None
    assert BH._momentum([100.0] * 120 + [110.0]) == pytest.approx(0.10)
    con = sqlite3.connect(str(db))                         # 窗口里有空值、最新一根有值 → 该日无动量
    con.executemany("INSERT INTO index_daily VALUES('1.000001',?,3000.0)", [(x,) for x in _dates(130)[6:]])
    con.commit()
    con.close()
    BH._fact_cache.update(at=0.0, data=None)
    rg = BH.payload(path=db)["regime"]
    assert len(rg["bull"]) == 130 and rg["bull"][-1] is not None
    # 窗口不够121根, 或窗口两端(第i根、第i-120根)落在空值上 → 没有动量:
    # 空值在下标0和3 → i=0..120(第120根的左端正是下标0)与 i=123(左端是下标3)
    closes = [None if i in (0, 3) else 3000.0 for i in range(130)]
    expect_none = [i for i in range(130)
                   if i < 120 or closes[i] is None or closes[i - 120] is None]
    assert [i for i, v in enumerate(rg["bull"]) if v is None] == expect_none


def test_compute_api_infinity_threshold_is_400_not_500(server_app, monkeypatch):
    """JSON 允许 Infinity: int(float('inf')) 抛 OverflowError, 原未被捕获 → 500"""
    import behavior
    monkeypatch.setattr(behavior, "payload", lambda *a, **k: {"dates": ["2026-01-01"]})
    r = server_app["client"].post("/admin/api/backtest/compute",
                                 data='{"factors": {"sentiment": -30}, "entry": Infinity, "exit": -30}',
                                 content_type="application/json")
    assert r.status_code == 400 and "应为整数" in r.get_json()["msg"]
    r = server_app["client"].post("/admin/api/backtest/compute",
                                 data='{"factors": {"sentiment": -30}, "entry": NaN, "exit": -30}',
                                 content_type="application/json")
    assert r.status_code == 400


def test_strategy_overwrite_needs_explicit_flag(server_app):
    """同名策略: 未带 overwrite → 409 且原配置不变; 带 overwrite 才覆盖"""
    c = server_app["client"]
    base = {"name": "同名", "entry": 20, "exit": -20}
    sid = c.post("/admin/api/backtest/strategies", json=dict(base, factors={"pe_pct": -40})).get_json()["id"]
    r = c.post("/admin/api/backtest/strategies", json=dict(base, factors={"pe_pct": -10}))
    assert r.status_code == 409 and r.get_json()["exists"] is True and r.get_json()["ok"] is False
    assert c.get("/admin/api/backtest/strategies").get_json()["strategies"][0]["config"]["factors"] == {"pe_pct": -40}
    assert c.post("/admin/api/backtest/strategies", json=dict(
        base, factors={"pe_pct": -10}, overwrite=True)).get_json()["id"] == sid
    assert c.get("/admin/api/backtest/strategies").get_json()["strategies"][0]["config"]["factors"] == {"pe_pct": -10}


# ════════════ ⑧ 份额序列取价 ════════════

def _flow_db(path, rows_share, rows_price):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE etf_daily(code TEXT, date TEXT, close REAL, PRIMARY KEY(code, date))")
    con.execute("CREATE TABLE etf_share(code TEXT, trade_date TEXT, fd_share REAL, "
                "PRIMARY KEY(code, trade_date))")
    con.executemany("INSERT INTO etf_share VALUES(?,?,?)", rows_share)
    con.executemany("INSERT INTO etf_daily VALUES(?,?,?)", rows_price)
    con.commit()
    con.close()


def _asof_reference(rows_share, rows_price, codes):
    """独立写的参照: 每只ETF"本次份额 − 上次入账时的份额"×当日收盘价;
    入账日=该ETF有收盘价的日子(没收盘价的变化留到它下一个有价日)"""
    share, price = {}, {}
    for c, td, s in rows_share:
        share.setdefault(f"{td[:4]}-{td[4:6]}-{td[6:8]}", {})[c] = s
    for c, d, p in rows_price:
        price.setdefault(d, {})[c] = p
    latest, booked, pending, out = {}, None, set(), []
    for d in sorted(share):
        latest.update(share[d])                     # 截至当天各ETF已知的最新份额
        if booked is None:                          # 首日只作基准
            booked = dict(latest)
            continue
        pending |= set(share[d])
        if d not in price:
            continue
        today = [c for c in codes if c in pending and c in price[d]]   # 今天入账的ETF
        deltas = {c: latest[c] - booked[c] for c in today if c in booked}
        for c in today:
            booked[c] = latest[c]
        pending -= set(today)
        if deltas:
            out.append({"date": d,
                        "net_yi": round(sum(deltas[c] * price[d][c] / 1e4 for c in deltas), 2),
                        "flows": {c: round(v * price[d][c] / 1e4, 2) for c, v in deltas.items()}})
    return out


def test_net_flow_series_single_price_query(tmp_path, monkeypatch):
    """收盘价一次取齐(原每个交易日查一次, 5000个交易日近3秒); 数据没有洞时结果就是
    相邻两日份额差×当日收盘价"""
    codes = EF.ETF_CODES
    import datetime as dt
    d0 = dt.date(2024, 1, 1)
    rows_share, rows_price, expect = [], [], []
    for i in range(40):
        d = (d0 + dt.timedelta(days=i)).isoformat()
        for j, c in enumerate(codes):
            rows_share.append((c, d.replace("-", ""), 1_000_000.0 + i * 50_000 + 10_000 * j))
            rows_price.append((c, d, 1.0 + 0.1 * j))
        if i:
            expect.append({"date": d, "net_yi": round(50_000 * sum(1.0 + 0.1 * j for j in range(8)) / 1e4, 2),
                           "flows": {c: round(50_000 * (1.0 + 0.1 * j) / 1e4, 2) for j, c in enumerate(codes)}})
    db = tmp_path / "s.db"
    _flow_db(db, rows_share, rows_price)
    real_connect = sqlite3.connect
    price_queries = []

    def counting_connect(*a, **k):                        # 统计对 etf_daily 的查询次数
        c = real_connect(*a, **k)
        c.set_trace_callback(lambda sql: price_queries.append(sql) if "FROM etf_daily" in sql else None)
        return c
    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", counting_connect)
        out = EF.net_flow_series(db, n_days=40)
    assert len(price_queries) == 1, f"应只查一次收盘价, 实际 {len(price_queries)} 次"
    assert out == expect and out == _asof_reference(rows_share, rows_price, codes)


def test_net_flow_series_pairs_each_fund_with_its_own_previous_shares(tmp_path):
    """D1-38(独立评审发现, 原有缺陷): 原按"上一条有份额记录的日期"整体配对。份额表里有
    非交易日的行(季末报告日、个别周六), 往往只有一两只ETF — 它成了下一个交易日的比较
    基准, 其余ETF当天的申赎被整段丢掉。生产实测 2026-01-19: 基准是只有 159915 的
    周六行, 显示-24.8亿, 8只合计实为净赎回379亿; 124个交易日净额不对, 累计线虚高205亿"""
    codes = EF.ETF_CODES
    T1, T2, T3, SAT, T4, T5 = ("2026-01-13", "2026-01-14", "2026-01-15", "2026-01-17",
                               "2026-01-19", "2026-01-20")
    s = lambda base, j: base + 10_000.0 * j               # noqa: E731
    rows_share, rows_price = [], []

    def put(day, base, who=range(8)):
        for j in who:
            rows_share.append((codes[j], day.replace("-", ""), s(base, j)))
            if not (day == T2 and j == 1):                # T2 当天 510300 缺收盘价
                rows_price.append((codes[j], day, 1.0 + 0.1 * j))

    put(T1, 1_000_000)                                   # 基准日
    put(T2, 1_050_000)                                   # 各 +5万份×价
    put(T3, 1_100_000, [j for j in range(8) if j != 2])  # 510500 这天没有份额行
    rows_share.append((codes[7], SAT.replace("-", ""), s(1_100_000, 7)))   # 周六: 只有 588000 一行
    put(T4, 600_000)                                     # 各 -50万份×价 (大幅净赎回)
    put(T5, 620_000)                                     # 各 +2万份×价
    db = tmp_path / "s.db"
    _flow_db(db, rows_share, rows_price)
    out = {x["date"]: x for x in EF.net_flow_series(db, n_days=10)}
    p = lambda j: 1.0 + 0.1 * j                           # noqa: E731
    assert set(out) == {T2, T3, T4, T5}                  # T1 是基准日, 周六没有收盘价
    assert out[T2]["net_yi"] == round(50_000 * sum(p(j) for j in range(8) if j != 1) / 1e4, 2)
    assert out[T3]["flows"][codes[1]] == round(100_000 * p(1) / 1e4, 2)      # 缺收盘价那天的变化顺延过来
    assert codes[2] not in out[T3]["flows"]                                 # 没有份额行: 当天不动
    # 周六的单代码行不能当基准: 8只的净赎回都要记在 T4
    assert set(out[T4]["flows"]) == set(codes)
    exp_t4 = {codes[j]: round(-500_000 * p(j) / 1e4, 2) for j in range(8)}
    exp_t4[codes[2]] = round(-450_000 * p(2) / 1e4, 2)          # 510500 少了T3那一步+5万份
    assert out[T4]["flows"] == exp_t4
    assert out[T4]["net_yi"] == round(sum(exp_t4.values()), 2)   # -534.0, 而非只有588000的 -85.0
    assert out[T5]["flows"] == {c: round(20_000 * p(j) / 1e4, 2) for j, c in enumerate(codes)}
    assert [x for x in EF.net_flow_series(db, n_days=10)] == _asof_reference(rows_share, rows_price, codes)


# ════════════ ⑥ 接口校验 ════════════

def test_compute_api_rejects_meaningless_params(server_app):
    """未知因子/权重全0/买入≤卖出/超范围: 400并说明原因(原先照算, 返回空组合或无意义结果)"""
    c = server_app["client"]
    cases = [
        ({"factors": {"no_such_factor": 30}}, "未知因子"),
        ({"factors": {"sentiment": 0, "pe_pct": 0}}, "权重都为0"),
        ({"factors": {"sentiment": -30}, "entry": -30, "exit": 30}, "买入阈值须大于卖出阈值"),
        ({"factors": {"sentiment": -30}, "entry": 20, "exit": 20}, "买入阈值须大于卖出阈值"),
        ({"factors": {"sentiment": -30}, "entry": 101, "exit": -30}, "-100~100"),
        ({"factors": {"sentiment": -30}, "entry": True, "exit": -30}, "应为整数"),
        ({"factors": {"sentiment": -250}, "entry": 30, "exit": -30}, "因子权重"),
        ({"factors": {"sentiment": "大"}, "entry": 30, "exit": -30}, "因子权重"),
        ({"factors": {}}, "至少勾选一个因子"),
        ({"factors": None}, "至少勾选一个因子"),
    ]
    for body, word in cases:
        r = c.post("/admin/api/backtest/compute", json=body)
        assert r.status_code == 400, body
        assert word in r.get_json()["msg"], (body, r.get_json())


def test_compute_api_accepts_valid_params_and_passes_them(server_app, monkeypatch):
    seen = {}

    def fake(weights=None, entry=30, exit_=-30, path=None):
        seen.update(w=weights, e=entry, x=exit_)
        return {"dates": ["2026-01-01"]}
    import behavior
    monkeypatch.setattr(behavior, "payload", fake)
    c = server_app["client"]
    r = c.post("/admin/api/backtest/compute", json={
        "factors": {"sentiment": -40, "pe_pct": 0}, "entry": 40, "exit": -40})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert seen == {"w": {"sentiment": -40}, "e": 40, "x": -40}     # 权重0的因子不进组合
    r = c.post("/admin/api/backtest/compute", json={"factors": {"sentiment": -40}})   # 阈值缺省
    assert r.status_code == 200 and (seen["e"], seen["x"]) == (BH.DEFAULT_ENTRY, BH.DEFAULT_EXIT)


def test_strategy_save_validates_like_compute(server_app):
    c = server_app["client"]
    for body, word in (({"name": "反向阈值", "factors": {"pe_pct": -40}, "entry": -20, "exit": 20}, "买入阈值须大于卖出阈值"),
                       ({"name": "未知", "factors": {"foo": 10}, "entry": 20, "exit": -20}, "未知因子"),
                       ({"name": "超范围", "factors": {"pe_pct": -400}, "entry": 20, "exit": -20}, "因子权重"),
                       ({"name": "全零", "factors": {"pe_pct": 0}, "entry": 20, "exit": -20}, "权重都为0"),
                       ({"name": "超阈值", "factors": {"pe_pct": -40}, "entry": 500, "exit": -20}, "-100~100")):
        r = c.post("/admin/api/backtest/strategies", json=body)
        assert r.status_code == 400 and word in r.get_json()["msg"], body
    assert c.get("/admin/api/backtest/strategies").get_json()["strategies"] == []   # 非法配置没有落库
    ok = c.post("/admin/api/backtest/strategies", json={
        "name": "合理", "factors": {"pe_pct": -40, "sentiment": 0}, "entry": 20, "exit": -20}).get_json()
    assert ok["ok"]
    cfg = c.get("/admin/api/backtest/strategies").get_json()["strategies"][0]["config"]
    assert cfg == {"factors": {"pe_pct": -40}, "entry": 20, "exit": -20}


# ════════════ ⑦ 页面 ════════════

def test_backtest_page_renders(server_app, js_syntax):
    r = server_app["client"].get("/admin/backtest")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    js_syntax(html)                                   # 渲染后的脚本仍过语法门禁
    for mk in ("id=\"regTip\"", "id=\"compTip\"", "按日净申购逐日累加", "灰色竖虚线",
               "策略名[${name}]已存在", "function strategySummary", "if (seq !== computeSeq) return;",
               "function readParams", "r.nt_overlay.norm[i]"):
        assert mk in html, f"缺: {mk}"
    assert html.count("</section>") == html.count("<section"), "section 闭合标签不成对(多了一个)"


def _synthetic_payload():
    """用真实引擎函数在合成数据上生成一份完整返回(不依赖生产库)"""
    import math
    dates = _dates(700, "2023-01-02")
    closes = [3000 * math.exp(0.5 * math.sin(i / 60)) * (1 + 0.012 * math.sin(i * 1.3)) for i in range(700)]
    idx = list(zip(dates, closes))
    nt = [(d, 8 * math.sin(i / 5)) for i, d in enumerate(dates) if i >= 100]
    ma5 = [(nt[i][0], round(sum(v for _d, v in nt[i - 4:i + 1]) / 5, 2)) for i in range(4, len(nt))]
    raw = {"sentiment": [(d, 40 * math.sin(i / 23)) for i, d in enumerate(dates)],
           "pe_pct_raw": [(d, 12 + 3 * math.cos(i / 80)) for i, d in enumerate(dates)],
           "margin": [(d, 1e12 + i * 1e9) for i, d in enumerate(dates) if i >= 200],
           "fflow": [(d, 30 * math.sin(i / 7)) for i, d in enumerate(dates)],
           "breadth": [(d, 10 * math.sin(i / 40)) for i, d in enumerate(dates) if i >= 450],
           "nt_daily": nt, "nt_flow": ma5}
    saved = (BH.fetch_factors, BH._index_close)
    try:
        BH.fetch_factors = lambda path=None: dict(raw)
        BH._index_close = lambda path=None: (idx, idx[100:])
        p = BH.payload()
    finally:
        BH.fetch_factors, BH._index_close = saved
    p["ok"] = True
    assert p["factor_joins"] and len(p["nt_overlay"]["norm"]) > 500 and p["backtest"]["metrics"]["n_days"] > 500
    return p


def test_backtest_page_behavior(tmp_path):
    """页面脚本实跑(沙箱ECharts: 12个场景) + 真实ECharts渲染引擎的完整返回。
    对修复前的页面8/11失败: 并发旧结果覆盖/阈值与权重不校验就发请求/同名策略静默覆盖/
    策略列表看不到构成/国家队叠加线起点115/无因子加入竖线/空数据显示undefined"""
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node不可用")
    lib = REPO / "server" / "static" / "js" / "echarts.min.js"
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(_synthetic_payload(), ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([node, str(HARNESS), str(HTML), str(lib), str(payload)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, "页面行为测试失败:\n" + out[-3000:]
    n_cases = len(re.findall(r"^test\(", HARNESS.read_text(encoding="utf-8"), flags=re.M)) + 1   # +真实ECharts场景
    assert f"passed {n_cases}" in out, out[-800:]
