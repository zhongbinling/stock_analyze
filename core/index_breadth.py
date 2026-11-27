# -*- coding: utf-8 -*-
"""指数成分股与成分股广度(MI-4): 深证成指 / 创业板指 / 科创50 的成分股(月度快照) → 每个交易日的
"成分股上涨占比""站上 20 日线占比" — 情绪图三个指数页签的副图。只作附图, 不进情绪分的评分模型。

成分股(tushare index_weight): 每个月末一个快照, 记的是当天收盘后各成分股在指数里的权重。只算"截至当天
已经有的最新一份": 交易日 d 用快照日 ≤ d 的最后一份, 不用以后才知道的名单。
- 深证成指 2010-01-29 起(2015-05-29 起 40 只 → 500 只), 创业板指 2010-06-30 起(100 只), 科创50 2020-07-31 起(50 只);
- 接口单次最多 7000 行(深证成指 500 只, 一次只拿到 14 个月), 按年分段取、段内翻页。

广度由本地个股日线库(raw_daily.db, 与 boards / sw_breadth 同一个源)聚合, 不调外部接口。等权, 每只成分股
算一票, 不按指数权重加权 — 权重加权的结果与指数自身的涨跌高度重合, 等权才看得出参与度。
- n: 当天有行情、且昨收有效的成分股数(停牌的不计); up: 收盘高于昨收的只数; 占比 = up ÷ n;
- 站上 20 日线: 收盘高于自己最近 20 个交易日收盘的均值; 占比 = above20 ÷ n20(n20 = 有 20 日线的只数,
  上市不满 20 个交易日的不计)。比的是复权序列: 逐日"收盘 ÷ 昨收"连乘(昨收是除权价), 除权不会把股价
  打成跳空。

表 index_weight(index_code, trade_date, con_code, weight): 月度快照原样落库(index_code 是 tushare 代码,
trade_date 是 ISO 日期)。
表 index_breadth(code, date, n, up, n20, above20, nc): 每指数每交易日一行; code 同 index_daily(config
index.extra 的 code); nc = 当天生效的那份快照里的成分股数。

日增(refresh, 更新链里挂的): 起点 = 近 45 天与"库内最新一份快照所在的那个月"里较早的那个 — 月末快照晚到、
或更新链停摆超过 45 天时, 固定回看 45 天会漏掉中间的月份。补全快照的入口(fetch_run)会顺带重算广度。
数据管理里每个板块 / 每个指数各登记一行资产, 只缺一个指数的日子体检才看得出。

用法: python index_breadth.py fetch|build|refresh [--start 20100101]     # 日增由 fetch_daily 挂载
"""
import argparse
import re
import sys
import time
from bisect import bisect_left
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import tushare_api as T  # noqa: E402
from db import all_checks, chk_same, chk_unique, connect, guarded_batch  # noqa: E402
from fetch_daily import norm_date  # noqa: E402
from raw_daily import raw_connect  # noqa: E402
from score_index import index_specs  # noqa: E402

W_TABLE, B_TABLE = "index_weight", "index_breadth"
W_SCHEMA = f"""CREATE TABLE IF NOT EXISTS {W_TABLE}(
  index_code TEXT NOT NULL, trade_date TEXT NOT NULL, con_code TEXT NOT NULL, weight REAL,
  PRIMARY KEY(index_code, trade_date, con_code))"""
B_SCHEMA = f"""CREATE TABLE IF NOT EXISTS {B_TABLE}(
  code TEXT NOT NULL, date TEXT NOT NULL,
  n INTEGER, up INTEGER, n20 INTEGER, above20 INTEGER, nc INTEGER,
  PRIMARY KEY(code, date))"""
W_COLS = "index_code,con_code,trade_date,weight"
W_UPSERT = (f"INSERT INTO {W_TABLE}(index_code,trade_date,con_code,weight) VALUES(?,?,?,?) "
            f"ON CONFLICT(index_code,trade_date,con_code) DO UPDATE SET weight=excluded.weight")
