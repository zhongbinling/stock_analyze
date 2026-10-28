# -*- coding: utf-8 -*-
"""D1-17 R2: 实时情绪分 — 注入"截至当前时点"快照行, 复用收盘评分管线

同一 metrics 派生 + 同一 score 分量/权重/EMA: 滚动窗口全部后视, 前缀不变
则历史分量不变, 今日行变化只影响今日 → 任一时点实时分严格等于
"该日就此收盘"的终值(同构由构造保证, replay 回放对账验证)。

快照 dict 字段(池字段可部分缺, 缺失分量由加权归一化吸收):
  date 'YYYY-MM-DD' / open high low close / volume1 amount1 volume2 amount2
  zt_count dt_count zb_rate lb_max jl_rate   (涨停结构, tushare口径)

用法:
  python rt_score.py                       # 实时: 拉快照 → 今日实时分
  python rt_score.py --replay 2026-10-27   # 回放: 当日收盘数据重建快照,
                                           # 与库内 sentiment.score 对账
"""
import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db import connect                                     # noqa: E402
from metrics import fetch_frame, derive                    # noqa: E402
import score as SC                                         # noqa: E402

# qt.gtimg 指数快照: 上证(量价主) + 深综(两市量能)
QT_INDEX_URL = "https://qt.gtimg.cn/q=sh000001,sz399106"


def rt_frame(conn, cfg, snap: dict):
    """历史(< 快照日, 库内已有该日则先删) + 合成快照行 → derive"""
    import pandas as pd
    df = fetch_frame(conn, cfg["index"]["primary"], cfg["index"]["secondary"])
    df = df[df["date"] < pd.Timestamp(snap["date"])].reset_index(drop=True)
    row = {"date": pd.Timestamp(snap["date"]),
           "open": snap.get("open"), "high": snap.get("high"),
           "low": snap.get("low"), "close": snap.get("close"),
           "volume": snap.get("volume1"), "amount": snap.get("amount1"),
           "volume2": snap.get("volume2"), "amount2": snap.get("amount2")}
    for k in ("zt_count", "dt_count", "zb_rate", "lb_max", "jl_rate"):
        row[k] = snap.get(k)
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    return derive(df)


def compute_rt(conn, snap: dict, cfg=None):
    """今日实时分: 返回 {score, v1, stage, comps, date}"""
    cfg = cfg or SC.load_cfg()
    df = rt_frame(conn, cfg, snap)
    comp = SC.component_scores(df, cfg["window"], cfg.get("pool_min_periods", 60))
    s = SC.weighted_score(comp, cfg["weights"], cfg["smooth"])
    v1 = SC.weighted_score(comp, cfg["v1_weights"], cfg["smooth"])
    i = len(df) - 1
    st = SC.stage_label(s.iloc[-1], cfg["thresholds"])
    return {"date": snap["date"], "score": round(float(s.iloc[-1]), 1),
            "v1": round(float(v1.iloc[-1]), 1), "stage": st,
            "comps": {k: (None if comp[k].isna().iloc[i] else round(float(comp[k].iloc[i]), 1))
                      for k in comp.columns}}


def quote_from_qt(a: dict, b: dict, fallback_date: str) -> dict:
    """qt双指数解析段 → 量价快照字段; 无效(盘前未出数/价格0)返回{}

    快照日取 qt 自带时间戳: 非交易日轮询返回的是上一交易日的数据,
    若用"今天"当快照日会在历史里造出幻影休息日; 用 ts 日则等价于对该
    交易日做回放, 语义自洽。high/low 缺失时以现价兜底(derive需有限值)。"""
    def _f(v):
        try:
            x = float(v)
            return x if x > 0 else None
        except (TypeError, ValueError):
            return None

    close = _f(a.get("price"))
    if close is None:
        return {}
    ts = str(a.get("ts") or "")
    d = (f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
         if len(ts) >= 8 and ts[:8].isdigit() else fallback_date)
    return {"date": d, "close": close,
            "open": _f(a.get("open")),
            "high": _f(a.get("high")) or close,
            "low": _f(a.get("low")) or close,
            "volume1": _f(a.get("vol_hand")) or 0,
            "amount1": (_f(a.get("amount_wan")) or 0) * 1e4,
            "volume2": _f(b.get("vol_hand")) or 0,
            "amount2": (_f(b.get("amount_wan")) or 0) * 1e4}


def fetch_snapshot(conn) -> dict:
    """实时快照(R1选型源): qt双指数量价 + tushare涨停结构(与每日
    fetch_tushare_mood_today 口径逐行一致; 失败→东财四池降级)"""
    from probe_rt import _curl, parse_qt
    snap = {"date": time.strftime("%Y-%m-%d")}
    code, body = _curl(QT_INDEX_URL)
    if code == 200:
        segs = [parse_qt(s + ";") for s in body.split(";") if "v_" in s]
        segs = [s for s in segs if s]
        if len(segs) >= 2:
            snap.update(quote_from_qt(segs[0], segs[1], snap["date"]))
    snap.update(_pool_snapshot(conn))
    return snap


