# -*- coding: utf-8 -*-
"""SW层级涨停结构 (D1-6 P2): 估算(2005起, raw精确涨停价) + 官方覆盖(2020+, limit_list_d)

方法:
- 涨停价 = round_half_up(pre_close × (1±限幅), 2); 板块限幅: 主板10%/创业板
  2020-08-24前10%后20%/科创板2019-07-22起20%/北交所30%
- 炸板 = high 触及涨停价 且 close 未封; 连板 = 逐日连续封板追踪(停牌缺数据重置, 近似)
- ST 5% 无法识别(已知偏低); 新股首日无涨跌幅→close≠涨停价自然不计
- sw_pool(level,index,date,zt,dt,zb,lb_max,lb2,source) 三层级; mood_raw 2005-2012
  缺失日填全结构估算(est2), 已有日/2020+官方段不动

用法: python sw_pool.py build|overlay|run|validate|check [--start 20050101]
"""
import argparse
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import tushare_api as T  # noqa: E402
from db import connect, upsert_mood  # noqa: E402
from raw_daily import raw_connect  # noqa: E402
from sw_components import members_on  # noqa: E402

OFFICIAL_START = "20200101"
_EPS = 0.005


def limit_ratio(ts_code: str, d_ymd: str) -> Decimal:
    c = ts_code.split(".")[0]
    if c.startswith(("300", "301")):
        return Decimal("0.20") if d_ymd >= "20200824" else Decimal("0.10")
    if c.startswith(("688", "689")):                 # 科创板(689=CDR, 如九号公司)
        return Decimal("0.20") if d_ymd >= "20190722" else Decimal("0.10")
    if c.startswith(("83", "87", "88", "43", "92")):
        return Decimal("0.30")                      # 北交所
    return Decimal("0.10")


def limit_price(pre: float, ratio: Decimal, up=True) -> float:
    m = (Decimal("1") + ratio) if up else (Decimal("1") - ratio)
    v = Decimal(str(pre)) * m
    return float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _day_flags(rows, d_ymd):
    """rows: (ts_code, pre_close, close, high) → {ts: (zt, dt, zb)}"""
    out = {}
    for ts, pre, close, high in rows:
        if not pre or pre <= 0 or close is None:
            continue
        r = limit_ratio(ts, d_ymd)
        up_p = limit_price(pre, r, True)
        dn_p = limit_price(pre, r, False)
        zt = abs(close - up_p) < _EPS
        dt = abs(close - dn_p) < _EPS
        # 炸板=触及涨停且收盘未封; close超涨停价(新股首日无限制/异常)不算
        zb = (high is not None and high >= up_p - _EPS
              and close <= up_p + _EPS) and not zt
        out[ts] = (zt, dt, zb)
    return out


