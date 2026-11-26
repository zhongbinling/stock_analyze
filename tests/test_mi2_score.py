# -*- coding: utf-8 -*-
"""MI-2 情绪图多指数 · 指数情绪分(docs/开发计划-情绪图多指数.md 第 5 节 MI-2)

① 输入组装 build_frame: 板块结构 → 结构分量、成交额单位、缺成交额 / 板块缺日的处理;
② 出分条件: 起步期不出分、五个组不齐不出分、没出分的日子记原因;
③ 与现役管线等价: 同样的输入喂给现役 score.compute 与本模块, 出了分的日子逐值一致;
④ 写库 save_index: 整体替换 + 保留旧表的三道守卫;
⑤ run / main: 单指数出错互不影响、权重覆盖生效、退出码;
⑥ 数据管理: 评分卡三行与健康三态、修复链挂上指数情绪评分(增量 / 重算两条链见 test_server_update.py)。
全部跑在临时库上(DB.DB_PATH 指向 tmp_path)。"""
import json
import shutil
import sqlite3
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

import boards as BD              # noqa: E402
import db as DB                  # noqa: E402
import score as SC               # noqa: E402
import score_index as SI         # noqa: E402

# 直接读 config.yaml(不经 load_cfg): 不受环境里的权重覆盖文件影响
CFG = yaml.safe_load((CORE / "config.yaml").read_text(encoding="utf-8"))
W = CFG["window"]                                   # 250
NAN = np.nan
REAL = [("0.399001", "SZ"), ("0.399006", "CYB"), ("1.000688", "KCB")]
SP = {"code": "9.000001", "name": "测试指数", "ts_code": "990001.TST", "board": "TST", "start": "20210101"}
SP2 = {"code": "9.000002", "name": "测试指数二", "ts_code": "990002.TST", "board": "TST", "start": "20210101"}
IDX_CODES = ["score:idx_399001", "score:idx_399006", "score:idx_000688"]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "sentiment.db"
    monkeypatch.setattr(DB, "DB_PATH", path)
    return path


def _bars(n=420, seed=1):
    """n 个交易日的指数日线: 随机游走, 成交额 200 ~ 600 亿(元)"""
    rng = np.random.default_rng(seed)
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    open_ = close * (1 + rng.normal(0, 0.005, n))
    return pd.DataFrame({
        "date": pd.bdate_range("2022-01-03", periods=n), "open": open_,
        "high": np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n))),
        "low": np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n))),
        "close": close, "amount": (200 + 400 * rng.random(n)) * 1e8})


def _board(dates, seed=2, skip=()):
    """板块日表行(与日线同一天序列); skip 里的日期不生成 → 板块缺日"""
    rng = np.random.default_rng(seed)
    n = len(dates)
    strong = rng.poisson(40, n)
    df = pd.DataFrame({
        "date": list(dates), "strong": strong, "weak": rng.poisson(20, n), "fade": rng.poisson(15, n),
        "streak_max": 1 + rng.poisson(1, n), "streak2": rng.poisson(4, n),
        "prev_strong": np.concatenate([[NAN], strong[:-1]])})
    keep = ~pd.DatetimeIndex(df["date"]).isin(pd.to_datetime(list(skip)))
    return df[keep].reset_index(drop=True)


def _cfg(extra=(SP,), **over):
    return {**CFG, "index": {**CFG["index"], "extra": list(extra)}, **over}


def _store(con, code, bars, board="TST", bd=None):
    """把日线与板块行写进临时库(bd 给空表 = 有这张表、还没有数据)"""
    con.executemany(
        "INSERT INTO index_daily(code,date,open,high,low,close,volume,amount) VALUES(?,?,?,?,?,?,?,?)",
        [(code, r.date.strftime("%Y-%m-%d"), r.open, r.high, r.low, r.close, None, r.amount)
         for r in bars.itertuples()])
    if bd is not None:
        con.execute(BD.SCHEMA)
        con.executemany(
            "INSERT INTO board_daily(board,date,strong,weak,fade,streak_max,streak2,prev_strong) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(board, r.date.strftime("%Y-%m-%d"), r.strong, r.weak, r.fade, r.streak_max, r.streak2,
              None if pd.isna(r.prev_strong) else r.prev_strong) for r in bd.itertuples()])
    con.commit()


def _stored(con, code):
    return pd.read_sql("SELECT * FROM index_sentiment WHERE index_code=? ORDER BY date", con, params=(code,))


def _eq(actual, expected):
    """逐值比较(缺失对缺失也算相等)"""
    np.testing.assert_allclose(np.asarray(actual, dtype=float), expected, rtol=0, atol=1e-9)


# ════════════ ① 输入组装 ════════════

