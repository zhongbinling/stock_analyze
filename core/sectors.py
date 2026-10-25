# -*- coding: utf-8 -*-
"""板块数据底座 (S5): 板块清单 / 日线 / 资金流 / 涨停行业聚合 —— 采集与回补

用法:
  python sectors.py daily            # 日常增量(并入 fetch_daily; 首次自动全量回补)
  python sectors.py backfill         # 全量回补板块日线(beg起) + 资金流
  python sectors.py pool-backfill    # 按mood_raw日期回补涨停行业聚合(约一年)
  python sectors.py stats            # 数据集状态(供数据管理页)
"""
import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import em_api  # noqa: E402
from db import connect  # noqa: E402

HIST_BEG = "20180101"          # 板块日线回补起点(温度计窗口绰绰有余)
SLEEP = 0.25
_unmatched = set()             # 未匹配的THS行业名(打印提示人工补alias)


# ---------------------------------------------------------------- 清单

def refresh_lists(conn) -> dict:
    """刷新行业+概念板块清单(概念默认停用); 返回行业板块当日涨跌家数快照"""
    snapshot, today = {}, time.strftime("%Y-%m-%d")
    for kind in ("industry", "concept"):
        rows = em_api.sector_list(kind)
        for r in rows:
            code, name = r.get("f12"), r.get("f14")
            if not code or not name:
                continue
            cur = conn.execute("SELECT enabled FROM sectors WHERE code=?", (code,)).fetchone()
            enabled = cur[0] if cur else (1 if kind == "industry" else 0)
            conn.execute(
                "INSERT INTO sectors(code,name,kind,enabled,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, kind=excluded.kind, "
                "updated_at=excluded.updated_at",
                (code, name, kind, enabled, today))
            if kind == "industry":
                snapshot[code] = (r.get("f104"), r.get("f105"))
        print(f"[sectors] 清单 {kind}: {len(rows)} 个")
    conn.commit()
    return snapshot


def _enabled_industries(conn) -> list:
    """所有已启用的板块(行业+概念), 用于日线/资金流采集"""
    return [r[0] for r in conn.execute(
        "SELECT code FROM sectors WHERE enabled=1 ORDER BY code")]


# ---------------------------------------------------------------- 日线/资金流

def _upsert_bars(conn, code, df) -> int:
    rows = [(code, r["date"], r["open"], r["close"], r["high"], r["low"], r["amount"])
            for _, r in df.iterrows()]
    conn.executemany(
        "INSERT INTO sector_daily(code,date,open,close,high,low,amount) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(code,date) DO UPDATE SET open=excluded.open,close=excluded.close,"
        "high=excluded.high,low=excluded.low,amount=excluded.amount", rows)
    if rows:
        conn.execute("UPDATE sectors SET first_date=?, last_date=? WHERE code=?",
                     (rows[0][1], rows[-1][1], code))
    return len(rows)


def _upsert_fflow(conn, code, df) -> int:
    rows = [(code, r["date"], r["main_net"], r["large_net"], r["super_net"])
            for _, r in df.iterrows()]
    conn.executemany(
        "INSERT INTO sector_fundflow(code,date,main_net,large_net,super_net) VALUES(?,?,?,?,?) "
        "ON CONFLICT(code,date) DO UPDATE SET main_net=excluded.main_net,"
        "large_net=excluded.large_net,super_net=excluded.super_net", rows)
    return len(rows)