B_UPSERT = (f"INSERT INTO {B_TABLE}(code,date,n,up,n20,above20,nc) VALUES(?,?,?,?,?,?,?) "
            f"ON CONFLICT(code,date) DO UPDATE SET n=excluded.n,up=excluded.up,n20=excluded.n20,"
            "above20=excluded.above20,nc=excluded.nc")

MA_N = 20              # 均线天数(按个股自己的交易日数: 停牌的日子没有行情, 不占天数)
WARM_DAYS = 90         # 算 20 日线要往前多读的自然日(≥ 20 个交易日)
REFRESH_DAYS = 45      # 日增回看的自然日: 月末快照在次月才发布, 回看一段才接得上新快照
W_FIRST = {"399001.SZ": "20100101", "399006.SZ": "20100601", "000688.SH": "20200701"}   # 各指数最早的快照月
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso(d) -> str:
    s = str(d)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


# ---------------------------------------------------------------- 成分股快照

def year_chunks(start_ymd: str, end_ymd: str) -> list:
    """[start, end](YYYYMMDD) 按自然年切成 [(起, 止)]"""
    out, y = [], int(start_ymd[:4])
    while f"{y}0101" <= end_ymd:
        out.append((max(f"{y}0101", start_ymd), min(f"{y}1231", end_ymd)))
        y += 1
    return out


def latest_month(conn) -> str | None:
    """库里最新一份快照所在的月的第一天(YYYYMMDD); 还没有快照返回 None"""
    row = conn.execute(f"SELECT MAX(trade_date) FROM {W_TABLE}").fetchone()
    return str(row[0]).replace("-", "")[:6] + "01" if row and row[0] else None


def daily_start(conn=None) -> str:
    """日增的起点(YYYYMMDD): 近 REFRESH_DAYS 天, 但不晚于库内最新一份快照所在的那个月 —
    月末快照晚到、或更新链停摆超过 REFRESH_DAYS 天时, 45 天的固定回看会漏掉中间的月份: 之后的日子按过期的名单算,
    而体检只看"这一天有没有行", 看不出来。从最新快照那个月起, 漏掉的月份都会被接上。"""
    back = (date.today() - timedelta(days=REFRESH_DAYS)).strftime("%Y%m%d")
    own = conn is None
    conn = connect() if own else conn
    try:
        conn.execute(W_SCHEMA)
        conn.commit()
        m = latest_month(conn)
    finally:
        if own:
            conn.close()
    return min(back, m) if m else back


def fetch_weights(start=None, end=None, cfg=None, conn=None) -> dict:
    """各指数的月度成分股快照 → index_weight(幂等覆盖)。
    start(YYYYMMDD) 是快照日期的下限, 省略 = 日增的起点(见 daily_start); end 省略 = 今天。
    返回 {tushare 指数代码: 写入行数}; 没有指数配置或没有 token 时返回 {}。"""
    specs = index_specs(cfg)
    if not specs or not T.has_token():
        return {}
    end = str(end).replace("-", "") if end else time.strftime("%Y%m%d")
    start = str(start).replace("-", "") if start else daily_start(conn)
    own = conn is None
    conn = connect() if own else conn
    out = {}
    try:
        conn.execute(W_SCHEMA)
        conn.commit()
        for sp in specs:
            ts = sp["ts_code"]
            beg = max(str(W_FIRST.get(ts) or "19900101"), str(start).replace("-", ""))
            rows = []
            for s, e in year_chunks(beg, end):
                for df in T.pull_frames("index_weight", W_COLS, index_code=ts, start_date=s, end_date=e):
                    for r in (df.to_dict("records") if df is not None else []):
                        d = str(r.get("trade_date") or "").replace("-", "")[:8]
                        if s <= d <= e:                       # 接口若无视起止日期也不收
                            rows.append((ts, _iso(d), str(r["con_code"]), _num(r.get("weight"))))
            rows.sort(key=lambda r: (r[1], r[2]))
            if rows:
                guarded_batch(conn, W_UPSERT, rows, label=f"index_weight {ts}",
                              check=all_checks(chk_same(0, ts), chk_unique(1, 2)))
            out[ts] = len(rows)
    finally:
        if own:
            conn.close()
    return out