def build(start="20050101") -> bool:
    """本地raw全量估算 → sw_pool(est) 三层级 + mood_raw 2005-2012缺失日填充"""
    sent = connect()
    sent.execute("""CREATE TABLE IF NOT EXISTS sw_pool(
      level TEXT, index_code TEXT, industry_name TEXT, date TEXT,
      zt_count INTEGER, dt_count INTEGER, zb_count INTEGER,
      lb_max INTEGER, lb2_count INTEGER, source TEXT,
      PRIMARY KEY(level, index_code, date))""")
    meta = {ic: (lv, nm) for ic, lv, nm in sent.execute(
        "SELECT index_code, level, industry_name FROM sw_components "
        "GROUP BY index_code, level, industry_name")}
    have_mood = {r[0].replace("-", "") for r in sent.execute(
        "SELECT date FROM mood_raw WHERE zt_count IS NOT NULL")}
    raw = raw_connect()
    days = [r[0] for r in raw.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date>=? ORDER BY trade_date",
        (str(start).replace("-", ""),))]
    print(f"[pool] 估算 {len(days)} 日 ({start} 起, 本地raw)")
    t0 = time.time()
    prev_streak, prev_zt_n = {}, 0
    for i, d in enumerate(days, 1):
        rows = raw.execute(
            "SELECT ts_code, pre_close, close, high FROM daily WHERE trade_date=?", (d,)).fetchall()
        flags = _day_flags(rows, d)
        streak = {ts: (prev_streak.get(ts, 0) + 1 if f[0] else 0)
                  for ts, f in flags.items()}
        # 全市场聚合 → mood_raw(仅缺失日)
        zt_n = sum(1 for f in flags.values() if f[0])
        dt_n = sum(1 for f in flags.values() if f[1])
        zb_n = sum(1 for f in flags.values() if f[2])
        lb2 = sum(1 for ts, s in streak.items() if s >= 2 and flags[ts][0])
        lb_max = max((s for ts, s in streak.items() if flags[ts][0]), default=0)
        if d not in have_mood:
            d_iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            upsert_mood(sent, d_iso,
                        zt_count=zt_n, dt_count=dt_n, zb_count=zb_n,
                        zb_rate=(zb_n / (zt_n + zb_n) * 100) if (zt_n + zb_n) else None,
                        lb_max=lb_max, lb2_count=lb2,
                        jl_rate=(lb2 / prev_zt_n * 100) if prev_zt_n else None,
                        yzt_count=prev_zt_n, zt_source="est2")
        prev_streak, prev_zt_n = streak, zt_n
        # 行业聚合 → sw_pool(est)
        comps = members_on(d, conn=sent)
        inv = {}
        for idx, ms in comps.items():
            for m in ms:
                inv.setdefault(m, []).append(idx)
        agg = {}
        for ts, (zt, dt, zb) in flags.items():
            for idx in inv.get(ts, ()):
                a = agg.setdefault(idx, [0, 0, 0, 0, 0])   # zt,dt,zb,lb_max,lb2
                if zt:
                    a[0] += 1
                    s = streak[ts]
                    a[3] = max(a[3], s)
                    if s >= 2:
                        a[4] += 1
                if dt:
                    a[1] += 1
                if zb:
                    a[2] += 1
        d_iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        sent.executemany(
            "INSERT OR REPLACE INTO sw_pool VALUES(?,?,?,?,?,?,?,?,?, 'est')",
            [(meta[idx][0], idx, meta[idx][1], d_iso, a[0], a[1], a[2], a[3], a[4])
             for idx, a in agg.items() if idx in meta])
        if i % 100 == 0:
            sent.commit()
            print(f"  [{i}/{len(days)}] {time.time()-t0:.0f}s")
    sent.commit()
    raw.close()
    sent.close()
    print(f"[pool] 估算完成 {time.time()-t0:.0f}s")
    return True


def overlay(start=OFFICIAL_START) -> bool:
    """2020+ limit_list_d 官方覆盖 sw_pool(幂等: 已official日跳过)"""
    sent = connect()
    raw = raw_connect()
    days = [r[0] for r in raw.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date>=? ORDER BY trade_date",
        (str(start).replace("-", ""),))]
    done = {r[0].replace("-", "") for r in sent.execute(
        "SELECT DISTINCT date FROM sw_pool WHERE source='official'")}
    todo = [d for d in days if d not in done]
    print(f"[overlay] {len(todo)}/{len(days)} 日官方聚合")
    if not todo:
        sent.close(); raw.close()
        return True
    meta = {ic: (lv, nm) for ic, lv, nm in sent.execute(
        "SELECT index_code, level, industry_name FROM sw_components "
        "GROUP BY index_code, level, industry_name")}
    t0, err_streak = time.time(), 0
    for i, d in enumerate(todo, 1):
        try:
            df = T.limit_list_d(d)
            err_streak = 0
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {d}: {e}")
            err_streak += 1
            if err_streak >= 5:
                print("  ✗✗ 连续5次异常, 中断")
                sent.close(); raw.close()
                return False
            time.sleep(2)
            continue
        if df is None or df.empty:
            continue
        u = df[df["limit"] == "U"]
        dn = df[df["limit"] == "D"]
        z = df[df["limit"] == "Z"]
        comps = members_on(d, conn=sent)
        inv = {}
        for idx, ms in comps.items():
            for m in ms:
                inv.setdefault(m, []).append(idx)
        agg = {}
        for ts, lt in zip(u["ts_code"], u.get("limit_times", [])):
            for idx in inv.get(ts, ()):
                a = agg.setdefault(idx, [0, 0, 0, 0, 0])
                a[0] += 1
                try:
                    s = int(float(lt))
                except (TypeError, ValueError):
                    s = 1
                a[3] = max(a[3], s)
                if s >= 2:
                    a[4] += 1
        for ts in dn["ts_code"]:
            for idx in inv.get(ts, ()):
                agg.setdefault(idx, [0, 0, 0, 0, 0])[1] += 1
        for ts in z["ts_code"]:
            for idx in inv.get(ts, ()):
                agg.setdefault(idx, [0, 0, 0, 0, 0])[2] += 1
        d_iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        sent.executemany(
            "INSERT OR REPLACE INTO sw_pool VALUES(?,?,?,?,?,?,?,?,?, 'official')",
            [(meta[idx][0], idx, meta[idx][1], d_iso, a[0], a[1], a[2], a[3], a[4])
             for idx, a in agg.items() if idx in meta])
        if i % 50 == 0:
            sent.commit()
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s")
    sent.commit()
    raw.close()
    sent.close()
    print(f"[overlay] 完成 {time.time()-t0:.0f}s")
    return True


