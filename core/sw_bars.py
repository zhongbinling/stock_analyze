# -*- coding: utf-8 -*-
"""SW行业指数日线 (D1-6 P4-B1): tushare sw_daily → sentiment.sw_daily

实测(2026-10-20): 参数是 ts_code(传index_code被忽略→返回全部~440指数);
官方回溯仅至~2012-08(SW2021口径); 单指数全历史一次调用(~3450行<4000上限)。

用法: python sw_bars.py backfill|check [--start 19990101]
"""
import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import tushare_api as T  # noqa: E402
from db import connect  # noqa: E402


def _iso(d):
    s = str(d)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def backfill(start="19990101") -> bool:
    """逐指数一次全历史调用(ts_code参数!), 幂等: 最新≥行情最新-5日的指数跳过"""
    sent = connect()
    sent.execute("""CREATE TABLE IF NOT EXISTS sw_daily(
      index_code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
      volume REAL, amount REAL, pct_chg REAL,
      PRIMARY KEY(index_code, date))""")
    sent.execute("CREATE INDEX IF NOT EXISTS idx_swd_date ON sw_daily(date)")
    sent.execute("""CREATE TABLE IF NOT EXISTS sw_daily_probe(
      index_code TEXT PRIMARY KEY, checked_at TEXT)""")   # 已探明无官方数据的指数
    mkt_last = sent.execute(
        "SELECT MAX(date) FROM index_daily WHERE code='1.000001'").fetchone()[0]
    from datetime import datetime, timedelta
    cutoff = ((datetime.strptime(mkt_last, "%Y-%m-%d") - timedelta(days=7))
              .strftime("%Y-%m-%d")) if mkt_last else "0000-00-00"
    idxs = [r[0] for r in sent.execute(
        "SELECT DISTINCT index_code FROM sw_components ORDER BY index_code")]
    todo = []
    for ic in idxs:
        last = sent.execute(
            "SELECT MAX(date) FROM sw_daily WHERE index_code=?", (ic,)).fetchone()[0]
        probed = sent.execute(
            "SELECT 1 FROM sw_daily_probe WHERE index_code=?", (ic,)).fetchone()
        if (not last or last < cutoff) and not (probed and last is None):
            todo.append(ic)                         # 无数据且已探明 → 跳过
    print(f"[sw-bars] {len(todo)}/{len(idxs)} 指数待补 (基准最新 {mkt_last})")
    if not todo:
        sent.close()
        return True
    t0, err_streak, failed = time.time(), 0, 0
    for i, ic in enumerate(todo, 1):
        try:
            df = T.call("sw_daily", "", ts_code=ic,
                        start_date=str(start).replace("-", ""),
                        end_date="20991231")
            err_streak = 0
            if df is not None and not df.empty:
                sent.executemany(
                    "INSERT OR REPLACE INTO sw_daily VALUES(?,?,?,?,?,?,?,?,?)",
                    [(ic, _iso(r["trade_date"]), r["open"], r["high"], r["low"],
                      r["close"], r["vol"], r["amount"], r["pct_change"])
                     for _, r in df.iterrows()])
                sent.commit()
            else:
                failed += 1                     # 该指数无官方数据(早于回溯边界)
                sent.execute("INSERT OR REPLACE INTO sw_daily_probe VALUES(?,date('now'))", (ic,))
                sent.commit()
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {ic}: {e}")
            err_streak += 1
            failed += 1
            if err_streak >= 5:
                print("  ✗✗ 连续5次异常(多为网络), 中断")
                sent.close()
                return False
            time.sleep(2)
        if i % 25 == 0:
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s")
    n, nd = sent.execute("SELECT COUNT(*), COUNT(DISTINCT index_code) FROM sw_daily").fetchone()
    sent.close()
    print(f"[sw-bars] 结束 {time.time()-t0:.0f}s: {n} 行 / {nd} 指数"
          + (f", 无数据 {failed} 指数" if failed else ""))
    return True


def check():
    sent = connect()
    try:
        n, ni, lo, hi = sent.execute(
            "SELECT COUNT(*), COUNT(DISTINCT index_code), MIN(date), MAX(date) FROM sw_daily").fetchone()
        print(f"[sw-bars] sw_daily: {n} 行 / {ni} 指数 ({lo} ~ {hi})")
    except Exception:
        print("[sw-bars] sw_daily 未建")
    sent.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "check"])
    ap.add_argument("--start", default="19990101")
    args = ap.parse_args()
    if args.cmd == "backfill":
        sys.exit(0 if backfill(args.start) else 1)
    check()


if __name__ == "__main__":
    main()
