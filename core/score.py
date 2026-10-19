# -*- coding: utf-8 -*-
"""评分层: 分量分位标准化 → 加权合成(缺失自动归一化) → EMA平滑 → 限幅 → 分档

用法:
  python score.py              # 计算并打印最近5日
  python score.py --csv        # 全量导出 out/sentiment.csv
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from db import connect  # noqa: E402
from metrics import build_frame, COMPONENTS, LABELS, POOL_KEYS  # noqa: E402


def load_cfg():
    """读 config.yaml, 并叠加 config_override.yaml / 环境变量 SENTIMENT_OVERRIDE 指向的文件
    (管理台用于临时或持久化覆盖权重等参数)"""
    import os

    def _deep_merge(base, over):
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                _deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    with open(BASE / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for p in [Path(os.environ.get("SENTIMENT_OVERRIDE", "")) if os.environ.get("SENTIMENT_OVERRIDE") else None,
              BASE / "config_override.yaml"]:
        if p and Path(p).exists():
            with open(p, encoding="utf-8") as f:
                _deep_merge(cfg, yaml.safe_load(f) or {})
    return cfg


def pct_pos(s: pd.Series, n: int, min_periods: int = None) -> pd.Series:
    """滚动分位 0~100 (与通达信 (X-LLV)/(HHV-LLV) 一致), 窗口不足为 NaN"""
    mp = min_periods or n
    lo = s.rolling(n, min_periods=mp).min()
    hi = s.rolling(n, min_periods=mp).max()
    return (s - lo) / (hi - lo) * 100


def component_scores(df: pd.DataFrame, window: int, pool_min_periods: int = 60) -> pd.DataFrame:
    """每个分量 → -100~+100 的分位分(方向已含)。
    池分量历史较短(同花顺约一年), 用较短的 min_periods 提前生效。"""
    import numpy as np

    chg = df["chg"]
    d_updown = chg.apply(lambda c: 1 if c >= 0 else -1)
    out = {}
    for key, _label, sign, mode, group in COMPONENTS:
        x = df[key]
        if mode == "rsi":
            s = (x - 50) * 2
        else:
            mp = pool_min_periods if group == "pool" else window
            s = pct_pos(x, window, mp) * 2 - 100
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


def weighted_score(comp: pd.DataFrame, weights: dict, smooth: int) -> pd.Series:
    """按行对可用分量归一化加权 → EMA平滑 → 限幅±100"""
    keys = [k for k in weights if k in comp.columns]
    w = pd.Series({k: weights[k] for k in keys})
    val = comp[keys].mul(w, axis=1)
    wsum = comp[keys].notna().mul(w, axis=1).sum(axis=1)
    raw = val.sum(axis=1) / wsum.mask(wsum <= 0)
    ema = raw.ewm(alpha=2 / (smooth + 1), adjust=False).mean()
    return ema.clip(-100, 100)


def stage_label(score, thresholds, labels=None):
    if pd.isna(score):
        return ""
    labels = labels or ["极度冰点", "冰点/恐慌", "偏冷/退潮", "中性偏弱", "中性",
                        "中性偏强", "偏热/发酵", "狂热/高潮", "极度狂热"]
    th = sorted(thresholds)
    # 分档: score<=th[0]→0档 ... 依序
    idx = 0
    for i, t in enumerate(th):
        if score > t:
            idx = i + 1
    return labels[idx]


def compute(cfg=None, conn=None):
    """返回 (df, comp, score_df): df=指标宽表, comp=分量分, score_df含score/v1_score/stage"""
    cfg = cfg or load_cfg()
    conn = conn or connect()
    df = build_frame(conn, cfg["index"]["primary"], cfg["index"]["secondary"])
    comp = component_scores(df, cfg["window"], cfg.get("pool_min_periods", 60))
    score_df = pd.DataFrame({
        "date": df["date"],
        "close": df["close"],
        "score": weighted_score(comp, cfg["weights"], cfg["smooth"]),
        "v1_score": weighted_score(comp, cfg["v1_weights"], cfg["smooth"]),
    })
    score_df["stage"] = score_df["score"].apply(lambda s: stage_label(s, cfg["thresholds"]))
    return df, comp, score_df


def save_to_db(conn, score_df, comp):
    """D1-7: 全量写 staging → 校验 → 原子切换(重建期间旧表全程可读, 崩溃零影响)"""
    from db import atomic_swap, make_staging
    st = make_staging(conn, "sentiment")
    cur = conn.cursor()
    for i, row in score_df.iterrows():
        if pd.isna(row["score"]):
            continue
        cj = {k: (None if pd.isna(v) else round(float(v), 1))
              for k, v in comp.iloc[i].items()}
        cur.execute(
            f"INSERT INTO {st}(date,score,stage,components) VALUES(?,?,?,?)",
            (row["date"].strftime("%Y-%m-%d"), round(float(row["score"]), 1),
             row["stage"], json.dumps(cj, ensure_ascii=False)))
    conn.commit()
    atomic_swap(conn, "sentiment")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="store_true", help="导出全量 CSV 到 out/")
    args = ap.parse_args()

    cfg = load_cfg()
    conn = connect()
    df, comp, score_df = compute(cfg, conn)
    save_to_db(conn, score_df, comp)

    if args.csv:
        out = BASE / "out"
        out.mkdir(exist_ok=True)
        merge = score_df.copy()
        for k in comp.columns:
            merge["s_" + k] = comp[k].round(1)
        merge.to_csv(out / "sentiment.csv", index=False, encoding="utf-8-sig")
        print(f"[csv] {len(merge)} 行 → out/sentiment.csv")

    tail = score_df.dropna(subset=["score"]).tail(5)
    for _, r in tail.iterrows():
        print(f"{r['date'].strftime('%Y-%m-%d')}  情绪分 {r['score']:+6.1f}  "
              f"[{r['stage']}]  V1对照 {r['v1_score']:+6.1f}")


if __name__ == "__main__":
    main()