def test_build_frame_maps_board_structure_and_units():
    d = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    bars = pd.DataFrame({"date": d, "open": [10.0, 10.0, 10.0, NAN], "high": [11.0, 11.0, 11.0, NAN],
                         "low": [9.0, 9.0, 9.0, NAN], "close": [10.0, 10.5, NAN, 11.0],
                         "amount": [3.5e10, 4e10, 5e10, None]})
    board = pd.DataFrame({
        "date": [d[0], d[2], d[3]],                        # 01-03 没有板块行; 01-04 板块有、日线没有
        "strong": [40, 7, 30], "weak": [10, 2, 5], "fade": [10, 3, 10],
        "streak_max": [5, 1, 3], "streak2": [8, 1, 6], "prev_strong": [32, 9, 25]})
    df = SI.build_frame(bars, board)
    # 没有收盘的日子丢掉(01-04); 01-05 是只有收盘的基日行, 留下
    days = df["date"].dt.strftime("%m-%d")
    assert days.tolist() == ["01-02", "01-03", "01-05"]
    # 结构分量: 强势 / 弱势家数、冲高回落率 = 冲高回落 / (强势 + 冲高回落)、连续强势高度、
    # 接力率 = 连续强势家数 / 前一日强势家数; 板块缺日 → 空
    _eq(df["zt_count"], [40, NAN, 30])
    _eq(df["dt_count"], [10, NAN, 5])
    _eq(df["zb_rate"], [20, NAN, 25])
    _eq(df["lb_max"], [5, NAN, 3])
    _eq(df["jl_rate"], [25, NAN, 24])
    # 量能: 成交额(元) → 亿; 缺成交额的日子为空(不当 0 计入均量)
    _eq(df["vol_total"], [350, 400, NAN])
    assert np.isnan(df["vr"].iloc[2])
    # 量价分量按该指数自己的日线算: 01-03 涨 5%
    _eq(df["chg"], [NAN, 5.0, 110.0 / 105.0 * 100 - 100])


def test_strong_and_fade_both_zero_leave_rates_blank():
    d = pd.to_datetime(["2024-01-02", "2024-01-03"])
    bars = pd.DataFrame({"date": d, "open": [10.0, 10.0], "high": [10.5, 10.5], "low": [9.5, 9.5],
                         "close": [10.0, 10.2], "amount": [1e10, 1e10]})
    board = pd.DataFrame({"date": d, "strong": [0, 0], "weak": [0, 0], "fade": [0, 0],
                          "streak_max": [0, 0], "streak2": [0, 0], "prev_strong": [0, 0]})
    df = SI.build_frame(bars, board)
    _eq(df["zt_count"], [0, 0])
    _eq(df["lb_max"], [0, 0])
    assert df["zb_rate"].isna().all() and df["jl_rate"].isna().all()   # 分母为 0 → 留空, 不是 0 / inf
    assert not np.isinf(df.drop(columns="date").to_numpy(dtype=float)).any()


# ════════════ ② 出分条件 ════════════

def test_no_scores_before_one_full_window():
    """量价分量要满一个分位窗口(250 个交易日)才有值: 这段不出分, 满了之后天天出分"""
    bars = _bars()
    res = SI.score_frame(SI.build_frame(bars, _board(bars["date"])), CFG)
    s, m = res["score"], res["missing"]
    assert len(s) == len(bars) == 420
    assert np.isnan(s.iloc[:W]).all() and s.notna().iloc[W:].all()
    # 起步期每一天都至少有一组缺(量能 / 动量 / 位置 / 波动满一个分位窗口才有值; 结构组只要攒满
    # pool_min_periods 个样本, 板块日表从头就有 → 第 60 天起不缺)
    warm = m.drop(columns="struct").any(axis=1)
    assert warm.loc[:W - 1].all()
    assert m["struct"].sum() == CFG["pool_min_periods"] - 1 and not m["struct"].iloc[CFG["pool_min_periods"] - 1:].any()
    assert not m.iloc[W:].any().any()


def test_missing_amount_blanks_only_that_day():
    """某天没有成交额: 只有那天不出分(量能组缺)。其后 20 个交易日 20 日均量被缺口带偏, 量比趋势不出,
    成交额分位照常 — 现役的滚动分位会连着空出一个窗口(见下面两个用例), 这里不会"""
    bars = _bars(700)
    k = 300                                                  # 这一天没有成交额
    bars.loc[k, "amount"] = NAN
    df = SI.build_frame(bars, _board(bars["date"]))
    res = SI.score_frame(df, CFG)
    s, m, comp = res["score"], res["missing"], res["comp"]
    assert s.notna().iloc[W:k].all() and np.isnan(s.iloc[k]) and s.notna().iloc[k + 1:].all()
    assert not m["vol"].iloc[W:k].any() and m["vol"].iloc[k] and not m["vol"].iloc[k + 1:].any()
    assert not m.drop(columns="vol").iloc[W:].any().any()                       # 只缺量能组
    assert np.isnan(df.loc[k, ["vol_total", "vr"]]).all()
    assert df.loc[k + 1:k + 19, "vol_total"].notna().all() and df.loc[k + 1:k + 19, "vr"].isna().all()
    assert df.loc[k + 20, "vr"] == df.loc[k + 20, "vr"]                         # 第 21 天起均量恢复
    assert comp.loc[k, ["vol_total", "vr"]].isna().all()
    assert comp.loc[k + 1:k + 19, "vol_total"].notna().all() and comp.loc[k + 1:k + 19, "vr"].isna().all()
    assert comp.loc[k + 20:, ["vol_total", "vr"]].notna().all().all()
    assert SI._reason(m.loc[k]) == "缺成交额数据"


