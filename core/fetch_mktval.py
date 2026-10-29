# -*- coding: utf-8 -*-
"""D1-15 I1: 全A/宽基估值聚合链 — daily_basic 即时聚合, 只存结果

表:
  mkt_index_members(index_code,trade_date,con_code,weight)  成分月快照缓存
  mkt_val_daily(date PK, alla_pe, alla_pb, pe50,pb50,pe300,pb300,
                pe500,pb500,pe1000,pb1000)                  聚合结果

算法(整体法/市值加权调和平均, 剔除亏损股): PE = Σmv / Σ(mv/pe), PB同。
宽基成分 = index_weight ≤目标日最近月快照(半年调样, 无未来函数)。

用法: python fetch_mktval.py --start 20100101   # 回补; 日增由更新链挂载
"""
import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from db import connect, trading_dates, guarded_batch, all_checks, chk_same  # noqa: E402
import tushare_api as T  # noqa: E402
from fetch_daily import load_cfg, norm_date, ymd  # noqa: E402

INDEXES = {"000016.SH": "50", "000300.SH": "300", "000905.SH": "500", "000852.SH": "1000"}
VAL_SQL = ("INSERT INTO mkt_val_daily(date,alla_pe,alla_pb,pe50,pb50,pe300,pb300,"
           "pe500,pb500,pe1000,pb1000,mv_total) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
           "ON CONFLICT(date) DO UPDATE SET alla_pe=excluded.alla_pe,"
           "alla_pb=excluded.alla_pb,pe50=excluded.pe50,pb50=excluded.pb50,"
           "pe300=excluded.pe300,pb300=excluded.pb300,pe500=excluded.pe500,"
           "pb500=excluded.pb500,pe1000=excluded.pe1000,pb1000=excluded.pb1000,"
           "mv_total=excluded.mv_total")