def backfill_history(conn, beg: str = HIST_BEG):
    codes = _enabled_industries(conn)
    if not codes:
        print("[sectors] 无启用板块, 先刷新清单")
        return
    print(f"[sectors] 全量回补: {len(codes)} 板块日线({beg}起, 约{len(codes)*2}次请求)")
    t0 = time.time()
    fails = 0
    for i, code in enumerate(codes, 1):
        try:
            df = em_api.sector_kline(code, beg=beg)
            if not df.empty:
                _upsert_bars(conn, code, df)
                fails = 0
            else:
                fails += 1
        except Exception as e:  # noqa: BLE001
            fails += 1
            if fails == 1:
                print(f"  ✗ {code} 日线失败: {e}")
        if fails >= 10:        # 熔断: push2his被IP限流时立即止损, 避免拉长封锁
            print(f"  ✗✗ 连续{fails}次失败(push2his可能限流), 中断K线回补; 稍后重跑 backfill 续传")
            break
        time.sleep(SLEEP)
        if i % 50 == 0:
            conn.commit()
            print(f"  [{i}/{len(codes)}] {time.time()-t0:.0f}s")
    conn.commit()
    print(f"[sectors] 日线回补结束 {time.time()-t0:.0f}s")
    refresh_fundflow(conn, force=True)