def test_pct_tolerant_matches_legacy_except_inside_gaps():
    """量价分量的滚动分位: 数据齐全、只有前导缺失时与现役 pct_pos 逐值相同;
    中间缺值时现役其后一个窗口都取不到, 这里只影响缺的那几天(容忍 10%)"""
    x = pd.Series(np.arange(60, dtype=float))
    np.testing.assert_allclose(SI.pct_tolerant(x, 30), SC.pct_pos(x, 30, 30), equal_nan=True)
    y = x.copy()
    y.iloc[:7] = NAN                                          # 前导缺失(如 60 日乖离): 起步期同样后移
    np.testing.assert_allclose(SI.pct_tolerant(y, 30), SC.pct_pos(y, 30, 30), equal_nan=True)
    assert SI.pct_tolerant(y, 30).first_valid_index() == 36
    z = x.copy()
    z.iloc[40] = NAN                                          # 中间缺一天
    tz, lz = SI.pct_tolerant(z, 30), SC.pct_pos(z, 30, 30)
    assert np.isnan(tz.iloc[40]) and tz.notna().iloc[41:].all()
    assert np.isnan(lz.iloc[40:70]).all()                     # 现役: 40 ~ 69 都取不到
    np.testing.assert_allclose(tz.iloc[70:], lz.iloc[70:])
    w = x.copy()
    w.iloc[40:44] = NAN                                       # 缺 4 天, 超过 30 × 10% 的容忍(3 天)
    tw = SI.pct_tolerant(w, 30)
    assert np.isnan(tw.iloc[40:70]).all() and tw.notna().iloc[70:].all()   # 窗口里有效值回到 27 个才出
    assert SI.pct_tolerant(pd.Series([NAN, NAN]), 5).isna().all()


def test_long_gap_blanks_until_window_is_mostly_full_again():
    """缺口长(30 天没有成交额): 之后要等分位窗口里有效值重新回到 90% 才出分 —
    仍比现役(要等整 250 天过去)早 24 个交易日"""
    bars = _bars(700)
    k, gap = 300, 30
    bars.loc[k:k + gap - 1, "amount"] = NAN
    df = SI.build_frame(bars, _board(bars["date"]))
    res = SI.score_frame(df, CFG)
    s, m = res["score"], res["missing"]
    mp = int(round(W * (1 - SI.TOL)))                         # 225
    recover = k + gap - 1 + mp                                # 第 554 行: 窗口 [305, 554] 里缺 25 天
    assert mp == 225 and recover == 554
    assert s.notna().iloc[W:k].all() and np.isnan(s.iloc[k:recover]).all()
    assert s.notna().iloc[recover:].all() and m["vol"].iloc[k:recover].all()
    tp, lp = SI.pct_tolerant(df["vol_total"], W), SC.pct_pos(df["vol_total"], W, W)
    assert np.isnan(lp.iloc[recover:k + gap - 1 + W]).all()           # 现役要到第 578 行
    np.testing.assert_allclose(tp.iloc[k + gap - 1 + W:], lp.iloc[k + gap - 1 + W:])
    assert res["comp"].loc[recover:, "vol_total"].notna().all()


def test_missing_high_low_blanks_only_that_day():
    """某天没有最高 / 最低价: 振幅缺(波动组只有它) → 那天不出分; K 线实体是 3 天均值, 那天起 3 天没值,
    但动量组还有 RSI / 涨跌幅 / 跳空 → 不影响出分"""
    bars = _bars(700)
    k = 320
    bars.loc[k, ["high", "low"]] = NAN
    df = SI.build_frame(bars, _board(bars["date"]))
    res = SI.score_frame(df, CFG)
    s, m, comp = res["score"], res["missing"], res["comp"]
    assert np.isnan(df.loc[k, "amp"]) and np.isnan(df.loc[k, "kr"])
    assert comp.loc[k:k + 2, "kr"].isna().all() and comp.loc[k:k + 2, "rsi"].notna().all()
    assert np.isnan(s.iloc[k]) and s.drop(k).notna().iloc[W:].all()
    assert m.loc[k].to_dict() == {"struct": False, "vol": False, "mom": False, "pos": False, "amp": True}
    assert not m.iloc[W:].drop(k).any().any()
    assert SI._reason(m.loc[k]) == "缺波动数据"


def test_missing_board_day_blanks_only_that_day():
    """板块日表缺一天: 只有那天不出分(结构组缺), 前后照常"""
    bars = _bars()
    res = SI.score_frame(SI.build_frame(bars, _board(bars["date"], skip=[bars["date"][330]])), CFG)
    s = res["score"]
    assert s.notna().iloc[W:330].all() and np.isnan(s.iloc[330]) and s.notna().iloc[331:].all()
    assert res["missing"].loc[330].to_dict() == {"struct": True, "vol": False, "mom": False,
                                                  "pos": False, "amp": False}
    assert not res["missing"].loc[331].any()
    assert SI._reason(res["missing"].loc[330]) == "缺板块结构数据"
    assert SI._reason(res["missing"].loc[331]) is None


