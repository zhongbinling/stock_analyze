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

from db import connect, trading_dates, guarded_day_write, guarded_batch  # noqa: E402
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


# ---------------------------------------------------------------- D1-12 G3: 校准与窗口探测

def _calibrate(sample: dict, rows, val_idx: int, key_idx: int, label: str):
    """G3: dc落地前与存量(东财旧值)抽样比对 — 中位差>1%且样本≥20告警(不阻断)。

    dc同源权威照常写入; 告警=上游口径漂移信号(任务日志可见)。
    近端多为dc自写入, 比对自然趋零, 仅真实漂移才触发。
    """
    diffs = []
    for r in rows:
        old = sample.get(r[key_idx])
        if old is None:
            continue
        old = float(old)
        if abs(old) < 1:                    # 0/近0值不参与相对差
            continue
        d = abs(float(r[val_idx]) - old) / abs(old)
        if d > 1e-9:                        # 完全一致不计
            diffs.append(d)
    if len(diffs) >= 20:
        med = sorted(diffs)[len(diffs) // 2]
        if med > 0.01:
            print(f"  ⚠ [calib] {label}: 与存量中位差 {med * 100:.2f}% "
                  f"({len(diffs)}对不一致) — 疑似口径漂移, 已按dc写入")


def dc_window_start(refresh_days: int = 7):
    """G3: 探测 dc_daily 可用窗口最早交易日(二分, ~12次调用), 缓存refresh_days天。

    用于截断 run() 的 start(窗口外日期逐日空耗调用); 探测失败返回 None。
    """
    import datetime as _dt
    import json as _json
    cache = BASE / "data" / "dc_window.json"
    today = _dt.date.today()
    if cache.exists():
        try:
            c = _json.loads(cache.read_text(encoding="utf-8"))
            if _dt.date.fromisoformat(c["probed"]) >= today - _dt.timedelta(days=refresh_days):
                return c["start"]
        except (ValueError, KeyError):
            pass
    conn = connect()
    days = [r[0].replace("-", "") for r in conn.execute(
        "SELECT DISTINCT date FROM index_daily WHERE date >= date('now', '-4 year') "
        "ORDER BY date")]
    conn.close()
    if len(days) < 30:
        return None

    def has(d):
        try:
            return not T.call("dc_daily", "ts_code", trade_date=d).empty
        except Exception:  # noqa: BLE001
            return False

    if not has(days[-1]):                   # 最新日无数据(节假日后未出数) → 验证前一日
        if len(days) < 2 or not has(days[-2]):
            return None
    lo, hi = 0, len(days) - 1               # days[hi]有数据, 找最小有数据下标
    while lo < hi:
        mid = (lo + hi) // 2
        if has(days[mid]):
            hi = mid
        else:
            lo = mid + 1
    start = days[lo]
    try:
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(_json.dumps(
            {"start": start, "probed": today.isoformat()}), encoding="utf-8")
    except OSError:
        pass
    print(f"[dc] 可用窗口起点: {start} (二分探测)")
    return start


def run(start="20260901") -> bool:
    """板块日线+涨跌家数幂等回补: 只处理覆盖不足的交易日; 单日两批走守卫写入"""
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
    win = dc_window_start()                  # G3: 截断至dc窗口, 窗口外日期空耗调用
    if win and ymd(iso) < win:
        print(f"[dc] start {ymd(iso)} 早于dc窗口起点 {win}, 已截断")
        iso = norm_date(win)
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
            if not rows:
                # G1: dc对某日无数据(未出数/超dc窗口) → 跳过不计失败,
                # 否则空批次会触发守卫拒绝并误判主源失败而回退东财
                print(f"  [{i}/{len(todo)}] {d}: dc日线无数据(未出数/超窗口), 跳过")
                continue
            _calibrate(dict(conn.execute(          # G3: 与存量(东财旧值)抽样比对
                "SELECT code, close FROM sector_daily "
                "WHERE date=? AND close IS NOT NULL LIMIT 300", (d,))),
                rows, val_idx=5, key_idx=0, label=f"日线 {d}")
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
            if rows2:
                guarded_day_write(
                    conn,
                    "INSERT INTO sector_daily(code,date,up_count,down_count) "
                    "VALUES(?,?,?,?) ON CONFLICT(code,date) DO UPDATE SET "
                    "up_count=excluded.up_count,down_count=excluded.down_count",
                    rows2, d, table="sector_daily", date_col="date")
            else:
                print(f"  [{i}/{len(todo)}] {d}: dc涨跌家数无数据, 跳过")
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


# ---------------------------------------------------------------- D1-12 G2: 资金流

FFLOW_UPSERT = ("INSERT INTO sector_fundflow(code,date,main_net) VALUES(?,?,?) "
                "ON CONFLICT(code,date) DO UPDATE SET main_net=excluded.main_net")


def _thin_fflow_dates(conn, dates, need) -> list:
    """资金流覆盖不足90%的交易日(EM残期28/508即此判出)"""
    out = []
    for d in dates:
        n = conn.execute("SELECT COUNT(*) FROM sector_fundflow WHERE date=?",
                         (d,)).fetchone()[0]
        if n < need * 0.9:
            out.append(d)
    return out


def refresh_fflow_dc(conn=None, win_days: int = 10) -> bool:
    """G2 日更主源: 近10自然日thin日按trade_date查(一次拿全板块), 守卫写入。

    返回 False(无token/异常)时由调用方回退东财逐板块。"""
    conn = conn or connect()
    if not T.has_token():
        print("[fflow-dc] 无 tushare token")
        return False
    codes = _enabled_codes(conn)
    if not codes:
        return True
    from datetime import date as _d, timedelta as _td
    start = (_d.today() - _td(days=win_days)).strftime("%Y%m%d")
    dates = trading_dates(conn, load_cfg()["index"]["primary"], start=norm_date(start))
    todo = _thin_fflow_dates(conn, dates, len(codes))
    print(f"[fflow-dc] {len(todo)}/{len(dates)} 交易日待补 (启用板块 {len(codes)})")
    if not todo:
        return True
    fails = 0
    for i, d in enumerate(todo, 1):
        try:
            df = T.call("moneyflow_ind_dc", "ts_code,net_amount", trade_date=ymd(d))
            rows = [(c, d, float(r["net_amount"]))
                    for _, r in df.iterrows()
                    if (c := r["ts_code"].split(".")[0]) in codes
                    and r["net_amount"] is not None]
            if not rows:
                print(f"  [{i}/{len(todo)}] {d}: dc无数据(未出数), 跳过")
                continue
            _calibrate(dict(conn.execute(          # G3: 与存量(东财旧值)抽样比对
                "SELECT code, main_net FROM sector_fundflow "
                "WHERE date=? AND main_net IS NOT NULL LIMIT 300", (d,))),
                rows, val_idx=2, key_idx=0, label=f"资金流 {d}")
            guarded_day_write(conn, FFLOW_UPSERT, rows, d,
                              table="sector_fundflow", date_col="date")
            print(f"  [{i}/{len(todo)}] {d}: {len(rows)} 板块")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ {d}: {e}")
            if fails >= 5:
                print("  ✗✗ 连续失败中断(重跑自愈)")
                return False
    return fails == 0


def backfill_fflow_dc(start="20230901") -> bool:
    """G2 历史回补: 按代码查全历史(2023-09起, 源最早), 每代码一批守卫写入(upsert幂等)。

    按代码一次调用拿全史(日更见refresh_fflow_dc), EM缺的2023-09~2026-04段
    与残缺日(28/508期)一并补齐。"""
    if not T.has_token():
        print("[fflow-dc] 无 tushare token, 跳过")
        return True
    t0 = time.time()
    conn = connect()
    codes = _enabled_codes(conn)
    iso = norm_date(start)
    print(f"[fflow-dc] 按代码回补 {len(codes)} 板块, 目标自 {iso} 起")
    if not codes:
        conn.close()
        return True
    ok, fails = 0, 0
    for i, code in enumerate(sorted(codes), 1):
        try:
            df = T.call("moneyflow_ind_dc", "trade_date,net_amount",
                        ts_code=f"{code}.DC")
            rows = [(code, norm_date(str(r["trade_date"])), float(r["net_amount"]))
                    for _, r in df.iterrows()
                    if r["net_amount"] is not None and str(r["trade_date"]) >= ymd(iso)]
            if not rows:
                continue
            _calibrate(dict(conn.execute(          # G3: 与存量(东财旧值)抽样比对
                "SELECT date, main_net FROM sector_fundflow "
                "WHERE code=? AND main_net IS NOT NULL LIMIT 300", (code,))),
                rows, val_idx=2, key_idx=1, label=f"fflow {code}")
            guarded_batch(conn, FFLOW_UPSERT, rows, label=f"fflow {code}",
                          check=lambda rs: None if len({r[1] for r in rs}) == len(rs)
                          else "批内日期重复")
            ok += 1
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ✗ {code}: {e}")
            if fails >= 10:
                print("  ✗✗ 连续失败中断(重跑续传)")
                break
        if i % 50 == 0:
            print(f"  [{i}/{len(codes)}] {ok}成功 {time.time()-t0:.0f}s")
    n = conn.execute("SELECT COUNT(*) FROM sector_fundflow").fetchone()[0]
    conn.close()
    print(f"[fflow-dc] 结束 {time.time()-t0:.0f}s, 共{n}行"
          + (f", 失败{fails}(重跑自愈)" if fails else ""))
    return fails == 0


def main():
    ap = argparse.ArgumentParser(description="东财板块数据 tushare(dc) 近端回补")
    ap.add_argument("--start", default="20260901", help="起始日 YYYYMMDD/ISO")
    args = ap.parse_args()
    sys.exit(0 if run(args.start) else 1)


if __name__ == "__main__":
    main()
