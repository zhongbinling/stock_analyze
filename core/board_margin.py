# -*- coding: utf-8 -*-
"""板块两融余额(MI-4): 创业板 / 科创板 每个交易日的融资余额、融券余额、两融余额、融资买入额与标的只数
— 情绪图创业板指 / 科创50 页签的两融副图。

tushare margin_detail 按交易日返回全市场两融标的的明细(股票 + ETF, 2026-11 约 4500 行 / 日)。这里只取
代码前缀属于该板块的个股(创业板 30x、科创板 68x, 与 boards.BOARDS 同口径), 逐日求和。
- 余额只覆盖"两融标的", 不是板块里的全部股票: 标的名单扩大时余额会跳升, 所以把当日的标的只数一并存下;
- 最早的日期: 创业板 2013-01-31(首批 6 只成为标的)、科创板 2019-07-22(开板首日 25 只); 之前的日子没有行。
- 不写半截数据: 某天源端只回了一个交易所的行时(上交所发布晚、接口缺行)这一天整批不写、留到下次再取 —
  体检按"这一天有没有行"判断, 只写一半的话另一半的洞就看不出。接口对某一天返回空表不算失败(两融 T+1 才发布)。

表 board_margin(board, date, rzye, rqye, rzrqye, rzmre, n): 每板块每交易日一行, 金额单位元(同 margin_daily)。
写库前自检: 各项非负、两融余额 ≈ 融资 + 融券、标的只数 ≥ 1、板块合计不超过所在交易所当日的两融余额
(margin_daily)。明细合计本来就略低于交易所汇总(实测低 0.5% ~ 8%), 所以这道校验只能拦"单位错 / 取错板块"
这类大错。

单次取数的行数: 接口按 limit / offset 分页, 服务端单次上限的确切值未知(2026-11-26 一页返回过 4492 行)。
页长取 4000 — 低于已经见过的单页行数, "不足一页就是最后一页"才成立: 一天不到 4000 行时只调一次,
更多时自动翻页(2024 年起约 4500 行 / 日, 两次)。

用法: python board_margin.py [--start 20130131]     # 自 start 起补缺的交易日; 日增由 fetch_daily 挂载
"""
import argparse
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd  # noqa: E402

import tushare_api as T  # noqa: E402
from db import connect, guarded_batch, trading_dates  # noqa: E402
from fetch_daily import load_cfg, norm_date, ymd  # noqa: E402

TABLE = "board_margin"
SCHEMA = f"""CREATE TABLE IF NOT EXISTS {TABLE}(
  board TEXT NOT NULL, date TEXT NOT NULL,
  rzye REAL, rqye REAL, rzrqye REAL, rzmre REAL, n INTEGER,
  PRIMARY KEY(board, date))"""
UPSERT = (f"INSERT INTO {TABLE}(board,date,rzye,rqye,rzrqye,rzmre,n) VALUES(?,?,?,?,?,?,?) "
          f"ON CONFLICT(board,date) DO UPDATE SET rzye=excluded.rzye,rqye=excluded.rqye,"
          "rzrqye=excluded.rzrqye,rzmre=excluded.rzmre,n=excluded.n")

BOARDS = {"CYB": ("30",), "KCB": ("68",)}          # 板块 → 个股代码前缀(同 boards.BOARDS)
FIRST = {"CYB": "20130131", "KCB": "20190722"}     # 各板块首次有两融标的的交易日(2026-11-27 实测)
EXCHANGE = {"CYB": "szse_rzrqye", "KCB": "sse_rzrqye"}    # 板块所在交易所的两融余额(margin_daily 的列)
API, FIELDS = "margin_detail", "trade_date,ts_code,rzye,rqye,rzrqye,rzmre"
PAGE = 4000            # 单次请求的行数(见模块说明)
MAX_ROWS = 60000       # 一天的行数上限: 接口异常(翻页永远是满页)时不至于无限翻下去
LOOKBACK_DAYS = 10     # 日增回看的自然日
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def aggregate(df) -> dict:
    """一个交易日的全市场明细 → {板块: (rzye, rqye, rzrqye, rzmre, n)}; 当天没有个股的板块不出现。
    n = 至少一项余额有值的个股数; 源端 rzrqye 为空的那一行用 rzye + rqye 补。"""
    out = {}
    if df is None or len(df) == 0:
        return out
    ts = df["ts_code"].astype(str)
    cols = {c: pd.to_numeric(df[c], errors="coerce") for c in ("rzye", "rqye", "rzrqye", "rzmre")}
    for board, prefixes in BOARDS.items():
        sub = {c: v[ts.str.startswith(prefixes)] for c, v in cols.items()}
        has = sub["rzye"].notna() | sub["rqye"].notna() | sub["rzrqye"].notna()
        if not bool(has.any()):
            continue
        sub = {c: v[has] for c, v in sub.items()}      # 三项余额都空的个股: 整行不算(连融资买入额也不加)
        rz, rq = sub["rzye"].fillna(0.0), sub["rqye"].fillna(0.0)
        tot = sub["rzrqye"].fillna(rz + rq)
        out[board] = (float(rz.sum()), float(rq.sum()), float(tot.sum()),
                      float(sub["rzmre"].fillna(0.0).sum()), int(len(rz)))
    return out


