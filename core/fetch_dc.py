# -*- coding: utf-8 -*-
"""D1-12: 东财板块数据 tushare 主源(dc系列) — 近端日线/涨跌家数回补

push2his 限流期间的数据恢复路径。dc_daily/dc_index 与东财同源同口径:
实测 close 相对差中位 0.000%(2092对), up_count 496/496 完全一致。
限制: tushare dc 系列仅近端窗口有数据(2024-06前为空),
深度历史仍走东财kline回补(fetch_daily sector_history)。

用法: python fetch_dc.py --start 20261012   # 补"半截/缺失"交易日
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from db import connect, trading_dates, guarded_day_write  # noqa: E402
import tushare_api as T  # noqa: E402
from fetch_daily import load_cfg, norm_date, ymd  # noqa: E402


def _enabled_codes(conn) -> set:
    """启用中的东财行业板块(概念停用不采)"""
    return {r[0] for r in conn.execute(
        "SELECT code FROM sectors WHERE kind='industry' AND enabled=1")}


def _thin_dates(conn, dates, need) -> list:
    """OHLC或涨跌家数覆盖不足90%的交易日 = 需回补日(半截/缺失同判)"""
    out = []
    for d in dates:
        n_ohlc, n_ud = conn.execute(
            "SELECT SUM(close IS NOT NULL), SUM(up_count IS NOT NULL) "
            "FROM sector_daily WHERE date=?", (d,)).fetchone()
        if (n_ohlc or 0) < need * 0.9 or (n_ud or 0) < need * 0.9:
            out.append(d)
    return out


def run(start="20260901") -> bool:
    """幂等回补: 只处理覆盖不足的交易日; 单日两批(日线+涨跌家数)均走守卫写入"""
    if not T.has_token():
        print("[dc] 无 tushare token, 跳过")
        return True
    t0 = time.time()
    conn = connect()
    codes = _enabled_codes(conn)
    if not codes:
        print("[dc] 无启用板块")
        conn.close()
        return True
    iso = norm_date(start)
    dates = trading_dates(conn, load_cfg()["index"]["primary"], start=iso)
    todo = _thin_dates(conn, dates, len(codes))
    print(f"[dc] {len(todo)}/{len(dates)} 交易日待补 (自 {iso}, 启用板块 {len(codes)})")
    if not todo:
        conn.close()
        return True
    ok, fails = 0, 0
    for i, d in enumerate(todo, 1):
        try:
            df = T.call("dc_daily", "ts_code,open,high,low,close,amount",
                        trade_date=ymd(d))
            rows = [(c, d, float(r["open"]), float(r["high"]), float(r["low"]),
                     float(r["close"]), float(r["amount"]))
                    for _, r in df.iterrows()
                    if (c := r["ts_code"].split(".")[0]) in codes]
            guarded_day_write(
                conn,
                "INSERT INTO sector_daily(code,date,open,high,low,close,amount) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET "
                "open=excluded.open,high=excluded.high,low=excluded.low,"
                "close=excluded.close,amount=excluded.amount",
                rows, d, table="sector_daily", date_col="date")
            df2 = T.call("dc_index", "ts_code,up_num,down_num", trade_date=ymd(d))
            rows2 = [(c, d, int(r["up_num"]), int(r["down_num"]))
                     for _, r in df2.iterrows()
                     if (c := r["ts_code"].split(".")[0]) in codes
                     and r["up_num"] is not None]
            guarded_day_write(
                conn,
                "INSERT INTO sector_daily(code,date,up_count,down_count) "
                "VALUES(?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET "
                "up_count=excluded.up_count,down_count=excluded.down_count",
                rows2, d, table="sector_daily", date_col="date")
            ok += 1
            print(f"  [{i}/{len(todo)}] {d}: 日线{len(rows)} 涨跌{len(rows2)}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ {d}: {e}")
            if fails >= 5:
                print("  ✗✗ 连续失败中断(重跑续传)")
                break
    n = conn.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0]
    conn.close()
    print(f"[dc] 结束 {time.time()-t0:.0f}s, 共{n}行"
          + (f", 失败{fails}日(重跑自愈)" if fails else ""))
    return fails == 0


def main():
    ap = argparse.ArgumentParser(description="东财板块数据 tushare(dc) 近端回补")
    ap.add_argument("--start", default="20260901", help="起始日 YYYYMMDD/ISO")
    args = ap.parse_args()
    sys.exit(0 if run(args.start) else 1)


if __name__ == "__main__":
    main()