def snapshots(conn, ts_code) -> list:
    """某指数库里的快照: [(快照日 YYYYMMDD, [成分股代码])], 升序"""
    out = []
    for d, c in conn.execute(
            f"SELECT trade_date, con_code FROM {W_TABLE} WHERE index_code=? ORDER BY trade_date",
            (ts_code,)).fetchall():
        d8 = str(d).replace("-", "")
        if not out or out[-1][0] != d8:
            out.append((d8, []))
        out[-1][1].append(c)
    return out


# ---------------------------------------------------------------- 广度

def stock_flags(bars) -> pd.DataFrame:
    """一只股票的日线 → 每个交易日的两个 0 / 1 标记(没有行情的日子不出现), trade_date(YYYYMMDD)为索引:
    up = 收盘 > 昨收; above = 复权收盘 > 自己最近 MA_N 个交易日复权收盘的均值(不满 MA_N 天为 NaN; 等于不算站上)。
    bars: trade_date, pre_close, close。昨收 / 收盘无效(空、≤ 0)的行不参与。
    复权序列 = 逐日"收盘 ÷ 昨收"连乘(昨收是除权价), 除权不会把股价打成跳空。
    "收盘 = 均线"并不罕见(股价只有两位小数): 复权序列是连乘出来的, 带 1e-16 量级的浮点误差, 直接比较会把
    "相等"随机判成"略大", 而且从哪一天开始累乘结果都不一样(全量重算与日增量算对不上, 11-27 在生产数据上
    实测 34 天里深证成指有 2 天如此)。所以: 窗口的 MA_N 个收盘之间没有除权(绝大多数日子)时, 按"分"的整数
    精确比较 — MA_N × 今日收盘 > 窗口内收盘之和; 有除权才比复权序列, 并留 1e-9 的相对容差(两位小数的价格,
    不相等时相对差不会小于 1e-8)。"""
    df = pd.DataFrame({"d": bars["trade_date"].astype(str).str.replace("-", "").str[:8],
                       "pre": pd.to_numeric(bars["pre_close"], errors="coerce"),
                       "close": pd.to_numeric(bars["close"], errors="coerce")}).reset_index(drop=True)
    df = df[(df["pre"] > 0) & (df["close"] > 0)].sort_values("d").reset_index(drop=True)
    if df.empty:
        return pd.DataFrame({"up": pd.Series(dtype=float), "above": pd.Series(dtype=float)})
    pre, close = df["pre"].to_numpy(), df["close"].to_numpy()
    adj = pd.Series(close / pre).cumprod()
    ma = adj.rolling(MA_N).mean()
    cents = np.round(close * 100).astype(np.int64)
    total = pd.Series(cents).rolling(MA_N).sum().to_numpy()
    split = np.zeros(len(df), dtype=bool)            # 当日昨收 ≠ 前一个行情日的收盘: 这一天除权了(或数据不一致)
    if len(df) > 1:
        split[1:] = pre[1:] != close[:-1]
    # 窗口的 MA_N 个收盘之间有 MA_N − 1 个间隔: 其中有没有除权日
    adjusted = pd.Series(split.astype(np.int64)).rolling(MA_N - 1).sum().to_numpy() > 0
    above = np.where(adjusted, adj.to_numpy() > ma.to_numpy() * (1 + 1e-9), MA_N * cents > total).astype(float)
    above[:MA_N - 1] = np.nan
    return pd.DataFrame({"up": (close > pre).astype(float), "above": above}, index=df["d"].to_numpy())


def align_flags(flags: dict, days: list) -> dict:
    """各股的标记对齐到全市场交易日轴 days(YYYYMMDD, 升序): {股票: 2 × 天数 的数组}, 没有行情的日子为 NaN"""
    idx = pd.Index(days)
    return {c: f.reindex(idx).to_numpy(dtype=float).T for c, f in flags.items()}