def test_late_board_data_delays_first_score():
    """板块日表比行情晚开始: 结构组攒满 pool_min_periods 个样本之前不出分"""
    bars = _bars()
    res = SI.score_frame(SI.build_frame(bars, _board(bars["date"]).iloc[300:]), CFG)
    first = 300 + CFG["pool_min_periods"] - 1
    assert np.isnan(res["score"].iloc[:first]).all() and res["score"].notna().iloc[first:].all()
    assert res["missing"]["struct"].iloc[:first].all() and not res["missing"]["struct"].iloc[first:].any()


def test_only_weighted_groups_gate_the_score():
    """齐全与否只看权重里出现的组: 权重只有动量组时, 没有板块数据也照常出分"""
    bars = _bars()
    cfg = _cfg(weights={"rsi": 0.6, "chg": 0.4})
    res = SI.score_frame(SI.build_frame(bars, _board(bars["date"]).iloc[0:0]), cfg)
    assert list(res["missing"].columns) == ["mom"]
    assert np.isnan(res["score"].iloc[:W]).all() and res["score"].notna().iloc[W:].all()


def test_every_weighted_component_belongs_to_a_group():
    """config 里的每个权重键都要归到某个组 — 新加分量却没归组的话, 缺了也不会拦住出分"""
    grouped = {k for keys in SI.GROUPS.values() for k in keys}
    assert set(CFG["weights"]) <= grouped
    assert set(SI.GROUP_LABEL) == set(SI.GROUPS) and set(SI.INDEX_LABELS) <= grouped


def test_specs_come_from_config():
    assert [(s["code"], s["board"]) for s in SI.index_specs(CFG)] == REAL
    assert SI.spec_of("1.000688", CFG)["name"] == "科创50"
    with pytest.raises(KeyError):
        SI.spec_of("9.999999", CFG)


# ════════════ ③ 与现役管线等价 ════════════

def test_same_inputs_match_market_score_pipeline(db):
    """回归检查: 同样的输入喂给现役大盘情绪(score.compute)与本模块 —
    出了分的日子分数逐值一致; 本模块留空的正是有组缺失的日子(现役按剩余权重归一化照常出分)"""
    con = DB.connect()
    bars = _bars(seed=7)
    P, S = "1.000001", "0.399106"
    iso = lambda r: r.date.strftime("%Y-%m-%d")              # noqa: E731
    # 现役读"主力指数成交量 + 次指数成交量(手)": 让主力指数的成交量 = 成交额 / 1e8、次指数为 0,
    # 与本模块的 vol_total 同值
    con.executemany(
        "INSERT INTO index_daily(code,date,open,high,low,close,volume,amount) VALUES(?,?,?,?,?,?,?,?)",
        [(P, iso(r), r.open, r.high, r.low, r.close, r.amount / 1e8, r.amount) for r in bars.itertuples()]
        + [(S, iso(r), None, None, None, None, 0.0, 0.0) for r in bars.itertuples()])
    # 全市场涨停结构(mood_raw)第 80 天才开始、第 350 天缺 — 板块日表用同一份数字
    mood = _board(bars["date"], seed=3).iloc[80:]
    mood = mood[mood["date"] != bars["date"][350]]
    con.executemany(
        "INSERT INTO mood_raw(date,zt_count,dt_count,zb_count,zb_rate,lb_max,lb2_count,jl_rate,yzt_count) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [(iso(r), r.strong, r.weak, r.fade, r.fade / (r.strong + r.fade) * 100,
          r.streak_max, r.streak2, r.streak2 / r.prev_strong * 100, r.prev_strong) for r in mood.itertuples()])
    con.commit()
    cfg = {**CFG, "index": {**CFG["index"], "primary": P, "secondary": S}}

    _, comp_m, mkt = SC.compute(cfg, con)
    res = SI.score_frame(SI.build_frame(bars, mood), cfg)
    mine, theirs = res["score"], mkt["score"]
    assert len(mine) == len(theirs) == len(bars)
    for k in CFG["weights"]:                                  # 分量分逐列逐值一致(含缺失位置)
        assert (res["comp"][k].isna() == comp_m[k].isna()).all(), k
        both = res["comp"][k].notna()
        np.testing.assert_allclose(res["comp"][k][both], comp_m[k][both], rtol=0, atol=1e-9)
    ok = mine.notna()
    assert np.isnan(mine.iloc[:W]).all() and ok.iloc[W:350].all()
    assert np.isnan(mine.iloc[350]) and ok.iloc[351:].all()
    assert theirs.notna().iloc[W:].all()                      # 350 缺结构: 现役照常出分, 本模块留空
    np.testing.assert_allclose(mine[ok], theirs[ok], rtol=0, atol=0.05)       # 出了分的日子: 与现役同一个数(差在取整)
    # 分档也一致, 除了分数取整后正好落在阈值上的日子: 现役按取整前的分数判, 本模块按入库的数判
    lab = lambda s: SC.stage_label(s, cfg["thresholds"])        # noqa: E731
    edge = mkt["score"].round(1).apply(lab) != mkt["stage"]
    assert edge.iloc[W:].sum() <= 5 and edge.sum() < 60
    assert res["stage"][ok & ~edge].tolist() == mkt["stage"][ok & ~edge].tolist()


