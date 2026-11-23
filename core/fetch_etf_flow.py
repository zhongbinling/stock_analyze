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


_REF_CODE = ETF_CODES[0]          # 510050.SH: 最早上市, 作各年"应有行数"的参照
_FULL_YEAR_ROWS = 200             # 完整历史年份的行数下限(一年约243个交易日)


def _complete_years(con, this_year: str) -> set:
    """已完整的(代码, 年份)集合。当年不判(每次都要重拉取新数据)。
    以参照ETF(510050, 最早上市)各年行数为"应有行数", 且该年参照自身达到
    完整年份下限(_FULL_YEAR_ROWS)时才可信 — 参照只有部分数据时(如先跑过
    半年回补), 其他ETF同年"行数相等"只是同样残缺:
      - 参照ETF自身: 该年行数≥下限
      - 非参照ETF: 行数与参照相等(上市当年/源端缺日的年份不等 → 每次照常重拉)
    D1-33: 回补重跑原样重拉全部年块(2006起 = 8只×21年×6s控频≈18分钟
    队列占用), 而历史年份数据不会变化。"""
    cnt = {(c, y): n for c, y, n in con.execute(
        "SELECT code, substr(trade_date,1,4), COUNT(*) FROM etf_share GROUP BY 1, 2")}
    out = set()
    for (c, y), n in cnt.items():
        if y >= this_year:
            continue
        ref_n = cnt.get((_REF_CODE, y), 0)
        if ref_n >= _FULL_YEAR_ROWS and (c == _REF_CODE or ref_n == n):
            out.add((c, y))
    return out


def backfill(start: str = "20210101", path=None, pace_s: float = 6.0) -> int:
    """历史回补: fund_share按年分块×8只ETF(接口支持全史, 实测2018整年247行)。
    逐年窗口+间隔控频(限频20/min层内); upsert幂等 — 中断/限频失败重跑只补缺块;
    已完整的历史年块跳过不调用(_complete_years)。
    价格侧etf_daily已有2006起全史, 份额补齐即得净流入长史。"""
    import datetime as dt
    if not T.has_token():
        return -1
    beg = dt.datetime.strptime(start, "%Y%m%d").date()
    today = dt.date.today()
    chunks = []
    lo = beg
    while lo <= today:
        hi = min(dt.date(lo.year, 12, 31), today)
        chunks.append((lo.strftime("%Y%m%d"), hi.strftime("%Y%m%d")))
        lo = dt.date(lo.year + 1, 1, 1)
    con = ensure_db(path)
    done = _complete_years(con, str(today.year))
    total, fails, skipped = 0, 0, 0
    for code in ETF_CODES:
        for s, e in chunks:
            if (code, s[:4]) in done:
                skipped += 1
                continue
            try:
                df = T.call("fund_share", "ts_code,trade_date,fd_share",
                            ts_code=code, start_date=s, end_date=e)
                rows = [(code, str(r["trade_date"]), float(r["fd_share"]))
                        for _, r in df.iterrows()]
                if rows:
                    con.executemany(
                        "INSERT INTO etf_share(code,trade_date,fd_share) VALUES(?,?,?) "
                        "ON CONFLICT(code,trade_date) DO UPDATE SET "
                        "fd_share=excluded.fd_share", rows)
                    con.commit()
                    total += len(rows)
            except Exception as ex:  # noqa: BLE001   # 限频/网络: 跳过该块, 重跑再补
                fails += 1
                print(f"  ✗ {code} {s}~{e}: {str(ex)[:60]}")
            time.sleep(pace_s)
    con.close()
    print(f"[etf_flow] 回补: +{total}行({fails}块失败, 重跑可续"
          + (f"; 跳过{skipped}个已完整年块" if skipped else "") + ")")
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
    ap.add_argument("--backfill", metavar="START", nargs="?", const="20210101",
                    help="历史回补(默认2021起, 可给更早YYYYMMDD)")
    a = ap.parse_args()
    if a.refresh:
        refresh()
    elif a.backfill:
        backfill(a.backfill)
    else:
        import json
        s = net_flow_series()
        print(json.dumps(s[-5:], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
