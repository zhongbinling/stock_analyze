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
# 不属于国家队口径、但跟着一起抓份额的ETF(MI-1: 深证成指页签的"挂钩ETF净流入"副图)。
# 国家队净申购序列只认 ETF_CODES 那 8 只 — 新增的不能混进去
ETF_EXTRA_CODES = ("159903.SZ",)
ETF_ALL = ETF_CODES + ETF_EXTRA_CODES


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
    """日增: fund_share近N日×(8只国家队ETF+深成指ETF)(每只1次调用)"""
    import datetime as dt
    if not T.has_token():
        return -1
    start = (dt.date.today() - dt.timedelta(days=lookback)).strftime("%Y%m%d")
    end = time.strftime("%Y%m%d")
    con = ensure_db(path)
    total = 0
    for code in ETF_ALL:
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
    """历史回补: fund_share按年分块×(8只国家队ETF+深成指ETF)(接口支持全史, 实测2018整年247行)。
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
    for code in ETF_ALL:
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


def net_flow_series(path=None, n_days: int = 120, codes=ETF_CODES) -> list:
    """净流入/流出序列: 每日ETF份额差×收盘价 → 亿, [{date, net_yi, flows:{code:yi}}]

    D1-38 review: 每只ETF各用自己的"截至当天已知的最新份额"与前一个交易日相减。
    原按"上一条有份额记录的日期"整体配对 — 份额表里有非交易日的行(季末报告日、个别
    周六), 且往往只有一两只ETF: 它成了下一个交易日的比较基准, 其余ETF当天的申赎
    被整段丢掉(实测 2026-01-19 显示-24.8亿, 8只合计实为净赎回379亿)。生产数据上
    124个交易日的净额因此不对, 累计线虚高约200亿。没有这类行的日子结果与原来相同。

    codes: 算哪几只ETF, 默认国家队口径的 8 只(回测与情绪图的"ETF净流入"用的就是它, 别改默认值)。
    情绪图的指数页签传单只, 如 ("159903.SZ",), 取"挂钩ETF净流入"。"""
    codes = tuple(codes) or ETF_CODES
    con = ensure_db(path)
    # 只取指定的这几只: 表里还有别的ETF时, 按 LIMIT n×只数 截取的窗口会被它们挤短, 序列起点随之上移
    marks = ",".join("?" * len(codes))
    rows = con.execute(
        f"SELECT trade_date, code, fd_share FROM etf_share WHERE code IN ({marks}) "
        "ORDER BY trade_date DESC, code LIMIT ?", (*codes, n_days * len(codes))).fetchall()
    if not rows:
        con.close()
        return []
    # 按日分组(日期归一ISO: etf_share存YYYYMMDD, etf_daily存YYYY-MM-DD)
    by_date = {}
    for d, c, s in rows:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        by_date.setdefault(iso, {})[c] = s
    dates = sorted(by_date.keys())
    # 需要etf_daily收盘价: 一次取齐(D1-38: 原每个交易日查一次, 5000个交易日要近3秒)
    prices_by_date = {}
    marks = ",".join("?" * len(codes))
    for d, c, close in con.execute(
            f"SELECT date, code, close FROM etf_daily WHERE code IN ({marks}) "
            "AND date>=? AND date<=?", (*codes, dates[0], dates[-1])):
        prices_by_date.setdefault(d, {})[c] = close
    out = []
    asof = {}                      # 各ETF截至当天已知的最新份额
    booked = None                  # 各ETF上一次入账时的份额(比较基准)
    pending = set()                # 有了新份额行、还没按收盘价入账的ETF
    for d in dates:
        asof.update(by_date[d])
        if booked is None:         # 窗口首日: 只作比较基准
            booked = dict(asof)
            continue
        pending.update(by_date[d])
        prices = prices_by_date.get(d)
        if not prices:
            continue               # 当日没有收盘价: 份额变化留到该ETF下一个有收盘价的日子
        net_yi, flows = 0.0, {}
        for c in codes:
            if c not in pending or c not in prices:
                continue
            pending.discard(c)
            if c in booked:
                # 份额差(万份)×收盘价 = 金额(万元) → 亿=÷1e4
                flow = (asof[c] - booked[c]) * prices[c] / 1e4
                flows[c] = round(flow, 2)
                net_yi += flow
            booked[c] = asof[c]
        if flows:
            out.append({"date": d, "net_yi": round(net_yi, 2), "flows": flows})
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