def fetch_day(d_ymd: str) -> pd.DataFrame:
    """一个交易日的全市场两融明细(翻页取全, 接口原始列)。失败抛异常, 由调用方按日容错。
    守卫: 返回的行必须都是这一天、个股不重复(翻页错位时会出现, 重复会被加两遍)。"""
    frames, offset = [], 0
    while True:
        df = T.call(API, FIELDS, trade_date=d_ymd, limit=PAGE, offset=offset)
        frames.append(df)
        if len(df) < PAGE:
            break
        offset += len(df)
        if offset >= MAX_ROWS:
            break
    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=FIELDS.split(","))
    day = df["trade_date"].astype(str).str.replace("-", "").str[:8]
    if bool((day != d_ymd).any()):
        raise RuntimeError(f"返回了别的交易日的行: {sorted(set(day) - {d_ymd})[:3]}")
    dup = int(df["ts_code"].astype(str).duplicated().sum())
    if dup:
        raise RuntimeError(f"个股重复 {dup} 行(翻页错位)")
    return df


def check_rows(rows, conn=None) -> str:
    """写库前的自检, 返回拒写原因(None = 通过)。给了 conn 时再比所在交易所当日的两融余额。"""
    for b, d, rzye, rqye, rzrqye, rzmre, n in rows:
        if b not in BOARDS:
            return f"未知板块 {b!r}"
        if not _ISO.match(str(d or "")):
            return f"{b} 日期格式不对 {d!r}"
        if min(rzye, rqye, rzrqye, rzmre) < 0:
            return f"{b} {d}: 余额为负 rzye={rzye!r} rqye={rqye!r} rzrqye={rzrqye!r} rzmre={rzmre!r}"
        # 源端把两融余额四舍五入到千位 / 万元, 逐股求和后与 融资 + 融券 有零头
        if abs(rzrqye - rzye - rqye) > max(1.0, 0.002 * rzrqye):
            return f"{b} {d}: 两融余额 {rzrqye!r} ≠ 融资 {rzye!r} + 融券 {rqye!r}"
        if n < 1:
            return f"{b} {d}: 标的只数 {n!r}"
    if conn is not None:
        for b, d, _rzye, _rqye, rzrqye, _rzmre, _n in rows:
            row = conn.execute(f"SELECT {EXCHANGE[b]} FROM margin_daily WHERE date=?", (d,)).fetchone()
            if row and row[0] and rzrqye > row[0] * 1.05:
                return (f"{b} {d}: 板块两融余额 {rzrqye / 1e8:.0f} 亿超过所在交易所当日的 "
                        f"{row[0] / 1e8:.0f} 亿(margin_daily)")
    return None


def todo_days(conn, start_ymd, end_ymd=None, cfg=None) -> list:
    """[start, end](YYYYMMDD; end 省略 = 不限)里还缺的交易日(YYYYMMDD, 升序)。
    交易日取主力指数的日期(index_daily)。一个板块自它首次有标的的日子起没有这一天的行, 这一天就要取 —
    一次调用同时拿到两个板块的数据, 所以只要有一个板块缺就算。"""
    days = trading_dates(conn, (cfg or load_cfg())["index"]["primary"],
                         start=norm_date(start_ymd), end=norm_date(end_ymd) if end_ymd else None)
    have = {b: set() for b in BOARDS}
    for b, d in conn.execute(f"SELECT board, date FROM {TABLE} WHERE date>=?", (norm_date(start_ymd),)):
        if b in have:
            have[b].add(d)
    out = []
    for d in days:
        d8 = ymd(d)
        if any(d8 >= FIRST[b] and d not in have[b] for b in BOARDS):
            out.append(d8)
    return out


def write_day(conn, d_ymd, df) -> int:
    """一天的明细 → 自检 → 写库; 返回写入行数(源端这一天还没有数据: 0)。自检不过抛 RuntimeError。
    不写半截数据: 某个板块自它首次有标的的日子起, 每个交易日都应该有行。源端只回了一个交易所的行时
    (上交所发布晚、接口缺行), 这一天整批不写、留到下次再取 — 否则缺的那个板块那一行会永久留洞:
    体检按"这一天有没有行"判断, 另一个板块有行就看不出来。"""
    agg = aggregate(df)
    d8 = str(d_ymd).replace("-", "")
    lack = [b for b in BOARDS if d8 >= FIRST[b] and b not in agg]
    if agg and lack:
        raise RuntimeError(f"拒写 {d_ymd}: 源端只回了 {'、'.join(sorted(agg))} 的行, 缺 {'、'.join(lack)}(不写半截数据)")
    rows = [(b, norm_date(d_ymd)) + a for b, a in sorted(agg.items())]
    if not rows:
        return 0
    why = check_rows(rows, conn)
    if why:
        raise RuntimeError(f"拒写 {d_ymd}: {why}")
    return guarded_batch(conn, UPSERT, rows, label=f"board_margin {d_ymd}")


