# -*- coding: utf-8 -*-
"""指数情绪分(MI-2): 深证成指 / 创业板指 / 科创50 各一条情绪分序列 — 情绪图多指数的评分层。

模型、权重、滚动分位窗口、EMA 平滑、分档阈值全部沿用现役大盘情绪(读同一份 config; 加权 / 平滑 / 分档
直接调 score 里的函数, 分量清单与模式取自 metrics.COMPONENTS), 只把输入换成"这个指数自己的"
(计划 docs/开发计划-情绪图多指数.md 2.1):

  组          分量                          现役大盘情绪            指数情绪分
  结构 20%    强势 / 弱势 / 冲高回落 /      全市场涨停结构          对应板块的强弱势家数
              连续强势高度 / 接力            (同花顺 / tushare)      (板块日表 board_daily)
  量能 20%    成交额分位×方向、5/20 量比    沪深两市成交量(手)      该指数自身成交额
  动量 28%    RSI、涨跌幅、跳空、实体        上证日线                该指数日线
  位置 20%    20 日 / 60 日乖离、价格位置    上证日线                该指数日线
  波动 12%    振幅×方向                     上证日线                该指数日线

现役大盘情绪(sentiment 表)不受本模块影响。

出分条件: 五个组各自至少有一个分量可用才出分, 否则留空。
- 起步期: 量价分量要满一个分位窗口(250 个交易日)才有值, 这段不出分;
- 缺数据: 某天没有成交额, 或板块日表还没到(当晚个股日线未入库), 那天不出分, 下次更新链补上。
现役模型在缺分量时按剩余权重归一化继续出分 — 早期会出现"只有量价、没有结构"的分数,
几个指数要横向比较, 这种分数不可比。

与现役唯一的实现差别在量价分量的滚动分位(pct_tolerant): 现役的 score.pct_pos 要求窗口内每一天都有值,
中间缺一天, 其后一个窗口的分位就都取不到。这里窗口内有效值不少于 90% 就出分位 — 某天没有成交额或
开高低, 只影响当天(量比趋势再往后 20 个交易日)。数据齐全时与现役逐值相同(测试里逐列比对)。

表 index_sentiment(index_code, date, score, stage, components, weights, note): 每个指数每个交易日一行,
自首个出分日起存到最后一个交易日。出了分的行有 score / stage / components(分量分, JSON) / weights;
没出分的行 score 为空、note 写缺了哪组数据(图上留空时标原因用)。每次整体替换该指数的行(单事务),
新结果的出分天数不到旧的一半、或最新出分日倒退时保留旧表。

用法: python score_index.py [--index 0.399006]     # 全部(或指定)指数; 更新链在"大盘评分"之后调用
退出码: 0 全部成功; 3 部分指数失败; 1 全部失败; 2 指定的代码不存在
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import score as SC                     # noqa: E402
from db import connect                # noqa: E402
from metrics import COMPONENTS, derive  # noqa: E402

TABLE = "index_sentiment"
SCHEMA = f"""CREATE TABLE IF NOT EXISTS {TABLE}(
  index_code TEXT NOT NULL, date TEXT NOT NULL,
  score REAL, stage TEXT, components TEXT, weights TEXT,
  note TEXT,
  PRIMARY KEY(index_code, date))"""
_INSERT = (f"INSERT INTO {TABLE}(index_code,date,score,stage,components,weights,note) "
           "VALUES(?,?,?,?,?,?,?)")
TOL = 0.1          # 量价分量的滚动分位: 窗口内有效值少于 (1 - TOL) × 窗口长度 才不出

# 组 → 分量键(与 config.weights 的键对应; 只有出现在权重里的组才参与"齐全"判定)
GROUPS = {
    "struct": ("zt_count", "dt_count", "zb_rate", "lb_max", "jl_rate"),
    "vol": ("vol_total", "vr"),
    "mom": ("rsi", "chg", "gap", "kr"),
    "pos": ("bias20", "bias60", "close"),
    "amp": ("amp",),
}
GROUP_LABEL = {"struct": "板块结构", "vol": "成交额", "mom": "动量", "pos": "位置", "amp": "波动"}
# 悬停明细里的分量名: 键与现役相同, 但口径是指数 / 板块的, 名称要跟着换
INDEX_LABELS = {
    "zt_count": "强势家数(≥+9.8%)", "dt_count": "弱势家数(≤−9.8%)", "zb_rate": "冲高回落率",
    "lb_max": "连续强势高度", "jl_rate": "接力率(昨强今强占比)",
    "vol_total": "指数成交额", "vr": "量比趋势5/20",
}


def index_specs(cfg=None) -> list:
    """config index.extra: [{code, name, ts_code, board, start}]"""
    cfg = cfg or SC.load_cfg()
    return list((cfg.get("index") or {}).get("extra") or [])


def spec_of(code, cfg=None) -> dict:
    for sp in index_specs(cfg):
        if sp["code"] == code:
            return sp
    raise KeyError(f"{code} 不在 config index.extra 里")


def build_frame(bars: pd.DataFrame, board: pd.DataFrame) -> pd.DataFrame:
    """该指数日线 + 对应板块日表 → 评分宽表(各分量的原始值), 按日期升序。

    bars: date, open, high, low, close, amount(成交额, 元; 可缺); board: date, strong, weak, fade,
    streak_max, streak2, prev_strong。量能与结构分量沿用现役的键名(vol_total / vr、zt_count …),
    这样分量分、加权、分档可以直接用 score 里现役的那几个函数。
    """
    amt = pd.to_numeric(bars["amount"], errors="coerce")
    num = lambda c: pd.to_numeric(bars[c], errors="coerce").reset_index(drop=True)   # noqa: E731
    df = pd.DataFrame({
        "date": pd.to_datetime(bars["date"]).reset_index(drop=True),
        "open": num("open"), "high": num("high"), "low": num("low"), "close": num("close"),
        "volume": (amt / 1e8).reset_index(drop=True),            # 元 → 亿; 现役的 volume2 / amount 不用
        "amount": 0.0, "volume2": 0.0, "amount2": 0.0,
    })
    # 没有收盘的日子先丢掉(留到 derive 之后会让下一天的涨跌幅、振幅跟着变空);
    # 指数基日只有收盘值、没有开高低 → 留下, 那天的量价分量自然为空
    df = df[df["close"].notna()].reset_index(drop=True)
    no_turnover = df["volume"].isna()
    df["volume"] = df["volume"].fillna(0.0)
    df = derive(df)
    # derive 把缺成交额的日子当 0: 当天的成交额分量不出; 其后 20 个交易日 20 日均量被带偏, 量比趋势不出
    stale = no_turnover.rolling(20, min_periods=1).max().astype(bool)
    df.loc[no_turnover, ["vol_total", "vol_und"]] = np.nan
    df.loc[stale, ["vr", "vr_und"]] = np.nan
    # 板块结构(左连接: 板块日表还没有的日子为空)
    df = df.set_index("date")
    b = board.set_index(pd.to_datetime(board["date"]))
    strong = pd.to_numeric(b["strong"], errors="coerce").reindex(df.index)
    weak = pd.to_numeric(b["weak"], errors="coerce").reindex(df.index)
    fade = pd.to_numeric(b["fade"], errors="coerce").reindex(df.index)
    prev = pd.to_numeric(b["prev_strong"], errors="coerce").reindex(df.index)
    df["zt_count"] = strong
    df["dt_count"] = weak
    df["zb_rate"] = fade / (strong + fade).where((strong + fade) > 0) * 100   # 冲高回落率
    df["lb_max"] = pd.to_numeric(b["streak_max"], errors="coerce").reindex(df.index)
    df["jl_rate"] = pd.to_numeric(b["streak2"], errors="coerce").reindex(df.index) / prev.where(prev > 0) * 100
    return df.reset_index().replace([np.inf, -np.inf], np.nan)


def pct_tolerant(s: pd.Series, n: int, tol: float = TOL) -> pd.Series:
    """滚动分位 0~100, 与 score.pct_pos 同口径((X - N日最低) / (N日最高 - N日最低)), 区别只在缺失值:
    pct_pos 要求窗口里 n 个值一个不缺 — 中间缺一天, 其后 n 天的分位都取不到; 这里窗口内有效值不少于
    (1 - tol) × n 就出。起步期不变: 自该序列首个有效值起, 窗口走满 n 个位置才出。"""
    x = pd.to_numeric(s, errors="coerce").astype(float)
    first = x.first_valid_index()
    if first is None:
        return pd.Series(np.nan, index=x.index, dtype=float)
    start = x.index.get_loc(first)
    mp = max(1, int(round(n * (1 - tol))))
    lo = x.rolling(n, min_periods=mp).min()
    hi = x.rolling(n, min_periods=mp).max()
    out = (x - lo) / (hi - lo) * 100
    return out.where(np.arange(len(x)) >= start + n - 1)


def component_scores(df: pd.DataFrame, window: int, pool_min_periods: int = 60) -> pd.DataFrame:
    """每个分量 → -100~+100 的分位分(方向已含)。分量清单、方向、模式取自现役的 metrics.COMPONENTS;
    结构组用现役的 pct_pos(本就允许窗口内缺失), 量价分量的分位用 pct_tolerant。
    数据齐全时与 score.component_scores 逐值相同。"""
    chg = df["chg"]
    d_updown = chg.apply(lambda c: 1 if c >= 0 else -1)
    out = {}
    for key, _label, sign, mode, group in COMPONENTS:
        x = df[key]
        if mode == "rsi":
            s = (x - 50) * 2
        else:
            if group == "pool":
                s = SC.pct_pos(x, window, pool_min_periods) * 2 - 100
            else:
                s = pct_tolerant(x, window) * 2 - 100
            if mode == "symdir":
                # 高振幅出现在两极, 用当日方向区分狂热/恐慌
                s = s * d_updown
            elif mode == "asymdir":
                # 通达信V2.1量能口径: 放量×方向; 缩量恒负(退潮), 缩量涨减半
                s = pd.Series(np.where(s > 0, s * d_updown,
                                       np.where(chg >= 0, s * 0.5, s)),
                              index=s.index)
        out[key] = s * sign
    return pd.DataFrame(out, index=df.index)


def missing_groups(comp: pd.DataFrame, weights: dict) -> pd.DataFrame:
    """每行缺了哪些组(布尔表): 组内一个可用分量都没有才算缺"""
    cols = {}
    for g, keys in GROUPS.items():
        ks = [k for k in keys if k in weights and k in comp.columns]
        if ks:
            cols[g] = comp[ks].notna().sum(axis=1) == 0
    return pd.DataFrame(cols, index=comp.index)


def score_frame(df: pd.DataFrame, cfg: dict = None) -> dict:
    """评分宽表 → {date, score, stage, comp(分量分), missing(缺的组)}。
    score 取一位小数(与入库一致), 分档按取整后的分数判 — 库里的分数和分档自洽。"""
    cfg = cfg or SC.load_cfg()
    comp = component_scores(df, cfg["window"], cfg.get("pool_min_periods", 60))
    score = SC.weighted_score(comp, cfg["weights"], cfg["smooth"]).round(1)
    missing = missing_groups(comp, cfg["weights"])
    score = score.mask(missing.any(axis=1))           # 五个组不齐 → 不出分
    return {"date": df["date"], "score": score,
            "stage": score.apply(lambda s: SC.stage_label(s, cfg["thresholds"])),
            "comp": comp, "missing": missing}


def _reason(m: pd.Series) -> str | None:
    """没出分的原因(缺了哪几组); 五个组都齐返回 None"""
    lack = [GROUP_LABEL[g] for g in m.index if m[g]]
    return f"缺{'、'.join(lack)}数据" if lack else None


def _none(v):
    """入库前的缺失值统一成 None(pandas 3 的字符串列里缺失是 NaN, 不是 None)"""
    return None if v is None or v is pd.NA or (isinstance(v, float) and v != v) else v


def _pack(row: pd.Series, weights: dict) -> str:
    return json.dumps({k: (None if pd.isna(row[k]) else round(float(row[k]), 1)) for k in weights},
                      ensure_ascii=False)


_COLS = ["date", "score", "stage", "components", "note"]


def compute_index(code: str, cfg: dict = None, conn=None) -> dict:
    """算一个指数: 读库 → 评分。返回 {"spec", "n"(出分天数), "rows"(升序; 自首个出分日起每个交易日一行,
    没出分的行 score 为空、note 写原因), "first_bar", "last_bar", "first_score", "last_score",
    "last_note"(最后一个交易日没出分时的原因)}; 没有行情时另带 msg"""
    cfg = cfg or SC.load_cfg()
    sp = spec_of(code, cfg)
    own = conn is None
    con = connect() if own else conn
    try:
        bars = pd.read_sql(
            "SELECT date, open, high, low, close, amount FROM index_daily WHERE code=? ORDER BY date",
            con, params=(code,), parse_dates=["date"])
        board = pd.read_sql(
            "SELECT date, strong, weak, fade, streak_max, streak2, prev_strong FROM board_daily "
            "WHERE board=? ORDER BY date", con, params=(sp["board"],), parse_dates=["date"])
    finally:
        if own:
            con.close()

    def blank(**kw):
        return {"spec": sp, "n": 0, "rows": pd.DataFrame(columns=_COLS), "first_score": None,
                "last_score": None, "first_bar": None, "last_bar": None, "last_note": None, **kw}

    if bars.empty:
        return blank(msg="没有该指数的日线")
    res = score_frame(build_frame(bars, board), cfg)
    scored = res["score"].notna()
    base = {"spec": sp, "first_bar": bars["date"].iloc[0].strftime("%Y-%m-%d"),
            "last_bar": bars["date"].iloc[-1].strftime("%Y-%m-%d"),
            "last_note": _reason(res["missing"].iloc[-1])}
    if not scored.any():
        return {**blank(), **base}
    keep = scored.cummax()                                     # 自首个出分日起, 每个交易日都存
    idx = list(res["score"].index[keep])
    rows = pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in res["date"][keep]],
        "score": res["score"][keep].to_numpy(),
        "stage": [s or None for s in res["stage"][keep]],
        "components": [_pack(res["comp"].loc[i], cfg["weights"]) if scored[i] else None for i in idx],
        "note": [_reason(m) for _, m in res["missing"][keep].iterrows()],
    }).reset_index(drop=True)
    has = rows["score"].notna()
    return {**base, "n": int(has.sum()), "rows": rows,
            "first_score": rows["date"][has].iloc[0], "last_score": rows["date"][has].iloc[-1]}


def save_index(conn, code: str, result: dict, weights: dict) -> int:
    """把一个指数的评分整体替换进库(单事务: 读的一方不会看到半截), 返回出分天数。
    新结果一天分都没出、出分天数比旧表少一半以上、或最新出分日倒退 → 抛错并保留旧表
    (多半是上游数据缺了一段)。"""
    conn.execute(SCHEMA)
    old_n, old_last = conn.execute(
        f"SELECT COUNT(score), MAX(CASE WHEN score IS NOT NULL THEN date END) "
        f"FROM {TABLE} WHERE index_code=?", (code,)).fetchone()
    rows = result["rows"]
    has = rows["score"].notna()
    n_new = int(has.sum())
    if n_new == 0:
        why = f"(最后一个交易日 {result['last_bar']} {result['last_note']})" if result.get("last_note") else ""
        raise RuntimeError(f"[index-score] {code} 没有任何出了分的日子{why}, 保留旧表({old_n or 0} 行)")
    new_last = rows.loc[has, "date"].iloc[-1]
    if old_n and (n_new < old_n * 0.5 or str(new_last) < str(old_last)):
        raise RuntimeError(f"[index-score] {code} 新结果 {n_new} 日 / 最新 {new_last}, "
                           f"旧表 {old_n} 日 / 最新 {old_last} — 保留旧表")
    wj = json.dumps(weights, ensure_ascii=False)
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute(f"DELETE FROM {TABLE} WHERE index_code=?", (code,))
        cur.executemany(_INSERT, [
            (code, r["date"], None if pd.isna(r["score"]) else float(r["score"]), _none(r["stage"]),
             _none(r["components"]), wj, _none(r["note"])) for r in rows.to_dict("records")])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return n_new


def run(codes=None, cfg: dict = None, conn=None) -> dict:
    """算 + 存。返回 {code: result}; 单个指数出错只记录不抛(更新链不因它中断, 数据管理页会显示它落后)"""
    cfg = cfg or SC.load_cfg()
    specs = [sp for sp in index_specs(cfg) if not codes or sp["code"] in codes]
    own = conn is None
    con = connect() if own else conn
    out = {}
    try:
        for sp in specs:
            code = sp["code"]
            name = sp.get("name", code)
            try:
                res = compute_index(code, cfg, con)
                if res.get("msg"):
                    print(f"[index-score] {name}: {res['msg']}")
                    out[code] = res
                    continue
                res["saved"] = save_index(con, code, res, cfg["weights"])
                out[code] = res
                for _, r in res["rows"][res["rows"]["score"].notna()].tail(5).iterrows():
                    print(f"{name:<6} {r['date']}  情绪分 {r['score']:+6.1f}  [{r['stage']}]")
                lag = ""
                if res["last_note"]:
                    lag = f"; 最新 {res['last_bar']} 还没出分({res['last_note']})"
                mid = int(res["rows"]["score"].isna().sum()) - (1 if res["last_note"] else 0)
                if mid > 0:
                    lag += f"; 其间另有 {mid} 日未出分"
                print(f"[index-score] {name}: {res['n']} 日 "
                      f"({res['first_score']} ~ {res['last_score']}, 行情自 {res['first_bar']}){lag}")
            except Exception as e:                               # noqa: BLE001
                out[code] = {"spec": sp, "error": f"{type(e).__name__}: {e}"}
                print(f"[index-score] {name} 失败: {type(e).__name__}: {e}")
    finally:
        if own:
            con.close()
    return out


def main():
    ap = argparse.ArgumentParser(description="指数情绪分(深证成指 / 创业板指 / 科创50)")
    ap.add_argument("--index", action="append", help="只算指定代码(默认 config index.extra 全部)")
    a = ap.parse_args()
    out = run(a.index)
    if not out:
        print(f"[index-score] config index.extra 里没有{(' ' + str(a.index)) if a.index else '指数'}")
        sys.exit(2 if a.index else 0)
    bad = [c for c, r in out.items() if r.get("error") or r.get("msg")]
    sys.exit(1 if len(bad) == len(out) else 3 if bad else 0)


if __name__ == "__main__":
    main()