def _pool_snapshot(conn) -> dict:
    """涨停结构(盘中即实时): tushare limit_list_d 主源, 东财四池降级"""
    try:
        import tushare_api as T
        if T.has_token():
            df = T.limit_list_d(time.strftime("%Y%m%d"))
            if df is not None and not df.empty:
                import pandas as pd
                u = df[df["limit"] == "U"]
                dn = df[df["limit"] == "D"]
                z = df[df["limit"] == "Z"]
                lb = pd.to_numeric(u.get("limit_times"), errors="coerce")
                lb2 = int((lb >= 2).sum()) if len(lb) else 0
                zt, zb = len(u), len(z)
                prev = conn.execute(
                    "SELECT zt_count FROM mood_raw WHERE date<? AND zt_count IS NOT NULL "
                    "ORDER BY date DESC LIMIT 1", (time.strftime("%Y-%m-%d"),)).fetchone()
                return {"zt_count": zt, "dt_count": len(dn),
                        "zb_rate": round(zb / (zt + zb) * 100, 1) if (zt + zb) else 0.0,
                        "lb_max": int(lb.max()) if lb.notna().any() else 0,
                        "jl_rate": round(lb2 / prev[0] * 100, 1) if prev and prev[0] else None}
    except Exception as e:                                  # noqa: BLE001
        print(f"[rt] tushare涨停结构失败: {e}")
    try:
        import em_api
        st = em_api.em_pool_stats(time.strftime("%Y%m%d"))
        if st:
            prev = conn.execute(
                "SELECT zt_count FROM mood_raw WHERE date<? AND zt_count IS NOT NULL "
                "ORDER BY date DESC LIMIT 1", (time.strftime("%Y-%m-%d"),)).fetchone()
            return {"zt_count": st["zt_count"], "dt_count": st["dt_count"],
                    "zb_rate": st["zb_rate"], "lb_max": st["lb_max"],
                    "jl_rate": None if not (prev and prev[0]) or not st.get("lb2_count")
                    else round(st["lb2_count"] / prev[0] * 100, 1)}
    except Exception as e:                                  # noqa: BLE001
        print(f"[rt] 东财四池降级失败(池分量缺失, 权重归一吸收): {e}")
    return {}


def replay(conn, date_iso: str, cfg=None):
    """回放对账: 用当日收盘数据重建快照 → 实时分 vs 库内 sentiment.score"""
    cfg = cfg or SC.load_cfg()
    row = conn.execute(
        "SELECT open,high,low,close,volume,amount FROM index_daily "
        "WHERE code=? AND date=?", (cfg["index"]["primary"], date_iso)).fetchone()
    row2 = conn.execute(
        "SELECT volume,amount FROM index_daily WHERE code=? AND date=?",
        (cfg["index"]["secondary"], date_iso)).fetchone()
    mood = conn.execute(
        "SELECT zt_count,dt_count,zb_rate,lb_max,jl_rate FROM mood_raw WHERE date=?",
        (date_iso,)).fetchone()
    if not row:
        return None
    snap = {"date": date_iso, "open": row[0], "high": row[1], "low": row[2],
            "close": row[3], "volume1": row[4], "amount1": row[5],
            "volume2": row2[0] if row2 else None, "amount2": row2[1] if row2 else None}
    if mood:
        snap.update(dict(zip(("zt_count", "dt_count", "zb_rate", "lb_max", "jl_rate"), mood)))
    rt = compute_rt(conn, snap, cfg)
    stored = conn.execute("SELECT score FROM sentiment WHERE date=?",
                          (date_iso,)).fetchone()
    rt["stored"] = stored[0] if stored else None
    rt["diff"] = None if stored is None or stored[0] is None else round(rt["score"] - stored[0], 2)
    return rt


def main():
    ap = argparse.ArgumentParser(description="实时情绪分(D1-17 R2)")
    ap.add_argument("--replay", metavar="DATE", help="回放对账 YYYY-MM-DD")
    args = ap.parse_args()
    conn = connect()
    try:
        if args.replay:
            r = replay(conn, args.replay)
            if not r:
                print(f"[replay] {args.replay} 无指数数据")
                return 1
            print(f"[replay] {r['date']}: 实时法 {r['score']:+.1f} vs 库内 "
                  f"{r['stored'] if r['stored'] is not None else '--'}"
                  + (f"  diff {r['diff']:+.2f}" if r["diff"] is not None else ""))
            print(f"  分量 {r['comps']}")
            return 0
        snap = fetch_snapshot(conn)
        r = compute_rt(conn, snap)
        print(f"[rt] {r['date']} {time.strftime('%H:%M:%S')} 实时情绪 {r['score']:+.1f}"
              f" [{r['stage']}]  V1 {r['v1']:+.1f}")
        print(f"  快照 close={snap.get('close')} vol1={snap.get('volume1')}"
              f" zt={snap.get('zt_count')} jl={snap.get('jl_rate')}")
        print(f"  分量 {r['comps']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