def test_stage_follows_the_rounded_score(monkeypatch):
    """分数取一位小数、分档按取整后的数判: 正好压在阈值上的日子, 库里的分数和分档对得上"""
    bars = _bars()
    df = SI.build_frame(bars, _board(bars["date"]))
    vals = [10.04, -10.04, -29.96, 9.96, -60.06]
    fake = pd.Series(NAN, index=df.index)
    fake.iloc[-len(vals):] = vals
    monkeypatch.setattr(SC, "weighted_score", lambda comp, weights, smooth: fake)
    res = SI.score_frame(df, CFG)
    assert np.isnan(res["score"].iloc[0])
    _eq(res["score"].iloc[-len(vals):], [10.0, -10.0, -30.0, 10.0, -60.1])
    assert res["stage"].iloc[-len(vals):].tolist() == ["中性", "中性偏弱", "偏冷/退潮", "中性", "冰点/恐慌"]


# ════════════ ④ 写库 ════════════

def _result(dates, n_unscored_tail=0, score=10.0):
    """save_index 要的 result: 全部出分, 或末尾 n 天没出分"""
    dates = list(dates)
    n = len(dates)
    scored = [i < n - n_unscored_tail for i in range(n)]
    rows = pd.DataFrame({
        "date": dates, "score": [score if ok else NAN for ok in scored],
        "stage": ["中性" if ok else None for ok in scored],
        "components": ["{}" if ok else None for ok in scored],
        "note": [None if ok else "缺成交额数据" for ok in scored]})
    return {"rows": rows}


def test_save_index_replaces_whole_series_and_keeps_old_table_on_bad_result(db):
    con = DB.connect()
    days = pd.bdate_range("2024-01-01", periods=400).strftime("%Y-%m-%d")
    assert SI.save_index(con, "0.399001", _result(days[:300]), CFG["weights"]) == 300
    SI.save_index(con, "0.399006", _result(days[:100]), CFG["weights"])
    # 再存一次: 整体替换 — 旧分不残留, 没出分的尾段 score 为空、note 记原因
    assert SI.save_index(con, "0.399001", _result(days[:302], n_unscored_tail=2, score=-12.3),
                         CFG["weights"]) == 300
    got = _stored(con, "0.399001")
    assert len(got) == 302 and got["score"].notna().sum() == 300
    assert (got["score"] == 10.0).sum() == 0 and (got["score"] == -12.3).sum() == 300
    assert pd.isna(got["stage"].iloc[301]) and pd.isna(got["components"].iloc[301])
    assert got["note"].iloc[301] == "缺成交额数据" and pd.isna(got["note"].iloc[0])
    assert json.loads(got["weights"].iloc[0]) == CFG["weights"]
    assert len(_stored(con, "0.399006")) == 100                                   # 别的指数不受影响
    # 守卫: 出分天数不到旧表一半 / 最新出分日倒退 / 一天都没出分 → 抛错, 旧表原样保留
    for bad in (_result(days[:100]), _result(days[5:290]), _result(days[:320], n_unscored_tail=320)):
        with pytest.raises(RuntimeError, match="保留旧表"):
            SI.save_index(con, "0.399001", bad, CFG["weights"])
    assert _stored(con, "0.399001").equals(got)
    # 尾段没出分但出分天数不减、最新出分日不退 → 放行
    assert SI.save_index(con, "0.399001", _result(days[:303], n_unscored_tail=3), CFG["weights"]) == 300
    assert len(_stored(con, "0.399001")) == 303


# ════════════ ⑤ run / main ════════════

def test_run_writes_scores_and_reason_for_unscored_days(db, capsys):
    con = DB.connect()
    bars = _bars(seed=5)
    d = bars["date"]
    _store(con, SP["code"], bars, bd=_board(d, skip=[d[330], d[419]]))     # 中间一天、最后一天板块缺
    out = SI.run(cfg=_cfg(), conn=con)
    res = out[SP["code"]]
    assert res["n"] == res["saved"] == 168 and len(res["rows"]) == 170   # 250 ~ 419 共 170 天, 两天没出分
    iso = lambda x: x.strftime("%Y-%m-%d")                   # noqa: E731
    assert (res["first_score"], res["last_score"], res["last_note"], res["last_bar"]) == (
        iso(d[W]), iso(d[418]), "缺板块结构数据", iso(d[419]))
    got = _stored(con, SP["code"])
    assert (len(got), int(got["score"].notna().sum())) == (170, 168)
    assert got["date"].iloc[0] == iso(d[W]) and got["date"].iloc[-1] == iso(d[419])
    assert got["note"].notna().sum() == 2 and set(got["note"].dropna()) == {"缺板块结构数据"}
    assert np.isnan(got["score"].iloc[80]) and got["note"].iloc[80] == "缺板块结构数据"
    comp = json.loads(got["components"].dropna().iloc[0])
    assert set(comp) == set(CFG["weights"])                    # 出了分的日子里单个分量也可能缺(记成 null)
    assert all(v is None or -100 <= v <= 100 for v in comp.values())
    assert json.loads(got["weights"].iloc[0]) == CFG["weights"]
    labels = {SC.stage_label(v, CFG["thresholds"]) for v in (-100, -70, -45, -20, 0, 20, 45, 70, 100)}
    assert set(got["stage"].dropna()) <= labels
    scored = got["score"].notna()                                 # 库里的分数和分档自洽
    assert got["score"][scored].apply(lambda s: SC.stage_label(s, CFG["thresholds"])).tolist() == \
        got["stage"][scored].tolist()
    text = capsys.readouterr().out
    assert "168 日" in text and "还没出分(缺板块结构数据)" in text
    SI.run(cfg=_cfg(), conn=con)                                           # 幂等: 再跑一遍结果不变
    assert _stored(con, SP["code"]).equals(got)