def breadth_rows(days, snaps, aligned, start_ymd="00000000") -> list:
    """逐日广度: [(交易日, n, up, n20, above20, nc)], 只算 start 起、且已经有快照的日子。
    days: 全市场交易日(YYYYMMDD 升序); snaps: snapshots() 的结果; aligned: align_flags() 的结果。
    每份快照管 [快照日, 下一份快照日) 这一段: 这段里成分股名单不变, 逐段一次矩阵求和。
    当天一只成分股都没有行情的日子(个股日线还没入库)不出行。"""
    out = []
    if not snaps:
        return out
    first = max(start_ymd, snaps[0][0])
    for k, (sd, codes) in enumerate(snaps):
        hi = snaps[k + 1][0] if k + 1 < len(snaps) else "99999999"
        a, b = bisect_left(days, max(sd, first)), bisect_left(days, hi)
        have = [c for c in codes if c in aligned]
        if a >= b or not have:
            continue
        up_m = np.stack([aligned[c][0][a:b] for c in have])       # 只数 × 天数
        ab_m = np.stack([aligned[c][1][a:b] for c in have])
        v_up, v_ab = ~np.isnan(up_m), ~np.isnan(ab_m)
        n = v_up.sum(axis=0)
        up = ((up_m == 1) & v_up).sum(axis=0)
        n20 = v_ab.sum(axis=0)
        above = ((ab_m == 1) & v_ab).sum(axis=0)
        for j in range(b - a):
            if n[j] > 0:
                out.append((days[a + j], int(n[j]), int(up[j]), int(n20[j]), int(above[j]), len(codes)))
    return out


def check_rows(rows, code) -> str:
    """写库前的自检, 返回拒写原因(None = 通过)"""
    for c, d, n, up, n20, above, nc in rows:
        if c != code:
            return f"混入别的指数 {c!r}"
        if not _ISO.match(str(d or "")):
            return f"{code} 日期格式不对 {d!r}"
        if not (1 <= n <= nc and 0 <= up <= n and 0 <= above <= n20 <= n):
            return f"{code} {d}: 家数不自洽 n={n} up={up} n20={n20} above20={above} nc={nc}"
    return None


def build(start=None, cfg=None, conn=None, raw=None) -> dict:
    """按库里的月度快照逐日算广度 → index_breadth: 自 start(YYYYMMDD; 省略 = 2010-01-01)起的日子整体重算、
    覆盖(幂等)。交易日取个股日线库的日期。返回 {指数(config 里的 code): 写入行数};
    自检不过抛 RuntimeError(整批不写)。"""
    specs = index_specs(cfg)
    if not specs:
        return {}
    start = str(start).replace("-", "") if start else "20100101"
    own_s, own_r = conn is None, raw is None
    sent = connect() if own_s else conn
    raw = raw_connect() if own_r else raw
    try:
        sent.execute(W_SCHEMA)                         # 全新的库: 两张表可能都还没建
        sent.execute(B_SCHEMA)
        sent.commit()
        warm = (date(int(start[:4]), int(start[4:6]), int(start[6:8])) - timedelta(days=WARM_DAYS))
        warm = warm.strftime("%Y%m%d")
        days = [str(r[0]).replace("-", "")
                for r in raw.execute("SELECT DISTINCT trade_date FROM daily WHERE trade_date>=? "
                                     "ORDER BY trade_date", (warm,)).fetchall()]
        out = {}
        for sp in specs:
            code, name = sp["code"], sp.get("name", sp["code"])
            snaps = snapshots(sent, sp["ts_code"])
            if not snaps or not days:
                print(f"[index-breadth] {name}: 没有成分股快照(index_weight 里没有它), 跳过")
                out[code] = 0
                continue
            flags = {}
            for c in sorted({c for _, codes in snaps for c in codes}):
                bars = raw.execute("SELECT trade_date, pre_close, close FROM daily "
                                   "WHERE ts_code=? AND trade_date>=? ORDER BY trade_date",
                                   (c, warm)).fetchall()
                if sum(1 for r in bars if r[0] < start) < MA_N - 1:
                    # 回看窗口里凑不满 20 个交易日(长期停牌后复牌的股票): 再取 start 之前的最后几行。
                    # 20 日线是"自己最近 20 个交易日", 不能因为日增只读了最近一段就变成没有 —
                    # 否则同一天全量重算与日增量算出来的 n20 / above20 不一样(11-27 在生产数据上空跑时发现)
                    older = raw.execute("SELECT trade_date, pre_close, close FROM daily WHERE ts_code=? "
                                        "AND trade_date<? ORDER BY trade_date DESC LIMIT ?",
                                        (c, start, MA_N - 1)).fetchall()
                    bars = sorted(set(older) | set(bars))
                if bars:
                    flags[c] = stock_flags(pd.DataFrame(bars, columns=["trade_date", "pre_close", "close"]))
            got = breadth_rows(days, snaps, align_flags(flags, days), start)
            data = [(code, _iso(d), n, up, n20, above, nc) for d, n, up, n20, above, nc in got]
            why = check_rows(data, code)
            if why:
                raise RuntimeError(f"[index-breadth] 拒写 {name}: {why}")
            if data:
                guarded_batch(sent, B_UPSERT, data, label=f"index_breadth {code}",
                              check=all_checks(chk_same(0, code), chk_unique(1)))
            out[code] = len(data)
            if data:
                print(f"[index-breadth] {name}({code}): {len(data)} 日 ({data[0][1]} ~ {data[-1][1]})")
        return out
    finally:
        if own_s:
            sent.close()
        if own_r:
            raw.close()


