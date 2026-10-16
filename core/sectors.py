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
    return [r[0] for r in conn.execute(
        "SELECT code FROM sectors WHERE kind='industry' AND enabled=1 ORDER BY code")]


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
    """资金流(约最近120日全量返回, 幂等); 默认7天内跳过; 连续失败熔断"""
    if not force:
        last = conn.execute("SELECT MAX(date) FROM sector_fundflow").fetchone()[0]
        if last and last >= (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"):
            print("[sectors] 资金流近期已新, 跳过")
            return
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

def update_daily(conn=None, auto_backfill=True):
    """日常增量: 清单 → 增量日线 → 快照家数 → 资金流(周频) → 当日池聚合;
    首次运行(sector_daily为空)自动全量回补"""
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
    else:
        last = conn.execute("SELECT MAX(date) FROM sector_daily").fetchone()[0]
        beg = ((datetime.strptime(last, "%Y-%m-%d") - timedelta(days=10))
               .strftime("%Y%m%d")) if last else HIST_BEG
        codes = _enabled_industries(conn)
        t0 = time.time()
        ok_n = 0
        for code in codes:
            try:
                df = em_api.sector_kline(code, beg=beg)
                if not df.empty:
                    _upsert_bars(conn, code, df)
                    ok_n += 1
            except Exception:  # noqa: BLE001
                pass
            time.sleep(SLEEP)
        conn.commit()
        print(f"[sectors] 增量日线: {ok_n}/{len(codes)} 板块至 {today} ({time.time()-t0:.0f}s)")
        refresh_fundflow(conn)

    # 当日快照: 板块涨跌家数(积累制)
    for code, (up, down) in snapshot.items():
        if up is None and down is None:
            continue
        conn.execute(
            "INSERT INTO sector_daily(code,date,up_count,down_count) VALUES(?,?,?,?) "
            "ON CONFLICT(code,date) DO UPDATE SET up_count=excluded.up_count,"
            "down_count=excluded.down_count", (code, today, up, down))
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
