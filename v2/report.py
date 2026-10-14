# -*- coding: utf-8 -*-
"""输出层: 当日情绪报告(文本) + 历史曲线图(matplotlib)

用法:
  python report.py                # 最新交易日情绪报告
  python report.py --date 2026-10-09
  python report.py --chart        # 另存历史曲线图 out/sentiment_history.png
  python report.py --chart --years 2
"""
import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402

from score import compute, load_cfg  # noqa: E402
from metrics import LABELS  # noqa: E402

# ---- 图表规范(dataviz 参考色板) ----
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"   # series-1: 情绪分主线
RED = "#e34948"    # diverging 暖极: 狂热区
FONT = ["Microsoft YaHei", "SimHei", "Segoe UI", "sans-serif"]


def text_report(cfg, df, comp, score_df, date_str=None):
    valid = score_df.dropna(subset=["score"])
    if valid.empty:
        print("无可用评分(数据不足), 请先运行 fetch_daily.py")
        return
    if date_str:
        row = valid[valid["date"].dt.strftime("%Y-%m-%d") == date_str]
        if row.empty:
            print(f"{date_str} 无评分(可能非交易日或数据缺失)")
            return
    else:
        row = valid.tail(1)
    i = row.index[0]
    r = row.iloc[0]
    prev = valid.loc[valid.index < i]
    prev_score = prev["score"].iloc[-1] if not prev.empty else float("nan")

    raw = df.loc[i]
    line = "═" * 56
    print(line)
    print(f"  A股市场情绪日报  {r['date'].strftime('%Y-%m-%d')}")
    print(line)
    delta = r["score"] - prev_score if pd.notna(prev_score) else float("nan")
    print(f"  情绪分: {r['score']:+.1f} / 100   [{r['stage']}]")
    print(f"  前日: {prev_score:+.1f}   日变化: {delta:+.1f}")
    print(f"  V1量价对照分(通达信口径): {r['v1_score']:+.1f}")
    print()
    print(f"  短线情绪: 涨停 {fmt(raw.get('zt_count'))} 家 | 跌停 {fmt(raw.get('dt_count'))} 家 | "
          f"炸板率 {fmt(raw.get('zb_rate'))}% | 连板高度 {fmt(raw.get('lb_max'))} | "
          f"接力率 {fmt(raw.get('jl_rate'))}% (2板+家数/昨日涨停)")
    print(f"  量价: 上证收盘 {fmt(raw.get('close'))} ({fmt(raw.get('chg'), plus=True)}%) | "
          f"两市成交量 {fmt((raw.get('vol_total') or 0) / 1e8)} 亿手 | RSI14 {fmt(raw.get('rsi'))}")
    print()

    # ---- 分项贡献表(当日实际生效权重 = 缺失分量归一化后) ----
    weights = cfg["weights"]
    s_row = comp.loc[i]
    keys = [k for k in weights if pd.notna(s_row.get(k))]
    wsum = sum(weights[k] for k in keys)
    print(f"  {'分量':<14}{'分位分':>8}{'权重':>8}{'贡献':>9}")
    print("  " + "-" * 42)
    contrib = []
    for k in keys:
        w_eff = weights[k] / wsum
        c = w_eff * s_row[k]
        contrib.append((k, c))
        print(f"  {LABELS.get(k, k):<14}{s_row[k]:>+8.1f}{w_eff:>8.2f}{c:>+9.1f}")
    print("  " + "-" * 42)
    top = max(contrib, key=lambda t: abs(t[1]))
    print(f"  主导分量: {LABELS.get(top[0], top[0])} ({top[1]:+.1f})")
    missing = [LABELS.get(k, k) for k in weights if k not in keys]
    if missing:
        print(f"  缺失分量(已归一化剔除): {'、'.join(missing)}")
    print(line)


def fmt(v, plus=False):
    if v is None or pd.isna(v):
        return "--"
    if isinstance(v, float):
        if float(v).is_integer():
            return str(int(v))
        return f"{v:+.2f}" if plus else f"{v:.2f}"
    return str(v)


def chart(cfg, df, score_df, years=None, out_png=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = FONT
    plt.rcParams["axes.unicode_minus"] = False

    d = score_df.dropna(subset=["score"]).copy()
    if d.empty:
        print("无数据可画")
        return
    if years:
        cutoff = d["date"].max() - pd.DateOffset(years=years)
        d = d[d["date"] >= cutoff]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 6.4), sharex=True, dpi=150,
        gridspec_kw={"height_ratios": [1, 1.7], "hspace": 0.22})
    fig.patch.set_facecolor(SURFACE)

    for ax in (ax1, ax2):
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=9)
        ax.xaxis_date()

    # 面板1: 指数收盘价(背景参考, 弱化灰)
    ax1.plot(d["date"], d["close"], color=MUTED, linewidth=1.6)
    ax1.set_title("上证指数收盘价（背景参考）", loc="left", fontsize=10, color=INK2, pad=8)

    # 面板2: 情绪分(单序列蓝线) + 极性分区(红=狂热/蓝=冰点)
    ax2.axhspan(60, 100, color=RED, alpha=0.06)
    ax2.axhspan(80, 100, color=RED, alpha=0.07)
    ax2.axhspan(-100, -60, color=BLUE, alpha=0.06)
    ax2.axhspan(-100, -80, color=BLUE, alpha=0.07)
    for y in (-60, -30, 30, 60):
        ax2.axhline(y, color=AXIS, linewidth=0.7, linestyle=(0, (4, 4)))
    ax2.axhline(0, color=AXIS, linewidth=1.0)
    ax2.plot(d["date"], d["score"], color=BLUE, linewidth=2.0)
    ax2.set_ylim(-105, 105)
    ax2.set_yticks([-100, -60, -30, 0, 30, 60, 100])
    ax2.set_title("A股市场情绪分（-100 ～ +100）", loc="left",
                  fontsize=11.5, color=INK, fontweight="bold", pad=8)

    last = d.iloc[-1]
    ax2.annotate(f"{last['score']:+.0f}", (last["date"], last["score"]),
                 textcoords="offset points", xytext=(8, 0),
                 color=INK2, fontsize=10, fontweight="bold")

    out_png = out_png or (BASE / "out" / "sentiment_history.png")
    out_png = Path(out_png)
    out_png.parent.mkdir(exist_ok=True)
    fig.savefig(out_png, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    print(f"[chart] {out_png}  ({d['date'].iloc[0].date()} ~ {d['date'].iloc[-1].date()}, {len(d)} 交易日)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD")
    ap.add_argument("--chart", action="store_true", help="输出历史曲线图")
    ap.add_argument("--years", type=int, default=None, help="图示年限(默认全部)")
    args = ap.parse_args()

    cfg = load_cfg()
    df, comp, score_df = compute(cfg)
    if args.chart:
        chart(cfg, df, score_df, years=args.years)
    else:
        text_report(cfg, df, comp, score_df, args.date)


if __name__ == "__main__":
    main()
