# -*- coding: utf-8 -*-
"""个股日线原始库(D1-6 P1): core/data/raw_daily.db 独立库, 全字段落库

- backfill: tushare daily 按 trade_date 逐日全市场调用(单次上限6000行, 按年不可行),
  断点幂等, err_streak≥5 网络快速失败, [i/N]进度
- agg: raw × sw_components(members_on 区间还原) → sentiment.sw_breadth 各层级涨跌家数
- calibrate: 自洽性校验(ΣL1 vs 两市涨跌家数 + 成分覆盖率) — SW与东财体系不同, 无逐板块对齐
- run = backfill + agg(补全通道fn, 返回bool自报成败)

用法: python raw_daily.py backfill|agg|run|check [--start 20050101]
"""
import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: F401,E402  (tushare_api 依赖)
import tushare_api as T  # noqa: E402
from db import connect  # noqa: E402
from sw_components import members_on  # noqa: E402

RAW_DB = BASE / "data" / "raw_daily.db"
_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"


def raw_connect():
    import sqlite3
    RAW_DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(RAW_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS daily(
      ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
      pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL,
      PRIMARY KEY(ts_code, trade_date))""")
    # 聚合按 trade_date 查询(PK前缀是ts_code走不了索引) — 无索引时每日全表扫,
    # 实测聚合1.1s/日; 建索引后毫秒级(1600万行建索引约1-2分钟, 仅首次)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_tradedate ON daily(trade_date)")
    return conn


def _iso(d):
    s = str(d)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


# ---------------------------------------------------------------- 交易日底座

def _ensure_index_daily(start):
    """交易日序列(2005起): sentiment.index_daily 已覆盖段直接用;
    缺失段按 tushare index_daily(000001.SH) 逐年补, 并 upsert 进 index_daily(联动情绪底座)"""
    conn = connect()
    have = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM index_daily WHERE code='1.000001' AND date>=? ORDER BY date",
        (_iso(start),))]
    have_ymd = {d.replace("-", "") for d in have}
    lo = _iso(start)
    if not have or min(have) > lo:
        print(f"[raw] index_daily 自 {min(have) if have else '--'} 补至 {lo}")
        years = range(int(lo[:4]), time.localtime().tm_year + 1)
        for y in years:
            try:
                df = T.call("index_daily", "", ts_code="000001.SH",
                            start_date=f"{y}0101", end_date=f"{y}1231")
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ index_daily {y}: {e} — 用现有交易日继续")
                break
            if df is None or df.empty:
                continue
            rows = [("1.000001", _iso(r["trade_date"]), r["open"], r["high"], r["low"],
                     r["close"], r["vol"], r["amount"], r["pct_chg"])
                    for _, r in df.iterrows()
                    if str(r["trade_date"]).replace("-", "") not in have_ymd]
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO index_daily(code,date,open,high,low,close,volume,amount,pct_chg) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", rows)
                have_ymd |= {r[1] for r in rows}
            conn.commit()
        have = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM index_daily WHERE code='1.000001' AND date>=? ORDER BY date",
            (lo,))]
    conn.close()
    return have


# ---------------------------------------------------------------- raw 回补

def backfill(start="20050101") -> bool:
    conn = raw_connect()
    days_all = _ensure_index_daily(start)     # 内部已做 YMD/ISO 归一
    have = {r[0] for r in conn.execute("SELECT DISTINCT trade_date FROM daily")}
    todo = [d.replace("-", "") for d in days_all if d.replace("-", "") not in have]
    print(f"[raw] {len(todo)}/{len(days_all)} 日待补 ({start} 起)")
    if not todo:
        conn.close()
        return True
    t0, err_streak, failed = time.time(), 0, 0
    for i, d in enumerate(todo, 1):
        ok_day = False
        try:
            df = T.call("daily", _DAILY_FIELDS, trade_date=d)
            err_streak = 0
            if df is not None and not df.empty:
                conn.executemany(
                    "INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [tuple(r) for r in df.itertuples(index=False)])
                conn.commit()
                ok_day = True
            elif df is not None:
                ok_day = True                      # 空数据(非交易日/未出数)不算失败
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {d}: {e}")
            err_streak += 1
            if err_streak >= 5:
                print("  ✗✗ 连续5次异常(多为网络), 中断")
                conn.close()
                return False
            time.sleep(2)
        if not ok_day:
            failed += 1
        if i % 50 == 0:
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s")
    n = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    conn.close()
    print(f"[raw] 结束 {time.time()-t0:.0f}s, 共 {n} 行"
          + (f", 失败 {failed} 日(重跑自愈)" if failed else ""))
    return True


# ---------------------------------------------------------------- SW 聚合

def agg(start="20050101") -> bool:
    """raw × 成分区间的表 → sentiment.sw_breadth(各层级涨跌家数), 幂等"""
    sent = connect()
    sent.execute("""CREATE TABLE IF NOT EXISTS sw_breadth(
      level TEXT, index_code TEXT, industry_name TEXT, date TEXT,
      up INTEGER, down INTEGER, flat INTEGER, total INTEGER,
      PRIMARY KEY(level, index_code, date))""")
    meta = {ic: (lv, nm) for ic, lv, nm in sent.execute(
        "SELECT index_code, level, industry_name FROM sw_components "
        "GROUP BY index_code, level, industry_name")}   # idx→(level, name) 一次建表
    raw = raw_connect()
    days = [r[0] for r in raw.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date>=? ORDER BY trade_date",
        (str(start).replace("-", ""),))]
    done = {r[0] for r in sent.execute("SELECT DISTINCT date FROM sw_breadth")}
    todo = [d for d in days if _iso(d) not in done]
    print(f"[agg] {len(todo)}/{len(days)} 日待聚合")
    if not todo:
        sent.close(); raw.close()
        return True
    t0 = time.time()
    for i, d in enumerate(todo, 1):
        pct = {r[0]: r[1] for r in raw.execute(
            "SELECT ts_code, pct_chg FROM daily WHERE trade_date=? AND pct_chg IS NOT NULL", (d,))}
        comps = members_on(d, conn=sent)
        d_iso = _iso(d)                     # sw_breadth.date 统一ISO(与库内其他表一致)
        rows = []
        for idx, members in comps.items():
            lv, name = meta.get(idx, (None, ""))
            if not lv:
                continue
            up = sum(1 for m in members if (pct.get(m) or 0) > 0)
            down = sum(1 for m in members if (pct.get(m) or 0) < 0)
            flat = sum(1 for m in members if pct.get(m) == 0)
            total = up + down + flat
            if total:
                rows.append((lv, idx, name, d_iso, up, down, flat, total))
        if rows:
            sent.executemany("INSERT OR REPLACE INTO sw_breadth VALUES(?,?,?,?,?,?,?,?)", rows)
        if i % 50 == 0:
            sent.commit()
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s")
    sent.commit()
    raw.close()
    sent.close()
    print(f"[agg] 结束 {time.time()-t0:.0f}s")
    return True


def run(start="20050101") -> bool:
    """补全通道入口: raw回补 → SW聚合"""
    ok1 = backfill(start)
    ok2 = agg(start) if ok1 else False
    return ok1 and ok2


def calibrate(days=30):
    """自洽性校验: 近N日 Σ(L1涨/跌) vs mood_raw 两市涨跌家数; 成分覆盖率"""
    conn = connect()
    sent = connect()
    raw = raw_connect()
    print(f"[calib] 近 {days} 个有数日:")
    devs, covs = [], []
    dates = [r[0] for r in sent.execute(
        "SELECT DISTINCT date FROM sw_breadth WHERE level='L1' ORDER BY date DESC LIMIT ?", (days,))]
    for d in dates:
        row = conn.execute("SELECT up_count, down_count FROM mood_raw WHERE date=?",
                           (_iso(d),)).fetchone()
        if not row or row[0] is None:
            continue
        su = sent.execute("SELECT SUM(up), SUM(down), SUM(total) FROM sw_breadth "
                          "WHERE level='L1' AND date=?", (d,)).fetchone()
        if not su or not su[0]:
            continue
        mkt = row[0] + row[1]
        devs.append(abs((su[0] + su[1]) - mkt) / mkt * 100 if mkt else 0)
        covs.append(su[2] / mkt * 100 if mkt else 0)
    conn.close(); sent.close(); raw.close()
    if devs:
        print(f"  ΣL1(涨+跌) vs 两市: 平均偏差 {sum(devs)/len(devs):.2f}%")
        print(f"  L1 成分覆盖率(Σtotal/两市): 平均 {sum(covs)/len(covs):.1f}%")
    else:
        print("  无可比数据(需 mood_raw 涨跌家数与 sw_breadth 同日)")


def check():
    raw = raw_connect()
    n, lo, hi, nd = raw.execute(
        "SELECT COUNT(*), MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM daily").fetchone()
    print(f"[raw] daily: {n} 行 / {nd} 日 ({lo} ~ {hi}, {RAW_DB.name})")
    raw.close()
    sent = connect()
    for lv in ("L1", "L2", "L3"):
        r = sent.execute("SELECT COUNT(*), COUNT(DISTINCT index_code), COUNT(DISTINCT date) "
                         "FROM sw_breadth WHERE level=?", (lv,)).fetchone()
        if r[0]:
            print(f"[agg] sw_breadth {lv}: {r[0]} 行 / {r[1]} 指数 / {r[2]} 日")
    sent.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "agg", "run", "check", "calibrate"])
    ap.add_argument("--start", default="20050101")
    args = ap.parse_args()
    if args.cmd == "backfill":
        sys.exit(0 if backfill(args.start) else 1)
    if args.cmd == "agg":
        sys.exit(0 if agg(args.start) else 1)
    if args.cmd == "run":
        sys.exit(0 if run(args.start) else 1)
    if args.cmd == "calibrate":
        calibrate()
    else:
        check()


if __name__ == "__main__":
    main()