def _ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS mkt_index_members(
      index_code TEXT, trade_date TEXT, con_code TEXT, weight REAL,
      PRIMARY KEY(index_code, trade_date, con_code))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mkt_val_daily(
      date TEXT PRIMARY KEY, alla_pe REAL, alla_pb REAL,
      pe50 REAL, pb50 REAL, pe300 REAL, pb300 REAL,
      pe500 REAL, pb500 REAL, pe1000 REAL, pb1000 REAL,
      mv_total REAL)""")
    # D1-18 M4: 旧库增列 mv_total(亿元, 全A总市值)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mkt_val_daily)")}
    if "mv_total" not in cols:
        conn.execute("ALTER TABLE mkt_val_daily ADD COLUMN mv_total REAL")
    conn.commit()


def _val_check(target_iso):
    def f(rows):
        r = rows[0]
        if r[0] != target_iso:
            return f"日期错位 {r[0]}"
        for i, v in enumerate(r):
            if i == 0 or v is None:
                continue
            hi = 1e7 if i == 11 else 1000        # M4: mv_total(亿)豁免<1000
            if v != v or v <= 0 or v >= hi:
                return f"越界 r[{i}]={v!r}"
        return None
    return all_checks(chk_same(0, target_iso), f)


def _agg(rows, members=None, min_n=30):
    """整体法 PE/PB: 剔除亏损(pe≤0/pb≤0)与无效市值; members=None 为全A"""
    tot_mv = tot_e = tot_bv = 0.0
    n = 0
    for ts, pe, pb, mv in rows:
        if members is not None and ts not in members:
            continue
        if not mv or mv <= 0 or pe is None or pb is None or pe <= 0 or pb <= 0:
            continue
        tot_mv += mv
        tot_e += mv / pe
        tot_bv += mv / pb
        n += 1
    if tot_mv <= 0 or tot_e <= 0 or tot_bv <= 0 or n < min_n:
        return None, None
    return round(tot_mv / tot_e, 4), round(tot_mv / tot_bv, 4)


def backfill_members(conn=None):
    """成分月快照全量缓存(按年分页, upsert幂等)"""
    import datetime as _dt
    conn = conn or connect()
    _ensure_tables(conn)
    this_year = _dt.date.today().year
    for code in sorted(INDEXES):
        got = 0
        for y in range(2010, this_year + 1):
            try:
                df = T.call("index_weight", "trade_date,con_code,weight",
                            index_code=code, start_date=f"{y}0101", end_date=f"{y}1231")
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ members {code} {y}: {e}")
                continue
            if df.empty:
                continue
            rows = [(code, str(r["trade_date"]), str(r["con_code"]),
                     float(r["weight"]) if r["weight"] is not None else None)
                    for _, r in df.iterrows()]
            if not rows:
                continue
            cur = conn.cursor()
            cur.executemany(
                "INSERT INTO mkt_index_members(index_code,trade_date,con_code,weight) "
                "VALUES(?,?,?,?) ON CONFLICT(index_code,trade_date,con_code) "
                "DO UPDATE SET weight=excluded.weight", rows)
            conn.commit()
            got += len(rows)
        print(f"[mktval] 成分 {code}: 累计 {got} 快照行")
    conn.close()


def _members_on(conn, code, ymd_str):
    """≤目标日的最近成分快照(集合); 无快照返回None"""
    snap = conn.execute(
        "SELECT MAX(trade_date) FROM mkt_index_members "
        "WHERE index_code=? AND trade_date<=?", (code, ymd_str)).fetchone()[0]
    if not snap:
        return None
    return {r[0] for r in conn.execute(
        "SELECT con_code FROM mkt_index_members WHERE index_code=? AND trade_date=?",
        (code, snap))}


def _f(v):
    """None/NaN/inf → None(pandas iterrows 把 None 变 nan 绕过判空;
    inf 不拦会污染Σ致当日守卫永久拒绝, 重跑无法自愈)"""
    import math
    if v is None:
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _fetch_one_day(conn, d_iso, verbose=True, min_n=30):
    """单日聚合落库, 返回是否写入"""
    d_ymd = ymd(d_iso)
    try:
        df = T.call("daily_basic", "ts_code,pe_ttm,pb,total_mv", trade_date=d_ymd)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {d_iso}: {e}")
        return False
    if df.empty:
        return False                        # 未出数/早于源起点
    rows = [(str(r["ts_code"]), _f(r["pe_ttm"]), _f(r["pb"]), _f(r["total_mv"]))
            for _, r in df.iterrows()]
    alla_pe, alla_pb = _agg(rows, min_n=min_n)
    vals = {"alla_pe": alla_pe, "alla_pb": alla_pb}
    for code, tag in INDEXES.items():
        mset = _members_on(conn, code, d_ymd)
        pe, pb = (None, None) if mset is None else _agg(rows, mset, min_n=min_n)
        vals[f"pe{tag}"], vals[f"pb{tag}"] = pe, pb
    # D1-18 M4: 全A总市值(亿元) — 与pe/pb有效性解耦(亏损股pe无效但市值在);
    # total_mv单位万元 → 亿元=÷1e4
    mv_total = round(sum(mv for _, _, _, mv in rows if mv and mv > 0) / 1e4, 1)
    row = (d_iso, vals["alla_pe"], vals["alla_pb"], vals["pe50"], vals["pb50"],
           vals["pe300"], vals["pb300"], vals["pe500"], vals["pb500"],
           vals["pe1000"], vals["pb1000"], mv_total)
    if all(v is None for v in row[1:]):
        return False
    guarded_batch(conn, VAL_SQL, [row], label=f"mktval {d_iso}",
                  check=_val_check(d_iso))
    if verbose:
        print(f"  {d_iso}: 全A {alla_pe}/{alla_pb} 300 {vals['pe300']}/{vals['pb300']}")
    return True


def refresh(conn=None, lookback_days: int = 7) -> bool:
    """日增(回看7天自愈): 逐交易日聚合"""
    from datetime import date as _d, timedelta as _td
    conn = conn or connect()
    _ensure_tables(conn)
    start = (_d.today() - _td(days=lookback_days)).strftime("%Y%m%d")
    dates = trading_dates(conn, load_cfg()["index"]["primary"], start=norm_date(start))
    n = 0
    for d in dates:
        if _fetch_one_day(conn, d, verbose=False):
            n += 1
    if n:
        print(f"[mktval] 估值聚合: {n} 日")
    return True


def verify_vs_official(conn=None):
    """互校(口径对照): 自算300PE vs index_val_daily官方。

    两者口径本就不同(TTM自算/剔除规则/月频成分时点 vs 官方),
    实测重叠日比值0.88~1.07无固定方向 — 中位差<15%属预期口径差;
    >15%才是真问题(成分缓存失效/算法回归)。"""
    conn = conn or connect()
    try:
        rows = conn.execute(
            "SELECT a.pe300, b.pe FROM mkt_val_daily a JOIN index_val_daily b "
            "ON a.date=b.date AND b.code='000300.SH' "
            "WHERE a.pe300 IS NOT NULL AND b.pe IS NOT NULL "
            "ORDER BY a.date DESC LIMIT 250").fetchall()
    except Exception:  # noqa: BLE001
        conn.close()
        return
    conn.close()
    if len(rows) < 30:
        return
    diffs = sorted(abs(p - o) / o for p, o in rows if o)
    med = diffs[len(diffs) // 2]
    if med > 0.15:
        print(f"[mktval] ⚠ 互校官方300PE中位差 {med*100:.2f}% (>15%, 疑成分缓存/算法回归)")
    else:
        print(f"[mktval] 互校官方300PE({len(rows)}日): 中位差 {med*100:.2f}% "
              f"(TTM自算vs官方口径差, 预期内)")


def run(start="20100101") -> bool:
    """回补: 成分缓存 → 逐交易日聚合(跳过已有)"""
    if not T.has_token():
        print("[mktval] 无 tushare token, 跳过")
        return True
    t0 = time.time()
    backfill_members()
    conn = connect()
    _ensure_tables(conn)
    iso = norm_date(str(start))
    dates = trading_dates(conn, load_cfg()["index"]["primary"], start=iso)
    # M4: mv_total空(旧史/新日)都重拉 — 旧日全行幂等upsert补列
    have = {r[0] for r in conn.execute(
        "SELECT date FROM mkt_val_daily WHERE mv_total IS NOT NULL")}
    todo = [d for d in dates if d not in have]
    print(f"[mktval] {len(todo)}/{len(dates)} 交易日待聚合 (自 {iso})")
    fails = 0
    for i, d in enumerate(todo, 1):
        # M4r: _fetch_one_day 内部吞错返回False — 连续失败计数须含False,
        # 否则网络故障(如DNS污染)会空转数小时不中断(实测1916次✗/12.8h)
        wrote = False
        try:
            wrote = _fetch_one_day(conn, d, verbose=(i % 50 == 0))
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {d}: {e}")
        fails = 0 if wrote else fails + 1
        if fails >= 10:
            print("  ✗✗ 连续失败中断(重跑续传)")
            break
        if i % 100 == 0:
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s")
    n = conn.execute("SELECT COUNT(*) FROM mkt_val_daily").fetchone()[0]
    conn.close()
    verify_vs_official()
    print(f"[mktval] 结束 {time.time()-t0:.0f}s, 共{n}日"
          + (f", 失败{fails}(重跑自愈)" if fails else ""))
    return fails == 0


def main():
    ap = argparse.ArgumentParser(description="全A/宽基估值聚合回补")
    ap.add_argument("--start", default="20100101")
    args = ap.parse_args()
    sys.exit(0 if run(args.start) else 1)


if __name__ == "__main__":
    main()
