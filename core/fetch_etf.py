# -*- coding: utf-8 -*-
"""D1-14 H3: 宽基ETF日线 — tushare fund_daily

表 etf_daily(code,date,close,amount,name,vol); amount单位千元, vol单位手。
白名单8只国家队宽基(50/三只300/500/1000/科创50/创业板)。
按代码查有5000行单次上限 → 分页续拉; 批次走守卫写入。

用法: python fetch_etf.py --start 20050223   # 回补; 日增由更新链挂载
"""
import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from db import (connect, guarded_batch, guarded_day_write,  # noqa: E402
                all_checks, chk_same, chk_unique)
import tushare_api as T  # noqa: E402
from fetch_daily import norm_date, ymd, _shift_ymd  # noqa: E402

# 国家队宽基白名单(汇金增持标的口径)
ETF_INDEXES = {
    "510050.SH": "上证50ETF", "510300.SH": "300ETF华泰柏瑞", "510330.SH": "300ETF华夏",
    "510310.SH": "300ETF易方达", "510500.SH": "500ETF", "512100.SH": "1000ETF",
    "588000.SH": "科创50ETF", "159915.SZ": "创业板ETF",
}
UPSERT = ("INSERT INTO etf_daily(code,date,close,amount,name,vol) VALUES(?,?,?,?,?,?) "
          "ON CONFLICT(code,date) DO UPDATE SET close=excluded.close,"
          "amount=excluded.amount,vol=excluded.vol")
_PAGE_CAP = 4900          # 5000行上限留余量, 达到即翻页续拉


def _ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS etf_daily(
      code TEXT, date TEXT, close REAL, amount REAL, name TEXT, vol REAL,
      PRIMARY KEY(code, date))""")
    conn.commit()


def _rows_of(code, name, df):
    return [(code, norm_date(str(r["trade_date"])),
             float(r["close"]) if r["close"] is not None else None,
             float(r["amount"]) if r["amount"] is not None else None,
             name, float(r["vol"]) if r["vol"] is not None else None)
            for _, r in df.iterrows()]


def _check(code):
    def f(rs):
        for r in rs:
            if r[2] is not None and (r[2] != r[2] or r[2] <= 0):
                return f"close越界 {r[2]!r}"
            if r[3] is not None and (r[3] != r[3] or r[3] < 0):
                return f"amount越界 {r[3]!r}"
        return None
    return all_checks(chk_same(0, code), chk_unique(1), f)


def refresh(conn=None) -> bool:
    """日增: 逐ETF单日小查询 → 当批守卫写入(T-1日数据)"""
    conn = conn or connect()
    _ensure_table(conn)
    today = time.strftime("%Y%m%d")
    rows = []
    for code, name in ETF_INDEXES.items():
        try:
            df = T.call("fund_daily", "trade_date,close,amount,vol",
                        ts_code=code, start_date=today, end_date=today)
            rows += _rows_of(code, name, df)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {code}: {e}")
    if not rows:
        return True                      # 未出数/非交易日
    d = rows[0][1]
    guarded_day_write(conn, UPSERT, rows, d, table="etf_daily",
                      date_col="date", date_idx=1)
    print(f"[etf] 宽基ETF: {len(rows)} 只 {d}")
    return True


def run(start="20050223") -> bool:
    """回补: 按代码分页全史(5000行上限翻页), 每代码一批守卫"""
    if not T.has_token():
        print("[etf] 无 tushare token, 跳过")
        return True
    t0 = time.time()
    conn = connect()
    _ensure_table(conn)
    iso = norm_date(str(start))
    beg = ymd(iso)
    end = time.strftime("%Y%m%d")
    print(f"[etf] 回补 {len(ETF_INDEXES)} 只自 {iso}")
    fails = 0
    for code, name in sorted(ETF_INDEXES.items()):
        try:
            all_rows, cur = [], beg
            while cur <= end:                       # 分页续拉(单次5000行上限)
                df = T.call("fund_daily", "trade_date,close,amount,vol",
                            ts_code=code, start_date=cur, end_date=end)
                if df.empty:
                    break
                all_rows += _rows_of(code, name, df)
                last = str(df["trade_date"].max())
                if len(df) < _PAGE_CAP or last >= end:
                    break
                cur = _shift_ymd(f"{last[:4]}-{last[4:6]}-{last[6:]}", 1)
            if all_rows:
                guarded_batch(conn, UPSERT, all_rows, label=f"etf {code}",
                              check=_check(code))
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ {code}: {e}")
            if fails >= 5:
                print("  ✗✗ 连续失败中断(重跑续传)")
                break
    n = conn.execute("SELECT COUNT(*) FROM etf_daily").fetchone()[0]
    conn.close()
    print(f"[etf] 结束 {time.time()-t0:.0f}s, 共{n}行"
          + (f", 失败{fails}(重跑自愈)" if fails else ""))
    return fails == 0


def main():
    ap = argparse.ArgumentParser(description="宽基ETF日线回补")
    ap.add_argument("--start", default="20050223")
    args = ap.parse_args()
    sys.exit(0 if run(args.start) else 1)


if __name__ == "__main__":
    main()