def test_run_isolates_single_index_failure(db, monkeypatch, capsys):
    con = DB.connect()
    bars = _bars(seed=5)
    _store(con, SP["code"], bars, bd=_board(bars["date"]))
    real = SI.compute_index

    def boom(code, cfg=None, conn=None):
        if code == SP2["code"]:
            raise RuntimeError("库打不开")
        return real(code, cfg, conn)
    monkeypatch.setattr(SI, "compute_index", boom)
    out = SI.run(cfg=_cfg((SP, SP2)), conn=con)
    assert out[SP["code"]]["n"] == 170 and "error" in out[SP2["code"]]
    assert "RuntimeError: 库打不开" in out[SP2["code"]]["error"]
    assert "测试指数二 失败: RuntimeError: 库打不开" in capsys.readouterr().out
    assert len(_stored(con, SP["code"])) == 170                   # 另一个指数照常入库


def test_run_reports_missing_inputs_without_raising(db, capsys):
    """没有日线 / 一直算不出分(板块日表还没有数据): 只报告、不抛、不写一行, 原因写明白"""
    con = DB.connect()
    bars = _bars(seed=5)
    _store(con, SP["code"], bars.iloc[0:0])                              # 没有日线
    _store(con, SP2["code"], bars, bd=_board(bars["date"]).iloc[0:0])   # 有日线、板块日表还没有数据
    out = SI.run(cfg=_cfg((SP, SP2)), conn=con)
    assert out[SP["code"]]["msg"] == "没有该指数的日线"
    err = out[SP2["code"]]["error"]
    assert err.startswith("RuntimeError: [index-score] 9.000002 没有任何出了分的日子(最后一个交易日 ")
    assert err.endswith(f"{bars['date'].iloc[-1]:%Y-%m-%d} 缺板块结构数据), 保留旧表(0 行)")
    text = capsys.readouterr().out
    assert "测试指数: 没有该指数的日线" in text and "测试指数二 失败: " + err in text
    assert con.execute("SELECT COUNT(*) FROM index_sentiment").fetchone()[0] == 0   # 什么都没写


def test_main_exit_codes(db, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["score_index.py"])
    monkeypatch.setattr(SI, "run", lambda codes=None, cfg=None, conn=None: {"a": {"error": "x"}})
    with pytest.raises(SystemExit) as e:
        SI.main()
    assert e.value.code == 1                                       # 全部失败 → 1
    monkeypatch.setattr(SI, "run", lambda codes=None, cfg=None, conn=None: {"a": {"n": 1}, "b": {"error": "x"}})
    with pytest.raises(SystemExit) as e:
        SI.main()
    assert e.value.code == 3                                       # 部分失败 → 3: 日志行标"部分完成", 任务不算失败
    monkeypatch.setattr(SI, "run", lambda codes=None, cfg=None, conn=None: {"a": {"n": 1}})
    with pytest.raises(SystemExit) as e:
        SI.main()
    assert e.value.code == 0                                       # 全部成功 → 0
    seen = []
    monkeypatch.setattr(sys, "argv", ["score_index.py", "--index", "9.000001"])
    monkeypatch.setattr(SI, "run", lambda codes=None, cfg=None, conn=None: seen.append(codes) or {"9.000001": {}})
    with pytest.raises(SystemExit) as e:
        SI.main()
    assert e.value.code == 0 and seen == [["9.000001"]]            # --index 可重复, 原样传给 run
    monkeypatch.setattr(sys, "argv", ["score_index.py", "--index", "9.999999"])
    monkeypatch.setattr(SI, "run", lambda codes=None, cfg=None, conn=None: {})
    with pytest.raises(SystemExit) as e:
        SI.main()
    assert e.value.code == 2                                       # 指定了不存在的代码
    monkeypatch.setattr(sys, "argv", ["score_index.py"])
    with pytest.raises(SystemExit) as e:
        SI.main()
    assert e.value.code == 0                                       # 配置里一个指数都没有: 没事可做


