# -*- coding: utf-8 -*-
"""校验层: 锚点日回放 + IC + 分位组单调性

用法: python backtest.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402

from score import compute, load_cfg  # noqa: E402

# 锚点日(权威极端日): 日期, 期望方向, 说明
ANCHORS = [
    ("2015-06-12", +1, "5178 杠杆牛顶(狂热)"),
    ("2015-08-24", -1, "股灾2.0 千股跌停(恐慌)"),
    ("2016-01-04", -1, "熔断(恐慌)"),
    ("2018-12-24", -1, "长熊尾部(冰点)"),
    ("2020-07-09", +1, "放量急涨(亢奋)"),
    ("2024-02-05", -1, "微盘流动性危机(极度冰点; 注意V1量价版因护盘会低估)"),
    ("2024-09-30", +1, "政策行情(极度狂热)"),
    ("2024-10-08", +1, "国庆后高潮"),
]


def nearest(valid, date_str, tol=3):
    """最近交易日(±tol天内)"""
    t = pd.Timestamp(date_str)
    dd = (valid["date"] - t).abs()
    i = dd.idxmin()
    return i if dd.loc[i] <= pd.Timedelta(days=tol) else None


def run_anchors(score_df):
    valid = score_df.dropna(subset=["score"])
    print("═" * 64)
    print("  锚点日回放 (分数符号应与期望方向一致, 量级≥30为佳)")
    print("═" * 64)
    hits = total = 0
    for dstr, want, note in ANCHORS:
        i = nearest(valid, dstr)
        if i is None:
            print(f"  {dstr}  {'✗':<4} 无数据(池数据未回补到该日期)  {note}")
            continue
        r = valid.loc[i]
        ok = (r["score"] > 0) == (want > 0)
        total += 1
        hits += ok
        v1ok = "" if pd.isna(r["v1_score"]) else f" V1:{r['v1_score']:+.0f}"
        print(f"  {r['date'].strftime('%Y-%m-%d')}  {'✓' if ok else '✗'}   "
              f"V2:{r['score']:+6.1f}{v1ok}   {note}")
    if total:
        print(f"  命中 {hits}/{total}")
    print()


def run_ic(score_df, horizon=5):
    valid = score_df.dropna(subset=["score"]).copy()
    fwd = valid["close"].shift(-horizon) / valid["close"] - 1
    m = fwd.notna() & valid["score"].notna()
    if m.sum() < 60:
        print(f"[IC] 有效样本不足({int(m.sum())}), 跳过")
        return
    ic = valid.loc[m, "score"].rank().corr(fwd[m].rank())  # Spearman
    print(f"  情绪分 vs 未来{horizon}日收益  Spearman IC = {ic:+.3f}  "
          f"(样本 {int(m.sum())} 日; 负值=高分后反而跌, 属情绪反向属性, 量级适中为宜)")
    ic_v1 = valid.loc[m, "v1_score"].rank().corr(fwd[m].rank())
    print(f"  V1量价分 vs 未来{horizon}日收益  Spearman IC = {ic_v1:+.3f}")
    print()


def run_quintiles(score_df, horizon=5):
    valid = score_df.dropna(subset=["score"]).copy()
    valid["fwd"] = valid["close"].shift(-horizon) / valid["close"] - 1
    valid = valid.dropna(subset=["fwd"])
    if len(valid) < 100:
        print(f"[分位组] 样本不足({len(valid)}), 跳过")
        return
    try:
        valid["组"] = pd.qcut(valid["score"], 5,
                              labels=["Q1冰点", "Q2", "Q3", "Q4", "Q5狂热"])
    except ValueError:
        print("[分位组] 分数取值过于集中, 跳过")
        return
    g = valid.groupby("组", observed=True)["fwd"].agg(["mean", "count"])
    print("  按情绪分五分组 → 未来5日平均收益(理想: Q1 > Q5, 单调为佳):")
    for name, row in g.iterrows():
        bar = "▇" * max(0, int(abs(row["mean"]) * 2000))
        sign = "+" if row["mean"] >= 0 else "-"
        print(f"    {name:<6} {row['mean'] * 100:+6.2f}%  n={int(row['count']):<5} {sign}{bar}")
    print()


def main():
    cfg = load_cfg()
    df, comp, score_df = compute(cfg)
    print()
    run_anchors(score_df)
    print("═" * 64)
    print("  信息系数(IC)检验")
    print("═" * 64)
    run_ic(score_df, horizon=5)
    print("═" * 64)
    print("  分位组单调性")
    print("═" * 64)
    run_quintiles(score_df, horizon=5)
    print("  提示: 池数据回补越完整, 以上统计越可靠; 当前结果仅供方向性参考。")


if __name__ == "__main__":
    main()
