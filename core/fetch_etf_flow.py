# -*- coding: utf-8 -*-
"""情绪图C1: 宽基ETF净流入/流出 — fund_share日度份额差×收盘价 = 金额口径"""
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tushare_api as T  # noqa: E402

MACRO_DB = BASE / "data" / "sentiment.db"

ETF_CODES = ("510050.SH", "510300.SH", "510310.SH", "510330.SH",
             "510500.SH", "512100.SH", "588000.SH", "159915.SZ")


def ensure_db(path=None):
    p = path or MACRO_DB
    con = sqlite3.connect(str(p), timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS etf_share(
      code TEXT, trade_date TEXT, fd_share REAL,
      PRIMARY KEY(code, trade_date))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_etf_share_date ON etf_share(trade_date)")
    con.commit()
    return con


def refresh(path=None, lookback: int = 30) -> int:
    """日增: fund_share近N日×8只ETF(每只1次调用)"""
    import datetime as dt
    if not T.has_token():
        return -1
    start = (dt.date.today() - dt.timedelta(days=lookback)).strftime("%Y%m%d")
    end = time.strftime("%Y%m%d")
    con = ensure_db(path)
    total = 0
    for code in ETF_CODES:
        try:
            df = T.call("fund_share", "ts_code,trade_date,fd_share",
                        ts_code=code, start_date=start, end_date=end)
            rows = [(code, str(r["trade_date"]), float(r["fd_share"]))
                    for _, r in df.iterrows()]
            con.executemany(
                "INSERT INTO etf_share(code,trade_date,fd_share) VALUES(?,?,?) "
                "ON CONFLICT(code,trade_date) DO UPDATE SET "
                "fd_share=excluded.fd_share", rows)
            total += len(rows)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {code}: {str(e)[:50]}")
    con.commit()
    con.close()
    if total:
        print(f"[etf_flow] 份额: +{total}行")
    return total


def net_flow_series(path=None, n_days: int = 120) -> list:
    """净流入/流出序列: 每日8只ETF份额差×收盘价 → 亿, [{date, net_yi, flows:{code:yi}}]"""
    con = ensure_db(path)
    rows = con.execute(
        "SELECT trade_date, code, fd_share FROM etf_share "
        "ORDER BY trade_date DESC, code LIMIT ?", (n_days * 8,)).fetchall()
    if not rows:
        con.close()
        return []
    # 按日分组(日期归一ISO: etf_share存YYYYMMDD, etf_daily存YYYY-MM-DD)
    by_date = {}
    for d, c, s in rows:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        by_date.setdefault(iso, {})[c] = s
    dates = sorted(by_date.keys())
    # 需要etf_daily收盘价
    out = []
    for i in range(1, len(dates)):
        d, dp = dates[i], dates[i - 1]
        cur, prev = by_date[d], by_date[dp]
        prices = {r[0]: r[1] for r in con.execute(
            "SELECT code, close FROM etf_daily WHERE date=?", (d,))}
        if not prices:
            continue
        net_yi, flows = 0.0, {}
        for c in ETF_CODES:
            if c in cur and c in prev and c in prices:
                # 份额差(万份)×收盘价 = 金额(万元) → 亿=÷1e4
                flow = (cur[c] - prev[c]) * prices[c] / 1e4
                flows[c] = round(flow, 2)
                net_yi += flow
        if flows:
            out.append({"date": d, "net_yi": round(net_yi, 2),
                        "flows": flows})
    con.close()
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    if a.refresh:
        refresh()
    else:
        import json
        s = net_flow_series()
        print(json.dumps(s[-5:], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