def test_mid_history_hole_is_recorded_and_visible(db, monkeypatch, capsys):
    """历史中段缺一天成交额(真实配置里的深证成指 / 深市板块): 只有那天不出分、原因入库;
    更新链日志与数据管理的评分卡上都看得到这个空洞 — 否则最新出分日照常往前走, 页面上发现不了"""
    con = DB.connect()
    bars = _bars(700, seed=11)
    d = bars["date"]
    bars.loc[300, "amount"] = NAN
    _store(con, "0.399001", bars, board="SZ", bd=_board(d))
    iso = lambda x: x.strftime("%Y-%m-%d")                   # noqa: E731
    out = SI.run(cfg=CFG, conn=con)
    res = out["0.399001"]
    assert res["n"] == 700 - W - 1 and res["last_note"] is None
    got = _stored(con, "0.399001")
    blank = got[got["score"].isna()]
    assert blank["date"].tolist() == [iso(d[300])] and blank["note"].tolist() == ["缺成交额数据"]
    text = capsys.readouterr().out
    assert "其间另有 1 日未出分" in text
    from server import market
    monkeypatch.setattr(market, "SENT_DB", db)
    monkeypatch.setattr(market, "_cfg_cache", None)
    row = next(x for x in market.score_datasets() if x["code"] == "score:idx_399001")
    assert (row["health"], row["rows"], row["last"]) == ("warn", 700 - W - 1, iso(d[699]))
    assert row["note"] == f"与该指数行情同步·此前另有 1 日未出分·评分自 {iso(d[W])}"


def test_guard_keeps_old_table_when_a_long_gap_appears(db):
    """库里已有完整的评分, 之后行情里出现一段长缺口(新结果出分天数不到旧表一半)→ 保留旧表, 不拿残缺的覆盖"""
    con = DB.connect()
    bars = _bars(700, seed=12)
    _store(con, "0.399001", bars, board="SZ", bd=_board(bars["date"]))
    SI.run(cfg=CFG, conn=con)
    before = _stored(con, "0.399001")
    assert int(before["score"].notna().sum()) == 700 - W
    lo, hi = (bars["date"].iloc[i].strftime("%Y-%m-%d") for i in (300, 330))
    con.execute("UPDATE index_daily SET amount=NULL WHERE code='0.399001' AND date>=? AND date<?", (lo, hi))
    con.commit()
    out = SI.run(cfg=CFG, conn=con)
    assert "保留旧表" in out["0.399001"]["error"] and "saved" not in out["0.399001"]
    assert _stored(con, "0.399001").equals(before)


def test_weight_override_applies(db, monkeypatch, tmp_path):
    """管理台的自定义权重 / 窗口(SENTIMENT_OVERRIDE 指向的文件, 叠加在默认配置上)对指数情绪同样生效:
    按覆盖后的配置出分, 表里记的也是这份权重"""
    con = DB.connect()
    bars = _bars(seed=5)
    bd = _board(bars["date"])
    _store(con, SP["code"], bars, bd=bd)
    over = tmp_path / "over.yaml"
    over.write_text(yaml.safe_dump({"index": {"extra": [SP]}, "weights": {"rsi": 0.6, "chg": 0.4},
                                    "window": 120, "pool_min_periods": 20}, allow_unicode=True),
                    encoding="utf-8")
    monkeypatch.setenv("SENTIMENT_OVERRIDE", str(over))
    res = SI.run()[SP["code"]]
    cfg = SC.load_cfg()
    assert (cfg["weights"]["rsi"], cfg["weights"]["chg"], cfg["window"]) == (0.6, 0.4, 120)
    assert cfg["weights"]["amp"] == CFG["weights"]["amp"] and cfg["smooth"] == CFG["smooth"]   # 没覆盖的沿用
    assert res["n"] == 300 and res["first_score"] == bars["date"].iloc[120].strftime("%Y-%m-%d")   # 窗口 120
    got = _stored(con, SP["code"])
    assert json.loads(got["weights"].iloc[0]) == cfg["weights"]
    expect = SI.score_frame(SI.build_frame(bars, bd), cfg)["score"].dropna().to_numpy()
    np.testing.assert_allclose(got["score"].to_numpy(), expect, rtol=0, atol=0.05)       # 入库取一位小数
    default = SI.score_frame(SI.build_frame(bars, bd), CFG)["score"].dropna().to_numpy()  # 与默认权重下不是一个数
    assert len(default) == 170 and (np.abs(expect[-170:] - default) > 0.5).sum() > 100


# ════════════ ⑥ 数据管理 ════════════

@pytest.fixture()
def mkt(fixture_db, tmp_path, monkeypatch):
    """每测一份夹具库副本; 返回 (market 模块, 库路径)"""
    from server import market
    p = tmp_path / "mkt.db"
    shutil.copy2(fixture_db, p)
    monkeypatch.setattr(market, "SENT_DB", p)
    monkeypatch.setattr(market, "_cfg_cache", None)
    return market, p