def refresh_fundflow(conn, force=False):
    """板块资金流: dc主源(tushare按日, 近10日thin补齐) → 东财逐板块兜底;
    默认5天内已新跳过(周频节奏), 连续失败熔断"""
    if not force:
        last = conn.execute("SELECT MAX(date) FROM sector_fundflow").fetchone()[0]
        if last and last >= (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"):
            print("[sectors] 资金流近期已新, 跳过")
            return
    try:                                    # D1-12 G2: dc主源(免东财限流)
        import fetch_dc
        if fetch_dc.refresh_fflow_dc(conn):
            return
    except Exception as e:  # noqa: BLE001
        print(f"[sectors] 资金流dc主源异常, 回退东财: {type(e).__name__}: {e}")
    codes = _enabled_industries(conn)
    print(f"[sectors] 资金流刷新: {len(codes)} 板块")
    t0, fails = time.time(), 0
    for i, code in enumerate(codes, 1):
        try:
            df = em_api.sector_fundflow(code)
            if not df.empty:
                _upsert_fflow(conn, code, df)
                fails = 0
            else:
                fails += 1
        except Exception:  # noqa: BLE001
            fails += 1
        if fails >= 10:
            print("  ✗✗ 连续失败(同域限流), 中断资金流; 稍后重跑续传")
            break
        time.sleep(0.2)
        if i % 100 == 0:
            conn.commit()
    conn.commit()
    print(f"[sectors] 资金流结束 {time.time()-t0:.0f}s")


# ---------------------------------------------------------------- 涨停行业聚合

def _resolve(conn, name: str):
    """行业名 → BK码: 精确匹配 → 去Ⅰ/Ⅱ/Ⅲ后缀(tushare申万风格名) → 别名表;
    name 可能为 None/NaN(float), 先做类型防御"""
    if not name or not isinstance(name, str):
        return None
    row = conn.execute("SELECT code FROM sectors WHERE name=? AND kind='industry'",
                       (name,)).fetchone()
    if row:
        return row[0]
    base = name.rstrip("ⅠⅡⅢ").strip()
    if base and base != name:
        row = conn.execute("SELECT code FROM sectors WHERE name=? AND kind='industry'",
                           (base,)).fetchone()
        if row:
            return row[0]
    row = conn.execute("SELECT code FROM sector_alias WHERE name=?", (name,)).fetchone()
    if row:
        return row[0]
    if base and base != name:
        row = conn.execute("SELECT code FROM sector_alias WHERE name=?", (base,)).fetchone()
        if row:
            return row[0]
    return None


def _write_pool_agg(conn, date_iso: str, ind: dict):
    for name, st in ind.items():
        if not isinstance(name, str) or not name:      # None/NaN(float)防御
            continue
        code = _resolve(conn, name)
        if not code:
            # 清单未入库(如东财clist被限流)时以 @行业名 落库, 待 remap_pending 重映射
            code = "@" + name
            _unmatched.add(name)
        conn.execute(
            "INSERT OR REPLACE INTO sector_pool_stat(date,code,zt_count,dt_count,zb_count,"
            "lb_max,lb2_count) VALUES(?,?,?,?,?,?,?)",
            (date_iso, code, st["zt"], st["dt"], st["zb"], st["lb_max"], st["lb2"]))


def remap_pending(conn) -> int:
    """把 @行业名 形态的池聚合行重映射为 BK 码(清单入库后调用); 返回重映射行数"""
    pend = conn.execute(
        "SELECT DISTINCT code FROM sector_pool_stat WHERE code LIKE '@%'").fetchall()
    n = 0
    for (code,) in pend:
        name = code[1:]
        bk = _resolve(conn, name)
        if not bk:
            continue
        rows = conn.execute("SELECT date,zt_count,dt_count,zb_count,lb_max,lb2_count "
                            "FROM sector_pool_stat WHERE code=?", (code,)).fetchall()
        for d, zt, dt_, zb, lbm, lb2 in rows:
            conn.execute(
                "INSERT OR REPLACE INTO sector_pool_stat(date,code,zt_count,dt_count,zb_count,"
                "lb_max,lb2_count) VALUES(?,?,?,?,?,?,?)",
                (d, bk, zt, dt_, zb, lbm, lb2))
        conn.execute("DELETE FROM sector_pool_stat WHERE code=?", (code,))
        n += len(rows)
        _unmatched.discard(name)     # 已成功映射, 从未匹配清单移除
    if n:
        conn.commit()
        print(f"[sectors] 重映射 {n} 行 @行业名 → BK码")
    return n


def agg_today(conn):
    """当日涨停池按行业聚合(东财池口径)。
    有 tushare token 时跳过: 当日行业聚合由主链的 fetch_tushare_mood_today 统一提供
    (tushare industry 体系完整且与2020起的历史同口径), 避免同日双口径双写。"""
    try:
        import tushare_api
        if tushare_api.has_token():
            print("[sectors] 当日行业聚合走 tushare 口径(见 [ts-mood]), 跳过东财池聚合")
            return
    except ImportError:
        pass
    d = time.strftime("%Y%m%d")
    try:
        st = em_api.ths_pool_stats(d, with_industry=True)
    except Exception as e:  # noqa: BLE001
        print(f"[sectors] 当日池聚合失败(不阻塞): {e}")
        return
    if st and st.get("ind"):
        _write_pool_agg(conn, time.strftime("%Y-%m-%d"), st["ind"])
        conn.commit()
        print(f"[sectors] 当日行业聚合: {len(st['ind'])} 个行业")


def pool_backfill(conn):
    """回补板块涨停行业聚合。口径注意: 行业归属(hybk)仅东财池携带, 其历史约最近
    一个月, 故只回补近40天(更早日期池接口返回空, 无谓浪费请求); 此后逐日积累。"""
    cutoff = (datetime.now() - timedelta(days=32)).strftime("%Y-%m-%d")   # 东财池边界约30天
    dates = [r[0] for r in conn.execute(
        "SELECT date FROM mood_raw WHERE zt_count IS NOT NULL AND date>=? ORDER BY date",
        (cutoff,))]
    have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM sector_pool_stat")}
    todo = [d for d in dates if d not in have]
    if not todo:
        print("[sectors] 池聚合无缺失日期(近40天窗口)")
        return
    print(f"[sectors] 池聚合回补: {len(todo)} 天 ({todo[0]} ~ {todo[-1]}, 东财池口径)")
    t0 = time.time()
    empty = 0
    for i, d in enumerate(todo, 1):
        try:
            st = em_api.ths_pool_stats(d.replace("-", ""), with_industry=True)
            ind = (st or {}).get("ind") or {}
            if ind:
                _write_pool_agg(conn, d, ind)
                conn.commit()
            else:
                empty += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {d}: {e}")
        time.sleep(0.35)
        if i % 15 == 0:
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s")
    print(f"[sectors] 池聚合回补完成 {time.time()-t0:.0f}s (空日期{empty}个=超出东财池历史)")


# ---------------------------------------------------------------- 日常入口

def is_trading_day(conn, iso: str) -> bool:
    """D1-11r: 当日是否交易日 — calendar优先(is_open), 未覆盖段回退周一~五粗口径。

    周末/节假日运行更新链时, 东财实时板块板是上一交易日的收盘状态,
    若照写会把旧状态盖章成"今日"产生伪交易日行(如2026-10-24周六)。
    """
    ymd = iso.replace("-", "")
    try:
        r = conn.execute("SELECT is_open FROM calendar WHERE cal_date=?", (ymd,)).fetchone()
        if r is not None:
            return bool(r[0])
    except Exception:  # noqa: BLE001
        pass
    return datetime.strptime(iso, "%Y-%m-%d").weekday() < 5


def _update_via_dc(win_days: int = 30) -> bool:
    """D1-12 G1: 板块日线+涨跌家数 tushare dc 主源(近30自然日窗口幂等回补)。

    push2his 限流常态化的稳定路径; 返回 False(无token/接口异常/存在失败日)
    时由调用方回退原东财路径。注意: 无token必须返回False — 若返回True,
    update_daily会以为主源成功而彻底跳过板块日线。"""
    try:
        import tushare_api as T
        if not T.has_token():
            print("[sectors] dc主源: 无 tushare token, 回退东财")
            return False
        from datetime import date as _d, timedelta as _td
        import fetch_dc
        start = (_d.today() - _td(days=win_days)).strftime("%Y%m%d")
        return bool(fetch_dc.run(start))
    except Exception as e:  # noqa: BLE001
        print(f"[sectors] dc主源异常, 回退东财: {type(e).__name__}: {e}")
        return False


def _kline_incremental_em(conn, beg, today):
    """东财逐板块增量日线(push2his, 兜底路径; 熔断: 连续10次失败快速跳过)"""
    codes = _enabled_industries(conn)
    t0 = time.time()
    ok_n, fails = 0, 0
    for i, code in enumerate(codes, 1):
        try:
            df = em_api.sector_kline(code, beg=beg)
            if not df.empty:
                _upsert_bars(conn, code, df)
                ok_n += 1
                fails = 0
            else:
                fails += 1
        except Exception as e:  # noqa: BLE001
            fails += 1
            if fails == 1:
                print(f"  ✗ {code} 日线失败: {e}")
        if fails >= 10:      # 熔断: push2his限流/不可达时快速跳过, 不再逐板块爬超时
            print(f"  ✗✗ 连续{fails}次失败(push2his可能限流), 跳过本次日线增量; 稍后重跑续传")
            break
        time.sleep(SLEEP)
        if i % 50 == 0:
            conn.commit()
            print(f"  [{i}/{len(codes)}] {ok_n}成功 {time.time()-t0:.0f}s")
    conn.commit()
    print(f"[sectors] 增量日线(东财兜底): {ok_n}/{len(codes)} 板块至 {today} "
          f"({time.time()-t0:.0f}s)")


def _snapshot_updown(conn, today, snapshot):
    """当日快照: 板块涨跌家数(东财实时板, 兜底路径) — D1-11r: 仅交易日写入
    (周末实时板=上一交易日状态重复盖章 → 伪交易日行污染热力图)"""
    if is_trading_day(conn, today):
        for code, (up, down) in snapshot.items():
            if up is None and down is None:
                continue
            conn.execute(
                "INSERT INTO sector_daily(code,date,up_count,down_count) VALUES(?,?,?,?) "
                "ON CONFLICT(code,date) DO UPDATE SET up_count=excluded.up_count,"
                "down_count=excluded.down_count", (code, today, up, down))
    else:
        print(f"[sectors] 当日快照跳过({today} 非交易日, 周末不产生伪交易日行)")


def update_daily(conn=None, auto_backfill=True):
    """日常增量: 清单 → 板块日线+涨跌家数(dc主源/东财兜底) → 资金流(周频) → 当日池聚合;
    首次运行(sector_daily为空)自动全量回补(深度历史仍走东财)"""
    conn = conn or connect()
    try:
        snapshot = refresh_lists(conn)
    except Exception as e:  # noqa: BLE001
        print(f"[sectors] 清单刷新失败(继续用现有清单): {type(e).__name__}: {e}")
        snapshot = {}
    today = time.strftime("%Y-%m-%d")

    n = conn.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0]
    if n == 0 and auto_backfill:
        backfill_history(conn)
        pool_backfill(conn)
        _snapshot_updown(conn, today, snapshot)   # 深史回补后同样落当日快照(原行为保留)
    else:
        # D1-12 G1: dc主源(tushare, 免push2his限流) → 失败回退东财(逐板块kline+实时板快照)
        if not _update_via_dc():
            last = conn.execute("SELECT MAX(date) FROM sector_daily").fetchone()[0]
            beg = ((datetime.strptime(last, "%Y-%m-%d") - timedelta(days=10))
                   .strftime("%Y%m%d")) if last else HIST_BEG
            _kline_incremental_em(conn, beg, today)
            _snapshot_updown(conn, today, snapshot)
    refresh_fundflow(conn)

    conn.commit()
    remap_pending(conn)
    agg_today(conn)
    if _unmatched:
        print(f"[sectors] ⚠ 未映射THS行业名{len(_unmatched)}个(已按@名字落库, 清单可用后自动重映射): "
              f"{sorted(_unmatched)[:8]}")


