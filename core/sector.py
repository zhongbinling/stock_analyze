# -*- coding: utf-8 -*-
"""板块温度引擎 (S6): 纵向温度(-100~100) + 横向热度(当日全板块百分位)

方法论与大盘情绪(QXXF)同构: 分量→250日滚动分位→中心化→加权(缺失归一化)→EMA(3)→限幅

用法:
  python sector.py calc               # 全量重算 → sector_scores
  python sector.py report [--date D] [--top N] [--code BKxxxx]
  python sector.py anchors            # 锚定回放
  python sector.py remap              # @行业名 自动前缀匹配 → sector_alias
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from db import connect  # noqa: E402

STAGES = [(-80, "极度冰点"), (-60, "冰点/恐慌"), (-30, "偏冷/退潮"), (-10, "中性偏弱"),
          (10, "中性"), (30, "中性偏强"), (60, "偏热/发酵"), (80, "狂热/高潮")]

CORDER = ["rsi", "chg", "gap", "kr", "vol_dir", "vr", "bias20", "bias60",
          "pos", "amp", "zt", "zt_share", "ud_ratio", "fflow"]


def stage_of(s):
    for th, name in STAGES:
        if s <= th:
            return name
    return "极度狂热"


def load_cfg():
    with open(BASE / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _pct(s, n, mp=None):
    lo = s.rolling(n, min_periods=mp or n).min()
    hi = s.rolling(n, min_periods=mp or n).max()
    return (s - lo) / (hi - lo)


def _asym(s_pos, chg):
    """非对称方向化: 放量×当日方向; 缩量恒负, 缩量涨减半(与大盘V2.1口径一致)"""
    d = np.where(chg >= 0, 1, -1)
    return pd.Series(np.where(s_pos > 0, s_pos * d,
                              np.where(chg >= 0, s_pos * 0.5, s_pos)),
                     index=s_pos.index)


# ---------------------------------------------------------------- 数据装载

def load_frames(conn):
    daily = pd.read_sql(
        "SELECT d.code AS code, d.date, d.open, d.close, d.high, d.low, d.amount, "
        "d.up_count, d.down_count FROM sector_daily d JOIN sectors s ON d.code=s.code "
        "WHERE s.kind='industry' AND s.enabled=1", conn, parse_dates=["date"])
    pool = pd.read_sql(
        "SELECT date,code,zt_count,dt_count,lb_max,lb2_count FROM sector_pool_stat "
        "WHERE code LIKE 'BK%'", conn, parse_dates=["date"])
    fflow = pd.read_sql(
        "SELECT date,code,main_net FROM sector_fundflow", conn, parse_dates=["date"])
    mkt = pd.read_sql(
        "SELECT date,zt_count FROM mood_raw WHERE zt_count IS NOT NULL",
        conn, parse_dates=["date"])
    daily["date"] = daily["date"].dt.strftime("%Y-%m-%d")
    pool["date"] = pool["date"].dt.strftime("%Y-%m-%d")
    fflow["date"] = fflow["date"].dt.strftime("%Y-%m-%d")
    mkt["date"] = mkt["date"].dt.strftime("%Y-%m-%d")
    return daily, pool, fflow, mkt


# ---------------------------------------------------------------- 单板块计算

def calc_one(df: pd.DataFrame, pool: pd.DataFrame, mkt_zt: pd.Series,
             weights: dict, window=250, pool_mp=60) -> pd.DataFrame:
    """单板块: 输入合并后的日线+涨停, 输出含 score 的明细"""
    prev = df["close"].shift(1)
    df["chg"] = (df["close"] / prev - 1) * 100
    df["amp"] = (df["high"] - df["low"]) / prev * 100
    df["gap"] = (df["open"] / prev - 1) * 100
    rng = (df["high"] - df["low"]).abs()
    df["kr"] = ((df["close"] - df["open"]) / rng.replace(0, np.nan)).rolling(3).mean()
    ma20 = df["close"].rolling(20).mean()
    ma60 = df["close"].rolling(60).mean()
    df["bias20"] = (df["close"] - ma20) / ma20 * 100
    df["bias60"] = (df["close"] - ma60) / ma60 * 100
    diff = df["close"].diff()
    up = diff.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = diff.abs().ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = up / dn * 100
    vr = df["amount"].rolling(5).mean() / df["amount"].rolling(20).mean()

    updown = np.where(df["chg"] >= 0, 1, -1)
    zt_share = df["zt_count"] / mkt_zt  # 板块涨停占全市场比重
    ud_ratio = (df["up_count"] - df["down_count"]) / (df["up_count"] + df["down_count"])

    comp = {}
    comp["rsi"] = _pct(df["rsi"], window) * 200 - 100
    comp["chg"] = _pct(df["chg"], window) * 200 - 100
    comp["gap"] = _pct(df["gap"], window) * 200 - 100
    comp["kr"] = _pct(df["kr"], window) * 200 - 100
    comp["vol_dir"] = _asym(_pct(df["amount"], window) * 200 - 100, df["chg"])
    comp["vr"] = _asym(_pct(vr, window) * 200 - 100, df["chg"])
    comp["bias20"] = _pct(df["bias20"], window) * 200 - 100
    comp["bias60"] = _pct(df["bias60"], window) * 200 - 100
    comp["pos"] = _pct(df["close"], window) * 200 - 100
    comp["amp"] = _pct(df["amp"], window) * 200 - 100
    comp["amp"] = comp["amp"] * pd.Series(updown, index=df.index)
    comp["zt"] = _pct(df["zt_count"], window, pool_mp) * 200 - 100
    comp["zt_share"] = _pct(zt_share, window, pool_mp) * 200 - 100
    comp["ud_ratio"] = _pct(ud_ratio, window, 20) * 200 - 100
    if "main_net" in df.columns:
        comp["fflow"] = _pct(df["main_net"], 60, 30) * 200 - 100

    w = pd.Series({k: float(v) for k, v in weights.items()})
    cdf = pd.DataFrame(comp, index=df.index)
    val = cdf[list(w.index)].mul(w, axis=1)
    wsum = cdf[list(w.index)].notna().mul(w, axis=1).sum(axis=1)
    raw = val.sum(axis=1) / wsum.mask(wsum <= 0)
    score = raw.ewm(alpha=2 / 4, adjust=False).mean().clip(-100, 100)

    out = pd.DataFrame({"date": df["date"].values, "code": df["code"].values,
                        "score": score.round(1).values})
    out["components"] = [
        json.dumps({k: (None if pd.isna(v) else round(float(v), 1))
                    for k, v in row.items() if pd.notna(v)}, ensure_ascii=False)
        for _, row in cdf.iterrows()]
    return out


# ---------------------------------------------------------------- 全量计算

def calc_all(conn=None):
    t0 = time.time()
    conn = conn or connect()
    cfg = load_cfg()
    weights = cfg.get("sector_weights", {})
    window = cfg.get("window", 250)
    daily, pool, fflow, mkt = load_frames(conn)
    mkt_zt = daily[["date"]].merge(mkt, on="date", how="left")["zt_count"]
    daily["zt_count"] = pool.groupby(["date", "code"])["zt_count"].sum().reindex(
        pd.MultiIndex.from_frame(daily[["date", "code"]])).values
    for col in ("lb_max", "lb2_count", "dt_count"):
        daily[col] = pool.groupby(["date", "code"])[col].max().reindex(
            pd.MultiIndex.from_frame(daily[["date", "code"]])).values
    daily["main_net"] = fflow.groupby(["date", "code"])["main_net"].sum().reindex(
        pd.MultiIndex.from_frame(daily[["date", "code"]])).values

    out = []
    comp_keep_days = 260           # components 明细仅保留近窗口日数(控制库体积)
    for code, g in daily.groupby("code", sort=True):
        g = g.sort_values("date").reset_index(drop=True)
        g["mkt"] = g["date"].map(mkt.set_index("date")["zt_count"])
        res = calc_one(g, None, g["mkt"], weights, window)
        if len(res) > comp_keep_days:
            res.loc[:len(res) - comp_keep_days - 1, "components"] = None
        out.append(res)
    allsc = pd.concat(out, ignore_index=True)
    allsc["rank_pct"] = allsc.groupby("date")["score"].rank(pct=True) * 100
    allsc["stage"] = allsc["score"].apply(stage_of)

    conn.execute("DELETE FROM sector_scores")
    conn.executemany(
        "INSERT OR REPLACE INTO sector_scores(date,code,score,rank_pct,stage,components) "
        "VALUES(?,?,?,?,?,?)",
        [(r["date"], r["code"], float(r["score"]),
          None if pd.isna(r["rank_pct"]) else round(float(r["rank_pct"]), 1),
          r["stage"], r["components"] if r["components"] else None)
         for _, r in allsc.iterrows()])
    conn.commit()
    conn.execute("VACUUM")         # 回收历史components空间
    print(f"[sector] 温度计算: {len(out)} 板块 × {allsc['date'].nunique()} 日 → {len(allsc)} 行 "
          f"({time.time()-t0:.1f}s, 最新 {allsc['date'].max()})")
    return allsc


# ---------------------------------------------------------------- 自动别名

def auto_alias(conn=None):
    """@行业名 → BK 前缀唯一匹配 → sector_alias"""
    conn = conn or connect()
    pend = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM sector_pool_stat WHERE code LIKE '@%'")]
    names = conn.execute(
        "SELECT code,name FROM sectors WHERE kind='industry'").fetchall()
    fixed, left = 0, []
    for p in pend:
        n = p[1:]
        cands = [c for c, nm in names if nm.startswith(n) or n.startswith(nm)]
        if len(cands) == 1:
            conn.execute("INSERT OR REPLACE INTO sector_alias(name,code) VALUES(?,?)",
                         (n, cands[0]))
            fixed += 1
        else:
            left.append((n, len(cands)))
    conn.commit()
    if fixed:
        from sectors import remap_pending
        remap_pending(conn)
    print(f"[sector] 自动别名: 唯一匹配 {fixed}/{len(pend)}")
    if left:
        print("  待人工确认(候选数):", left[:15])


# ---------------------------------------------------------------- 报告/锚定

def report(conn=None, date=None, top=15, code=None):
    conn = conn or connect()
    date = date or conn.execute("SELECT MAX(date) FROM sector_scores").fetchone()[0]
    if code:
        rows = conn.execute(
            "SELECT date,score,rank_pct,stage,components FROM sector_scores "
            "WHERE code=? ORDER BY date DESC LIMIT 10", (code,)).fetchall()
        nm = conn.execute("SELECT name FROM sectors WHERE code=?", (code,)).fetchone()
        print(f"── {code} {nm[0] if nm else ''} 最近10日 ──")
        for r in rows:
            print(f"  {r[0]}  {r[1]:+6.1f} [{r[3]}] 排名{r[2] or 0:.0f}%")
        return
    rows = conn.execute(
        "SELECT sc.code, s2.name, sc.score, sc.rank_pct, sc.stage FROM sector_scores sc "
        "JOIN sectors s2 ON sc.code=s2.code WHERE sc.date=? "
        "ORDER BY sc.score DESC", (date,)).fetchall()
    hot = rows[:top]
    cold = sorted(rows, key=lambda r: r[2])[:top]
    print(f"── 板块温度 {date} · 共{len(rows)}板块 ──")
    print("  最热:")
    for r in hot:
        print(f"    {r[1]:<8} {r[2]:+6.1f} [{r[4]}] 排名{r[3] or 0:.0f}%")
    print("  最冷:")
    for r in cold:
        print(f"    {r[1]:<8} {r[2]:+6.1f} [{r[4]}] 排名{r[3] or 0:.0f}%")


ANCHORS = [
    ("2024-10-08", "证券", +60), ("2024-10-08", "半导体", +60),
    ("2024-09-30", "证券", +60), ("2024-02-05", "软件", -30),
    ("2024-02-05", "传媒", -30),
]


def anchors(conn=None):
    conn = conn or connect()
    print("── 板块温度锚定回放 ──")
    ok = tot = 0
    for d, kw, want in ANCHORS:
        rows = conn.execute(
            "SELECT s2.name, sc.score FROM sector_scores sc JOIN sectors s2 "
            "ON sc.code=s2.code WHERE sc.date=? AND s2.name LIKE ?", (d, f"%{kw}%")).fetchall()
        for nm, sc in rows[:3]:
            hit = (sc > 0) == (want > 0)
            tot += 1
            ok += hit
            print(f"  {d} {nm:<8} {sc:+6.1f} (期望{'正' if want > 0 else '负'}) "
                  f"{'✓' if hit else '✗'}")
    # 横向有效性: 前10 vs 后10 次日涨幅
    df = pd.read_sql(
        "SELECT date,code,score FROM sector_scores", conn)
    daily = pd.read_sql("SELECT date,code,close FROM sector_daily", conn)
    daily["fwd"] = daily.groupby("code")["close"].shift(-1) / daily["close"] - 1
    m = df.merge(daily[["date", "code", "fwd"]], on=["date", "code"])
    m["rk"] = m.groupby("date")["score"].rank(pct=True)
    top = m[m["rk"] > 0.9]["fwd"].mean() * 100
    bot = m[m["rk"] < 0.1]["fwd"].mean() * 100
    print(f"  横向: 温度前10%板块次日均涨 {top:+.2f}% vs 后10% {bot:+.2f}% "
          f"({'✓ 有效' if top > bot else '✗ 无效'})  锚点 {ok}/{tot}")


def export(conn=None, date=None, top=30):
    """导出当日板块温度 TopN 为 CSV(含分量明细), 可与 tdx 涨停生态公式对照"""
    conn = conn or connect()
    date = date or conn.execute("SELECT MAX(date) FROM sector_scores").fetchone()[0]
    rows = conn.execute(
        "SELECT sc.code, s2.name, sc.score, sc.rank_pct, sc.stage, sc.components "
        "FROM sector_scores sc JOIN sectors s2 ON sc.code=s2.code "
        "WHERE sc.date=? ORDER BY sc.score DESC", (date,)).fetchall()
    if not rows:
        print("无数据")
        return
    import csv
    out = BASE / "out" / f"sector_top_{date.replace('-', '')}.csv"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["排名", "代码", "板块名", "温度", "排名百分位", "阶段",
                    "RSI", "涨幅", "跳空", "实体", "量能", "量比", "乖离20", "乖离60",
                    "位置", "振幅", "涨停", "涨停占比", "涨跌比", "主力净流入"])
        for i, r in enumerate(rows[:top], 1):
            c = json.loads(r[5]) if r[5] else {}
            w.writerow([i, r[0], r[1], r[2], r[3], r[4]] +
                       [c.get(k, "") for k in CORDER])
    print(f"[sector] 导出: {out} (Top {min(top, len(rows))}, {date})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["calc", "report", "anchors", "remap", "export"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--code", default=None)
    args = ap.parse_args()
    conn = connect()
    if args.cmd == "calc":
        calc_all(conn)
    elif args.cmd == "report":
        report(conn, args.date, args.top, args.code)
    elif args.cmd == "anchors":
        anchors(conn)
    elif args.cmd == "export":
        export(conn, args.date, args.top)
    else:
        auto_alias(conn)


if __name__ == "__main__":
    main()