def test_score_card_index_rows_and_health(mkt):
    """评分卡多出三行; 健康基准是各指数自己的最新行情日, 最后一天没出分时说明原因"""
    market, p = mkt
    by = {d["code"]: d for d in market.score_datasets()}
    a, b, c = (by[x] for x in IDX_CODES)
    assert [x["name"] for x in (a, b, c)] == ["指数情绪分·深证成指", "指数情绪分·创业板指", "指数情绪分·科创50"]
    assert all(x["health"] == "bad" and "无该指数日线" in x["note"] for x in (a, b, c))   # 夹具没有三个新指数
    # 没有评分表也不拖垮整张卡: 其余各行照常
    assert by["score:sentiment"]["health"] == "ok" and by["score:bk"]["health"] == "ok"

    con = sqlite3.connect(str(p))
    for code, upto in (("0.399001", "2026-09-29"), ("0.399006", "2026-09-30"), ("1.000688", "2026-09-30")):
        for d in ("2026-09-25", "2026-09-29", "2026-09-30"):
            if d <= upto:
                con.execute("INSERT INTO index_daily VALUES(?,?,3000,3010,2990,3005,3e11)", (code, d))
    con.execute(SI.SCHEMA)
    con.executemany(
        "INSERT INTO index_sentiment(index_code,date,score,stage,note) VALUES(?,?,?,?,?)",
        [("0.399001", "2026-09-25", -39.0, "偏冷/退潮", None), ("0.399001", "2026-09-29", -39.5, "偏冷/退潮", None),
         ("0.399006", "2026-09-29", -41.0, "偏冷/退潮", None),
         ("0.399006", "2026-09-30", None, None, "缺成交额数据")])
    con.commit()
    con.close()
    by = {d["code"]: d for d in market.score_datasets()}
    a, b, c = (by[x] for x in IDX_CODES)
    assert (a["health"], a["rows"], a["days"], a["first"], a["last"], a["codes"]) == (
        "ok", 2, 2, "2026-09-25", "2026-09-29", None)
    assert a["note"] == "与该指数行情同步·评分自 2026-09-25"
    assert (b["health"], b["rows"], b["last"]) == ("warn", 1, "2026-09-29")        # 不数没出分的行
    assert b["note"] == "落后该指数行情 1 天·2026-09-30 未出分(缺成交额数据)·评分自 2026-09-29"
    assert (c["health"], c["rows"]) == ("bad", 0) and "未生成" in c["note"]        # 有日线、还没有评分
    # 更新链日志的计数: 只数出了分的行(最新 = 09-29, 不是没出分的 09-30)
    assert market.pipeline_stats()["index_sentiment"] == {"rows": 3, "last": "2026-09-29"}


def test_pipeline_stats_without_index_table(mkt):
    market, _p = mkt
    assert market.pipeline_stats()["index_sentiment"] == {"rows": 0, "last": None}


def test_repair_chain_runs_index_score_without_blocking(server_app, monkeypatch):
    """定时更新走的修复链: 大盘评分之后、板块温度之前多一步指数情绪评分; 它失败不挡后面的板块温度"""
    from server import jobs, market
    monkeypatch.setattr(market, "scan_gaps", lambda win: {
        "window_days": win, "trading_days": 5, "latest": "2026-11-26", "assets": []})
    runs = []

    def fake_run(cmd, append, cwd=None, env_extra=None):
        c = [str(x) for x in cmd]
        runs.append(c[2:])
        return 1 if "score_index.py" in c else 0
    monkeypatch.setattr(jobs, "_run", fake_run)
    lines = []
    with server_app["app"].app_context():
        assert jobs.task_repair(7)(lines.append) == 0
    assert runs == [["fetch_daily.py"], ["score.py"], ["score_index.py"], ["sector.py", "calc"]]
    assert "指数情绪评分失败, 继续后续步骤" in " ".join(lines)


def test_repair_chain_marks_partial_index_score(server_app, monkeypatch):
    """部分指数失败(退出码 3): 日志写明"部分完成", 同样不挡后面的板块温度"""
    from server import jobs, market
    monkeypatch.setattr(market, "scan_gaps", lambda win: {
        "window_days": win, "trading_days": 5, "latest": "2026-11-26", "assets": []})
    runs = []

    def fake_run(cmd, append, cwd=None, env_extra=None):
        c = [str(x) for x in cmd]
        runs.append(c[2:])
        return 3 if "score_index.py" in c else 0
    monkeypatch.setattr(jobs, "_run", fake_run)
    lines = []
    with server_app["app"].app_context():
        assert jobs.task_repair(7)(lines.append) == 0
    assert runs[-1] == ["sector.py", "calc"] and "指数情绪评分部分完成" in " ".join(lines)


def test_datasets_api_includes_index_score_rows(mkt, server_app):
    market, p = mkt
    con = sqlite3.connect(str(p))
    con.execute("INSERT INTO index_daily VALUES('0.399001','2026-09-30',3000,3010,2990,3005,3e11)")
    con.execute(SI.SCHEMA)
    con.execute("INSERT INTO index_sentiment(index_code,date,score,stage) "
                "VALUES('0.399001','2026-09-30',-40.0,'偏冷/退潮')")
    con.commit()
    con.close()
    r = server_app["client"].get("/admin/api/datasets")
    assert r.status_code == 200
    row = next(s for s in r.get_json()["scores"] if s["code"] == "score:idx_399001")
    assert (row["health"], row["rows"], row["last"], row["source"]) == ("ok", 1, "2026-09-30", "score_index.py")