def run(start="20050101") -> bool:
    return build(start) and overlay()


def validate(days=30):
    """自校验: 2020+重叠段, 本地估算 vs mood_raw官方涨停家数一致率"""
    sent = connect()
    raw = raw_connect()
    dates = [r[0] for r in sent.execute(
        "SELECT date FROM mood_raw WHERE zt_count IS NOT NULL AND zt_source!='est2' "
        "ORDER BY date DESC LIMIT ?", (days,))]
    hits = total = 0
    devs = []
    for d in reversed(dates):
        d_ymd = d.replace("-", "")
        rows = raw.execute("SELECT ts_code, pre_close, close, high FROM daily "
                           "WHERE trade_date=?", (d_ymd,)).fetchall()
        est = sum(1 for f in _day_flags(rows, d_ymd).values() if f[0])
        off = sent.execute("SELECT zt_count FROM mood_raw WHERE date=?", (d,)).fetchone()[0]
        total += 1
        devs.append(abs(est - off))
        if abs(est - off) <= max(2, off * 0.05):
            hits += 1
    raw.close()
    sent.close()
    if total:
        print(f"[validate] {total} 日: 一致率(±5%或±2家) {hits}/{total} = {hits/total*100:.0f}%")
        print(f"  涨停家数平均绝对差 {sum(devs)/len(devs):.1f} 家, 最大 {max(devs)} 家")


def check():
    sent = connect()
    for lv in ("L1", "L2", "L3"):
        r = sent.execute("SELECT COUNT(*), COUNT(DISTINCT date) FROM sw_pool WHERE level=?",
                         (lv,)).fetchone()
        if r[0]:
            print(f"  {lv}: {r[0]} 行 / {r[1]} 日")
    r = sent.execute("SELECT source, COUNT(*), COUNT(DISTINCT date) FROM sw_pool "
                     "GROUP BY source").fetchall()
    for row in r:
        print(f"  source={row[0]}: {row[1]} 行 / {row[2]} 日")
    n = sent.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM mood_raw "
                     "WHERE zt_source='est2'").fetchone()
    print(f"  mood_raw est2 段: {n[0]} 日 ({n[1]} ~ {n[2]})")
    sent.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "overlay", "run", "validate", "check"])
    ap.add_argument("--start", default="20050101")
    args = ap.parse_args()
    if args.cmd == "build":
        sys.exit(0 if build(args.start) else 1)
    if args.cmd == "overlay":
        sys.exit(0 if overlay(args.start) else 1)
    if args.cmd == "run":
        sys.exit(0 if run(args.start) else 1)
    if args.cmd == "validate":
        validate()
    else:
        check()


if __name__ == "__main__":
    main()