def run(start=None, end_ymd=None, conn=None, cfg=None) -> bool:
    """取 [start, end] 里缺的交易日并写入(start 省略 = 创业板首次有标的那天; end 省略 = 不限)。
    返回 True = 要取的都取到了(或者本来就没有缺的); 中途失败(连续 5 个交易日取不到)返回 False, 重跑从缺的续上。"""
    start = str(start).replace("-", "") if start else FIRST["CYB"]
    own = conn is None
    conn = connect() if own else conn
    try:
        conn.execute(SCHEMA)
        conn.commit()
        todo = todo_days(conn, start, end_ymd, cfg)
        if not todo:
            return True
        print(f"[board-margin] 待取 {len(todo)} 个交易日 ({todo[0]} ~ {todo[-1]})")
        t0, fails, streak, written = time.time(), 0, 0, 0
        for i, d in enumerate(todo, 1):
            try:
                written += write_day(conn, d, fetch_day(d))
                streak = 0
            except Exception as e:  # noqa: BLE001   网络 / 限频 / 自检拒写: 这一天留到下次
                fails += 1
                streak += 1
                print(f"  ✗ {d}: {type(e).__name__}: {str(e)[:140]}")
                if streak >= 5:
                    print("  ✗✗ 连续 5 个交易日失败(多为网络 / 限频), 中断; 重跑从缺的续上")
                    break
            if i % 100 == 0 or i == len(todo):
                print(f"  [{i}/{len(todo)}] {time.time() - t0:.0f}s, 写入 {written} 行")
        print(f"[board-margin] 结束 {time.time() - t0:.0f}s, 写入 {written} 行"
              + (f", 失败 {fails} 个交易日(重跑自愈)" if fails else ""))
        # 接口对某一天返回空表不算失败(两融 T+1 才发布, 日增时"昨天还没出"是常态), 所以返回值只看有没有报错;
        # 但要把"取过之后还缺的日子"写进日志, 手动补全时看得出哪些天其实没补上
        left = todo_days(conn, start, end_ymd, cfg)
        if left:
            print(f"[board-margin] 取过之后仍缺 {len(left)} 个交易日(源端还没有这一天的数据, 或见上面的失败原因): "
                  f"{', '.join(left[:6])}{' …' if len(left) > 6 else ''}")
        return fails == 0
    finally:
        if own:
            conn.close()


def refresh(lookback_days: int = LOOKBACK_DAYS, conn=None) -> bool:
    """日增(更新链): 近 N 个自然日里缺的交易日各取一次。两融余额 T+1 才发布, 今天的不取。
    没有 token 时跳过(返回 True, 不影响主流程)。"""
    if not T.has_token():
        print("[board-margin] 无 tushare token, 跳过")
        return True
    yesterday = date.today() - timedelta(days=1)
    start = (yesterday - timedelta(days=lookback_days - 1)).strftime("%Y%m%d")
    own = conn is None
    conn = connect() if own else conn
    try:
        conn.execute(SCHEMA)
        conn.commit()
        todo = todo_days(conn, start, ymd(yesterday))
        if not todo:
            print(f"[board-margin] 近 {lookback_days} 日无缺失")
            return True
    finally:
        if own:
            conn.close()
    return run(start, ymd(yesterday), None if own else conn)


def backfill(start=None) -> bool:
    """补全通道入口(数据管理页的[补全]): 自 start 起缺的交易日全部取回。幂等, 中断后重跑续传。
    首次全量约 3400 个交易日(2013-01 至今), 每次 1 ~ 2 次调用、约 1 秒。"""
    if not T.has_token():
        print("[board-margin] 无 tushare token, 跳过")
        return True
    return run(start)


def main():
    ap = argparse.ArgumentParser(description="板块两融余额(创业板 / 科创板)")
    ap.add_argument("--start", default=None, help="回补起始日 YYYYMMDD(默认 20130131)")
    ap.add_argument("--end", default=None, help="回补结束日 YYYYMMDD(默认不限)")
    ap.add_argument("--refresh", action="store_true", help="只做日增(近 10 日)")
    a = ap.parse_args()
    if a.refresh:
        ok = refresh()
    else:
        ok = run(a.start, a.end)
    print(f"board_margin: {TABLE} 共 "
          f"{connect().execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0]} 行")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