def stats(conn=None) -> list:
    """板块数据集状态(数据管理页)"""
    conn = conn or connect()
    today = time.strftime("%Y-%m-%d")
    out = []
    n_ind, n_con = conn.execute(
        "SELECT SUM(kind='industry'), SUM(kind='concept') FROM sectors").fetchone()
    out.append({"code": "sectors", "name": "板块清单(东财BK)", "source": "东财clist",
                "rows": (n_ind or 0) + (n_con or 0),
                "first": None, "last": today,
                "health": "ok" if (n_ind or 0) >= 100 else "warn",
                "note": f"行业{n_ind or 0}(启用, 细分到三级) + 概念{n_con or 0}(默认停用)"})
    rows, codes, first, last = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT code), MIN(date), MAX(date) FROM sector_daily").fetchone()
    out.append({"code": "sector_daily", "name": "板块日线", "source": "东财push2his",
                "rows": rows, "first": first, "last": last,
                "health": "ok" if last == today or _stale(last) <= 4 else "warn",
                "note": f"{codes} 板块; 涨跌家数为采集日起积累"})
    rows, last = conn.execute(
        "SELECT COUNT(*), MAX(date) FROM sector_pool_stat").fetchone()
    out.append({"code": "sector_pool", "name": "板块涨停聚合(THS行业)", "source": "同花顺",
                "rows": rows or 0, "first": None, "last": last,
                "health": "ok" if last == today or _stale(last) <= 4 else "warn",
                "note": f"覆盖至 {last or '--'}(源历史约一年)"})
    rows, codes, last = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT code), MAX(date) FROM sector_fundflow").fetchone()
    out.append({"code": "sector_fflow", "name": "板块资金流", "source": "东财fflow",
                "rows": rows or 0, "first": None, "last": last,
                "health": "ok" if (last and _stale(last) <= 7) else "warn",
                "note": f"{codes or 0} 板块·约最近120日·周频刷新"})
    return out


def _stale(last: str) -> int:
    if not last:
        return 99
    try:
        y, m, d = map(int, last.split("-"))
        return (datetime.now() - datetime(y, m, d)).days
    except ValueError:
        return 99


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["daily", "backfill", "pool-backfill", "stats"])
    args = ap.parse_args()
    conn = connect()
    if args.cmd == "daily":
        update_daily(conn)
    elif args.cmd == "backfill":
        refresh_lists(conn)
        backfill_history(conn)
    elif args.cmd == "pool-backfill":
        pool_backfill(conn)
    else:
        for s in stats(conn):
            print(s)


if __name__ == "__main__":
    main()