# ---------------------------------------------------------------- 入口

def refresh() -> bool:
    """日增(更新链): 先拉近 REFRESH_DAYS 天的月度快照(接上新发布的月末快照), 再重算这一段的广度。
    快照拉取失败时照常用库里已有的快照算。返回 True = 两步都没出错。"""
    conn = connect()
    ok = True
    try:
        # 起点要在拉取之前定: 以"拉取之前库内最新的快照"为准 — 拉取之后才算的话, 最新快照已经是新到的那份,
        # 起点跟着挪到它的下个月, 新快照所在的那个月里的日子就漏在重算范围之外(还是按旧名单算的)
        start = daily_start(conn)
        try:
            fetch_weights(start, conn=conn)
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[index-breadth] 成分股快照拉取失败(用库里已有的快照算): {type(e).__name__}: {e}")
        try:
            build(start, conn=conn)            # 新到快照之后的日子都按它重算
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[index-breadth] 广度计算失败: {type(e).__name__}: {e}")
    finally:
        conn.close()
    return ok


def fetch_run(start=None) -> bool:
    """补全通道入口(数据管理页[补全]): 自 start 起的月度快照全部拉回(幂等), 再用新快照把 start 起的广度重算一遍 —
    两张表是连着的, 只补快照不重算的话广度还按旧名单。不给 start(命令行)时与日增同一个起点。无 token 跳过。"""
    if not T.has_token():
        print("[index-weight] 无 tushare token, 跳过")
        return True
    beg = str(start).replace("-", "") if start else daily_start()
    got = fetch_weights(beg)
    print(f"[index-weight] 快照: " + (", ".join(f"{k} {v} 行" for k, v in sorted(got.items())) or "没有指数配置"))
    built = build(beg)
    print("[index-weight] 广度随之重算: " + (", ".join(f"{k} {v} 日" for k, v in sorted(built.items())) or "没有指数配置"))
    return True


def build_run(start=None) -> bool:
    """补全通道入口: 自 start 起逐日重算广度(用库里已有的快照与个股日线, 不调接口)"""
    build(start)
    return True


def main():
    ap = argparse.ArgumentParser(description="指数成分股与成分股广度")
    ap.add_argument("cmd", nargs="?", default="refresh", choices=["fetch", "build", "refresh"])
    ap.add_argument("--start", default=None, help="起始日 YYYYMMDD(fetch / build 用; 默认 20100101)")
    a = ap.parse_args()
    t0 = time.time()
    if a.cmd == "fetch":
        ok = fetch_run(a.start)
    elif a.cmd == "build":
        ok = build_run(a.start)
    else:
        ok = refresh()
    print(f"[index-breadth] {a.cmd} 结束 {time.time() - t0:.0f}s")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
