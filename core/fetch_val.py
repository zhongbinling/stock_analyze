# -*- coding: utf-8 -*-
"""D1-14 H1: 指数估值(PE/PB) — tushare index_dailybasic

表 index_val_daily(code,date,pe,pb,name); 7指数白名单。
回补=按代码查全史(实测2014-06起, 每代码1次调用); 日增=按trade_date一次拿全指数。
批次走 guarded_batch/guarded_day_write 守卫(值域 pe(0,500)/pb(0,30)容None)。

用法: python fetch_val.py --start 20140601   # 回补; 日增由更新链挂载
"""
import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from db import connect, guarded_batch, guarded_day_write, all_checks, chk_same, chk_unique  # noqa: E402
import tushare_api as T  # noqa: E402
from fetch_daily import norm_date, ymd  # noqa: E402

# 宽基白名单(中证全指不在接口清单, 全A口径以宽基组合呈现——见计划H1执行稿)
VAL_INDEXES = {
    "000016.SH": "上证50", "000300.SH": "沪深300", "000905.SH": "中证500",
    "000852.SH": "中证1000", "000001.SH": "上证指数", "399006.SZ": "创业板指",
    "000688.SH": "科创50",
}
UPSERT = ("INSERT INTO index_val_daily(code,date,pe,pb,name) VALUES(?,?,?,?,?) "
          "ON CONFLICT(code,date) DO UPDATE SET pe=excluded.pe,pb=excluded.pb")


def _ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS index_val_daily(
      code TEXT, date TEXT, pe REAL, pb REAL, name TEXT,
      PRIMARY KEY(code, date))""")
    conn.commit()


def _val_ok(rs):
    for r in rs:
        for v, lo, hi, tag in ((r[2], 0.0, 500.0, "pe"), (r[3], 0.0, 30.0, "pb")):
            if v is None:
                continue
            if v != v or v <= lo or v >= hi:
                return f"{tag}越界 {v!r}(合法 {lo}~{hi})"
    return None


def refresh(conn=None, lookback_days: int = 7) -> bool:
    """日增: 近7自然日一次查询 → 按日分批守卫写入(错过日自动补, 幂等upsert)

    D1-14r: 原只查当日 — 服务某晚未跑则该日永久缺(10-26实证);
    回看窗口与两融/dc同语义。
    """
    conn = conn or connect()
    _ensure_table(conn)
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    by_date = {}
    for i in range(lookback_days + 1):     # 接口限制: 区间查询须带trade_date/ts_code → 逐日
        d_ymd = (today - _td(days=i)).strftime("%Y%m%d")
        try:
            df = T.call("index_dailybasic", "ts_code,pe,pb", trade_date=d_ymd)
        except Exception as e:  # noqa: BLE001
            print(f"[val] 日增失败({d_ymd}, 不影响主流程): {e}")
            return False
        for _, r in df.iterrows():
            c = r["ts_code"]
            if c not in VAL_INDEXES:
                continue
            d_iso = norm_date(d_ymd)
            by_date.setdefault(d_iso, []).append(
                (c, d_iso,
                 float(r["pe"]) if r["pe"] is not None else None,
                 float(r["pb"]) if r["pb"] is not None else None, VAL_INDEXES[c]))
    for d, batch in sorted(by_date.items()):
        guarded_day_write(conn, UPSERT, batch, d, table="index_val_daily",
                          date_col="date", date_idx=1)
    if by_date:
        print(f"[val] 指数估值: {sum(len(b) for b in by_date.values())} 行 "
              f"({','.join(by_date)})")
    return True


def run(start="20140601") -> bool:
    """回补: 按代码查全史(每代码1次调用), 本地过滤start, 每代码一批守卫"""
    if not T.has_token():
        print("[val] 无 tushare token, 跳过")
        return True
    t0 = time.time()
    conn = connect()
    _ensure_table(conn)
    iso = norm_date(start)
    print(f"[val] 回补 {len(VAL_INDEXES)} 指数自 {iso}")
    fails = 0
    for i, (code, name) in enumerate(sorted(VAL_INDEXES.items()), 1):
        try:
            df = T.call("index_dailybasic", "trade_date,pe,pb", ts_code=code)
            rows = [(code, norm_date(str(r["trade_date"])),
                     float(r["pe"]) if r["pe"] is not None else None,
                     float(r["pb"]) if r["pb"] is not None else None, name)
                    for _, r in df.iterrows()
                    if str(r["trade_date"]) >= ymd(iso)]
            if not rows:
                continue
            guarded_batch(conn, UPSERT, rows, label=f"val {code}",
                          check=all_checks(chk_same(0, code), chk_unique(1), _val_ok))
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ {code}: {e}")
            if fails >= 5:
                print("  ✗✗ 连续失败中断(重跑续传)")
                break
    n = conn.execute("SELECT COUNT(*) FROM index_val_daily").fetchone()[0]
    conn.close()
    print(f"[val] 结束 {time.time()-t0:.0f}s, 共{n}行"
          + (f", 失败{fails}(重跑自愈)" if fails else ""))
    return fails == 0


def main():
    ap = argparse.ArgumentParser(description="指数估值PE/PB回补")
    ap.add_argument("--start", default="20140601")
    args = ap.parse_args()
    sys.exit(0 if run(args.start) else 1)


if __name__ == "__main__":
    main()
