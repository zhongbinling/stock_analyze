# -*- coding: utf-8 -*-
"""每日数据采集: 指数日线 + 涨停/跌停/炸板/昨日涨停池 → SQLite

用法:
  python fetch_daily.py                    # 指数全量刷新 + 自 pool_start 起回补缺失的池数据
  python fetch_daily.py --start 20260901   # 只回补指定起始日之后的池数据
  python fetch_daily.py --force            # 忽略断点, 重新抓取(覆盖)
首次回补约 300 个交易日需 15~30 分钟, 支持断点续跑(重跑自动跳过已有日期)。
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

import em_api  # noqa: E402
from db import connect, upsert_index, upsert_mood, get_mood_dates, trading_dates  # noqa: E402


def load_cfg():
    with open(BASE / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def norm_date(d) -> str:
    s = str(d)
    return s[:10] if "-" in s else f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def ymd(d) -> str:
    return norm_date(d).replace("-", "")


def fetch_index(cfg, index_start):
    conn = connect()
    for key in ("primary", "secondary"):
        secid = cfg["index"][key]
        beg = index_start.replace("-", "")
        df, source = em_api.index_kline(secid, beg=beg)
        if df.empty:
            print(f"[index] {secid}: 无数据!")
            continue
        n = 0
        for _, r in df.iterrows():
            amt = None if pd.isna(r.get("amount")) else float(r["amount"])
            n += upsert_index(conn, secid, norm_date(r["date"]),
                              r["open"], r["high"], r["low"], r["close"],
                              r["volume"], amt, r["pct_chg"])
        print(f"[index] {secid}({source}): {n} rows "
              f"({df['date'].iloc[0]} ~ {df['date'].iloc[-1]})")


def fetch_pools(cfg, dates, force=False):
    conn = connect()
    done = set() if force else get_mood_dates(conn)
    todo = [d for d in dates if d not in done]
    if not todo:
        print("[pools] 无缺失日期, 跳过")
        return
    per_day = 4 * (cfg["fetch"]["sleep"] + 0.4)
    print(f"[pools] 待抓取 {len(todo)} 天 ({todo[0]} ~ {todo[-1]}), "
          f"预计 {len(todo) * per_day / 60:.0f} 分钟")
    ok = 0
    from datetime import date as _date, timedelta as _td
    recent_cutoff = (_date.today() - _td(days=45)).strftime("%Y-%m-%d")
    for i, d in enumerate(todo):
        try:
            st = em_api.ths_pool_stats(ymd(d))
            if not st and d >= recent_cutoff:
                st = em_api.em_pool_stats(ymd(d))  # 东财兜底(仅近月)
            if not st:
                continue  # 非交易日/超出数据源历史范围
            upsert_mood(conn, d, **st)
            ok += 1
            if ok % 20 == 0 or i == len(todo) - 1:
                jl = f"{st['jl_rate']:.0f}%" if st.get("jl_rate") is not None else "--"
                print(f"  [{i + 1}/{len(todo)}] 最新 {d}: 涨停{st['zt_count']} "
                      f"跌停{st['dt_count']} 炸板率{st['zb_rate']}% "
                      f"连板高{st['lb_max']} 接力率{jl}")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i + 1}/{len(todo)}] {d} 失败: {e}")
        time.sleep(cfg["fetch"]["sleep"])
    print(f"[pools] 完成, 成功 {ok} 天")


def _est_zt_count(df, date_iso):
    """从 daily(pct_chg) 估算涨停/跌停家数(按板块规则, 日期感知)
    创业板2020-08-24前为10%制, 之后为20%制"""
    import pandas as pd
    codes = df["ts_code"].astype(str)
    pct = pd.to_numeric(df["pct_chg"], errors="coerce")
    main = codes.str.startswith(("60", "00")) & ~codes.str.startswith(("68", "8", "4"))
    cyb = codes.str.startswith("30")
    kcb = codes.str.startswith("68")
    # 创业板阈值: 2020-08-24 改革前 10%, 之后 20%
    cyb_th = 9.8 if date_iso < "2020-08-24" else 19.8
    zt = (main & (pct >= 9.8)) | (cyb & (pct >= cyb_th)) | (kcb & (pct >= 19.8))
    dt = (main & (pct <= -9.8)) | (cyb & (pct <= -cyb_th)) | (kcb & (pct <= -19.8))
    return int(zt.sum()), int(dt.sum())


def backfill_zt_est(start="20120101"):
    """S2.2-B: 从 tushare daily 估算涨停/跌停家数(2012~2019, 不覆盖已有tushare数据)"""
    import tushare_api
    if not tushare_api.has_token():
        print("[zt-est] 无 token, 跳过")
        return
    conn = connect()
    s = str(start)
    iso_start = s if "-" in s else f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    dates = trading_dates(conn, load_cfg()["index"]["primary"], start=iso_start)
    have = {r[0] for r in conn.execute(
        "SELECT date FROM mood_raw WHERE zt_count IS NOT NULL")}
    todo = [d for d in dates if d not in have]
    if not todo:
        print("[zt-est] 无缺失日期")
        return
    print(f"[zt-est] {len(todo)} 天 ({todo[0]} ~ {todo[-1]}), 预计 {len(todo)*0.7/60:.0f} 分钟")
    t0, empty = time.time(), 0
    for i, d in enumerate(todo, 1):
        try:
            df = tushare_api.call("daily", "ts_code,pct_chg", trade_date=d.replace("-", ""))
            if df.empty:
                empty += 1
                if empty >= 5:
                    print("  ✗✗ 连续空数据, 中断")
                    break
                continue
            empty = 0
            zt, dt_ = _est_zt_count(df, d)
            if zt > 0 or dt_ > 0:
                upsert_mood(conn, d, zt_count=zt, dt_count=dt_, zt_source="est")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {d}: {e}")
            time.sleep(2)
        if i % 50 == 0:
            conn.commit()
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s")
    conn.commit()
    print(f"[zt-est] 完成 {time.time()-t0:.0f}s")


def fetch_breadth_today():
    """当日涨跌家数(乐咕口径, 可选; 历史序列无法获取, 仅最新一天)"""
    conn = connect()
    try:
        import akshare as ak
        df = ak.stock_market_activity_legu()
        kv = dict(zip(df.iloc[:, 0].astype(str), df.iloc[:, 1]))
        up, down, stat = kv.get("上涨"), kv.get("下跌"), kv.get("统计日期")
        if up is None or down is None or stat is None:
            print(f"[breadth] 字段不全, 跳过: {list(kv.items())[:6]}")
            return
        d = norm_date(stat)
        upsert_mood(conn, d, up_count=int(up), down_count=int(down))
        print(f"[breadth] {d} 上涨{int(up)} 下跌{int(down)}")
    except Exception as e:  # noqa: BLE001
        print(f"[breadth] 失败(不影响主流程, 未安装akshare也会跳过): {type(e).__name__}")


def breadth_from_tushare(start="20180102"):
    """S2.1-B: 从 tushare daily 回补涨跌家数历史(2018起)
    每日全市场~5000股的 pct_chg → 统计 >0(上涨)/<0(下跌)家数"""
    import tushare_api
    if not tushare_api.has_token():
        print("[breadth-ts] 无 token, 跳过")
        return
    conn = connect()
    s = str(start)
    iso_start = s if "-" in s else f"{s[:4]}-{s[4:6]}-{s[6:8]}"   # YYYYMMDD→ISO
    dates = trading_dates(conn, load_cfg()["index"]["primary"], start=iso_start)
    have = {r[0] for r in conn.execute(
        "SELECT date FROM mood_raw WHERE up_count IS NOT NULL")}
    todo = [d for d in dates if d not in have]
    if not todo:
        print("[breadth-ts] 无缺失日期")
        return
    print(f"[breadth-ts] {len(todo)} 天 ({todo[0]} ~ {todo[-1]}), 预计 {len(todo)*0.7/60:.0f} 分钟")
    t0, empty = time.time(), 0
    for i, d in enumerate(todo, 1):
        try:
            df = tushare_api.call("daily", "ts_code,pct_chg",
                             trade_date=d.replace("-", ""))
            if df.empty:
                empty += 1
                if empty >= 5:
                    print("  ✗✗ 连续空数据, 中断")
                    break
                continue
            empty = 0
            pct = df["pct_chg"]
            up = int((pct > 0).sum())
            down = int((pct < 0).sum())
            if up > 0 or down > 0:
                upsert_mood(conn, d, up_count=up, down_count=down)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {d}: {e}")
            time.sleep(2)
        if i % 50 == 0:
            conn.commit()
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s")
    conn.commit()
    print(f"[breadth-ts] 完成 {time.time()-t0:.0f}s (空{empty})")


def fetch_breadth_today_from_tushare():
    """S2.1-B: 主链增量——当日涨跌家数(tushare daily, 1请求/日)"""
    try:
        import tushare_api
        if not tushare_api.has_token():
            return
        d = time.strftime("%Y%m%d")
        df = tushare_api.call("daily", "ts_code,pct_chg", trade_date=d)
        if df.empty:
            return
        pct = df["pct_chg"]
        up, down = int((pct > 0).sum()), int((pct < 0).sum())
        if up > 0 or down > 0:
            conn = connect()
            upsert_mood(conn, time.strftime("%Y-%m-%d"), up_count=up, down_count=down)
            print(f"[breadth-ts] 当日: 上涨{up} 下跌{down}")
    except Exception as e:  # noqa: BLE001
        print(f"[breadth-ts] 当日失败(不影响主流程): {type(e).__name__}")


def fetch_sectors():
    """板块数据底座(S5): 清单/增量日线/资金流/当日涨停行业聚合; 首次自动全量回补"""
    try:
        import sectors
        sectors.update_daily()
    except Exception as e:  # noqa: BLE001
        print(f"[sectors] 失败(不阻塞主链): {type(e).__name__}: {e}")


def fetch_calendar():
    """同步交易日历(tushare, 可选): 过去2年+未来6个月 → calendar 表

    返回 True/False 供补全入口感知成败(主链每日调用忽略返回值, 行为不变)。
    """
    try:
        import tushare_api
        if not tushare_api.has_token():
            return True
        from datetime import date
        start = (date.today().replace(year=date.today().year - 2)).strftime("%Y%m%d")
        end = (date.today().replace(year=date.today().year + 1)).strftime("%Y1231")
        df = tushare_api.trade_cal(start, end)
        conn = connect()
        for _, r in df.iterrows():
            conn.execute(
                "INSERT INTO calendar(cal_date,is_open,pretrade_date) VALUES(?,?,?) "
                "ON CONFLICT(cal_date) DO UPDATE SET is_open=excluded.is_open,"
                "pretrade_date=excluded.pretrade_date",
                (r["cal_date"], int(r["is_open"]), r["pretrade_date"]))
        conn.commit()
        n_open = conn.execute("SELECT COUNT(*) FROM calendar WHERE is_open=1").fetchone()[0]
        print(f"[calendar] tushare 交易日历: {len(df)} 天({df['cal_date'].min()}~{df['cal_date'].max()}), 交易日{n_open}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[calendar] 失败(不影响主流程): {type(e).__name__}: {e}")
        return False


def fetch_tushare_mood_today():
    """tushare 当日涨停结构 upsert(主链内): 统一 mood_raw/板块聚合口径为 tushare,
    与 THS 互为校验; 失败不影响 THS 主源"""
    try:
        import tushare_api
        if not tushare_api.has_token():
            return
        import pandas as pd
        d = time.strftime("%Y%m%d")
        df = tushare_api.limit_list_d(d)
        if df.empty:
            return                       # 盘前/非交易日
        conn = connect()
        iso = time.strftime("%Y-%m-%d")
        prev = conn.execute(
            "SELECT zt_count FROM mood_raw WHERE date<? AND zt_count IS NOT NULL "
            "ORDER BY date DESC LIMIT 1", (iso,)).fetchone()
        u = df[df["limit"] == "U"]
        dn = df[df["limit"] == "D"]
        z = df[df["limit"] == "Z"]
        lb = pd.to_numeric(u.get("limit_times"), errors="coerce")
        lb2 = int((lb >= 2).sum()) if len(lb) else 0
        zb = len(z)
        zt = len(u)
        upsert_mood(conn, iso, zt_count=zt, dt_count=len(dn), zb_count=zb,
                    zb_rate=round(zb / (zt + zb) * 100, 1) if (zt + zb) else 0.0,
                    lb_max=int(lb.max()) if lb.notna().any() else 0, lb2_count=lb2,
                    jl_rate=round(lb2 / prev[0] * 100, 1) if prev and prev[0] else None,
                    yzt_count=prev[0] if prev else None)
        # 板块聚合(tushare industry)
        ind = {}
        def b(n):
            return ind.setdefault(n, {"zt": 0, "dt": 0, "zb": 0, "lb_max": 0, "lb2": 0})
        for _, r in u.iterrows():
            n = r.get("industry")
            if isinstance(n, str) and n:
                lv = r.get("limit_times")
                lv = float(lv) if lv == lv else 0
                bb = b(n)
                bb["zt"] += 1
                bb["lb_max"] = max(bb["lb_max"], int(lv))
                if lv >= 2:
                    bb["lb2"] += 1
        for _, r in dn.iterrows():
            n = r.get("industry")
            if isinstance(n, str) and n:
                b(n)["dt"] += 1
        for _, r in z.iterrows():
            n = r.get("industry")
            if isinstance(n, str) and n:
                b(n)["zb"] += 1
        import sectors as _sec
        _sec._write_pool_agg(conn, iso, ind)
        conn.commit()
        _sec.remap_pending(conn)
        print(f"[ts-mood] tushare当日口径: zt={zt} dt={len(dn)} zb={zb} 行业{len(ind)}")
    except Exception as e:  # noqa: BLE001
        print(f"[ts-mood] 失败(不影响主流程): {type(e).__name__}: {e}")


def fetch_margin():
    """融资融券汇总(tushare, 可选): 增量/首次回补(2020起) → margin_daily"""
    try:
        import tushare_api
        if not tushare_api.has_token():
            return
        conn = connect()
        last = conn.execute("SELECT MAX(date) FROM margin_daily").fetchone()[0]
        start = _next_ymd(last) if last else "20200101"
        today = time.strftime("%Y%m%d")
        total = 0
        while start <= today:
            df = tushare_api.margin(start, today)
            if df.empty:
                break
            for d, g in df.groupby("trade_date"):
                iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                tot = g.sum(numeric_only=True)
                sse = g[g["exchange_id"] == "SSE"]["rzrqye"].sum()
                szse = g[g["exchange_id"] == "SZSE"]["rzrqye"].sum()
                conn.execute(
                    "INSERT INTO margin_daily(date,rzye,rzmre,rqye,rzrqye,sse_rzrqye,szse_rzrqye) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
                    "rzye=excluded.rzye,rzmre=excluded.rzmre,rqye=excluded.rqye,"
                    "rzrqye=excluded.rzrqye,sse_rzrqye=excluded.sse_rzrqye,szse_rzrqye=excluded.szse_rzrqye",
                    (iso, float(tot["rzye"]), float(tot["rzmre"]),
                     float(tot["rqye"]), float(tot["rzrqye"]),
                     float(sse) if sse else None, float(szse) if szse else None))
                total += 1
            conn.commit()
            last_d = df["trade_date"].max()
            if last_d >= today:
                break
            start = _next_ymd(f"{last_d[:4]}-{last_d[4:6]}-{last_d[6:8]}")   # 分页续拉
        print(f"[margin] 两融汇总: 本次写入 {total} 日")
    except Exception as e:  # noqa: BLE001
        print(f"[margin] 失败(不影响主流程): {type(e).__name__}: {e}")


def _next_ymd(iso: str) -> str:
    from datetime import date, timedelta
    y, m, d = map(int, iso.split("-"))
    return (date(y, m, d) + timedelta(days=1)).strftime("%Y%m%d")


def backfill_tushare(start: str = "20200102"):
    """tushare limit_list_d 历史回补(2020年起):
    mood_raw 涨停结构(不覆盖已有THS段) + sector_pool_stat 行业聚合(@名落库, 自动remap)"""
    import tushare_api
    if not tushare_api.has_token():
        print("[ts-backfill] 无 token, 跳过")
        return
    conn = connect()
    cfg = load_cfg()
    s = str(start)
    iso_start = s if "-" in s else f"{s[:4]}-{s[4:6]}-{s[6:8]}"   # 支持 YYYYMMDD/ISO
    dates = trading_dates(conn, cfg["index"]["primary"], start=iso_start)
    have_mood = {r[0] for r in conn.execute(
        "SELECT date FROM mood_raw WHERE zt_count IS NOT NULL")}
    have_pool = {r[0] for r in conn.execute("SELECT DISTINCT date FROM sector_pool_stat")}
    todo = [d for d in dates if d not in have_mood or d not in have_pool]
    if not todo:
        print("[ts-backfill] 无缺失日期")
        return
    print(f"[ts-backfill] {len(todo)} 天 ({todo[0]} ~ {todo[-1]}), 预计 {len(todo) * 0.8 / 60:.0f} 分钟")
    import sectors as _sec
    import pandas as pd
    empty, prev_u, t0 = 0, None, time.time()
    for i, d in enumerate(todo, 1):
        try:
            df = tushare_api.limit_list_d(d.replace("-", ""))
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {d}: {e}")
            time.sleep(2)
            continue
        if df.empty:
            empty += 1
            if empty >= 5:
                print("  ✗✗ 连续5天空数据(超出tushare历史), 中断")
                break
            continue
        empty = 0
        u = df[df["limit"] == "U"]
        dn = df[df["limit"] == "D"]
        z = df[df["limit"] == "Z"]

        if d not in have_mood:
            zt, dt_n, zb = len(u), len(dn), len(z)
            lb = pd.to_numeric(u.get("limit_times"), errors="coerce")
            lb_max = int(lb.max()) if lb.notna().any() else 0
            lb2 = int((lb >= 2).sum()) if len(lb) else 0
            zb_rate = round(zb / (zt + zb) * 100, 1) if (zt + zb) else 0.0
            jl = round(lb2 / prev_u * 100, 1) if prev_u else None
            upsert_mood(conn, d, zt_count=zt, dt_count=dt_n, zb_count=zb,
                        zb_rate=zb_rate, lb_max=lb_max, lb2_count=lb2,
                        jl_rate=jl, yzt_count=prev_u)
        if d not in have_pool:
            def b(n):
                return ind.setdefault(n, {"zt": 0, "dt": 0, "zb": 0, "lb_max": 0, "lb2": 0})
            ind = {}
            for _, r in u.iterrows():
                if not r.get("industry"):
                    continue
                lv = r.get("limit_times")
                lv = float(lv) if lv == lv else 0
                bb = b(r["industry"])
                bb["zt"] += 1
                bb["lb_max"] = max(bb["lb_max"], int(lv))
                if lv >= 2:
                    bb["lb2"] += 1
            for _, r in dn.iterrows():
                if r.get("industry"):
                    b(r["industry"])["dt"] += 1
            for _, r in z.iterrows():
                if r.get("industry"):
                    b(r["industry"])["zb"] += 1
            _sec._write_pool_agg(conn, d, ind)
        prev_u = len(u)
        if i % 30 == 0:
            conn.commit()
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s 最新 {d}")
    conn.commit()
    _sec.remap_pending(conn)
    print(f"[ts-backfill] 完成 {time.time()-t0:.0f}s")


# ---------------------------------------------------------------- D1-2: 统一补全入口

# dataset: (标签, [(表, 计数列None=全行, 日期列)], 执行函数(start), 默认起始日)
# 增量度量=执行前后按日计数差集, 不改既有回补函数内部; 重复执行幂等(函数自身跳过已有日期)
BACKFILLS = {
    "zt_est": ("涨停家数·估算回补(2012-2019)", [("mood_raw", "zt_count", "date")],
               lambda start: backfill_zt_est(start), "20120101"),
    "zt_ts": ("涨停结构·tushare回补(2020起)",
              [("mood_raw", "zt_count", "date"), ("sector_pool_stat", None, "date")],
              lambda start: backfill_tushare(start), "20200102"),
    "breadth": ("涨跌家数·tushare回补(2018起)", [("mood_raw", "up_count", "date")],
                lambda start: breadth_from_tushare(start), "20180102"),
    "calendar": ("交易日历·刷新", [("calendar", None, "cal_date")],
                 lambda start: fetch_calendar(), "20240101"),
}


def _count_by_date(conn, table, col, date_col) -> dict:
    q = f"SELECT {date_col}, COUNT(*) FROM {table}"
    if col:
        q += f" WHERE {col} IS NOT NULL"
    q += " GROUP BY 1"
    return dict(conn.execute(q).fetchall())


def run_backfill(dataset, start=None) -> dict:
    """统一补全: 前置快照 → 执行 → 逐日增量度量 → 结构化结果(服务端落 data_backfill_log)

    start 省略时用该数据集的默认起始日(zt_est 2012/breadth 2018/zt_ts 2020)。
    """
    t0 = time.time()
    spec = BACKFILLS.get(dataset)
    if not spec:
        return {"dataset": dataset, "label": "", "status": "fail",
                "error": f"未知数据集: {dataset}", "days": [], "rows_written": 0,
                "date_from": None, "date_to": None, "duration_s": 0}
    label, targets, fn, def_start = spec
    iso_start = norm_date(start or def_start)
    try:
        from backup import snapshot
        snapshot("sentiment", "pre-backfill")
    except Exception as e:  # noqa: BLE001
        print(f"[backfill] pre-backfill快照失败(继续): {e}")
    conn = connect()
    try:
        pre = {t: _count_by_date(conn, t, c, dcol) for t, c, dcol in targets}
    finally:
        conn.close()
    err = None
    try:
        ret = fn(ymd(iso_start))
        if ret is False:            # 执行函数自报失败(内部吞错型, 如 calendar 网络失败)
            err = "执行函数自报失败(详见任务日志)"
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        print(f"[backfill] ✗ 执行异常: {err}")
    conn = connect()
    try:
        per_day = {}
        for t, c, dcol in targets:
            post = _count_by_date(conn, t, c, dcol)
            for d, n in post.items():
                delta = n - pre[t].get(d, 0)
                if delta > 0:
                    per_day[d] = per_day.get(d, 0) + delta
    finally:
        conn.close()
    days = [{"date": d, "rows": n} for d, n in sorted(per_day.items())]
    return {"dataset": dataset, "label": label,
            "status": "fail" if err else "ok",
            "date_from": days[0]["date"] if days else iso_start,
            "date_to": days[-1]["date"] if days else iso_start,
            "rows_written": sum(n for n in per_day.values()),
            "days": days, "duration_s": round(time.time() - t0, 1), "error": err}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="池回补起始日 YYYYMMDD (默认取 config pool_start)")
    ap.add_argument("--end", default=None, help="池回补结束日 YYYYMMDD (默认今天)")
    ap.add_argument("--index-start", default=None, help="指数起始日 (默认取 config index_start)")
    ap.add_argument("--force", action="store_true", help="忽略断点重新抓池")
    ap.add_argument("--no-breadth", action="store_true", help="跳过当日涨跌家数")
    ap.add_argument("--no-sectors", action="store_true", help="跳过板块数据(S5)")
    ap.add_argument("--ts-backfill", nargs="?", const="20200102", metavar="START",
                    help="tushare历史回补(涨跌停→mood_raw+板块聚合), 默认20200102起")
    ap.add_argument("--breadth-backfill", nargs="?", const="20180102", metavar="START",
                    help="涨跌家数历史回补(tushare daily_basic), 默认20180102起")
    ap.add_argument("--zt-est-backfill", nargs="?", const="20120101", metavar="START",
                    help="涨停/跌停家数估算回补(daily pct_chg), 默认20120101起")
    ap.add_argument("--backfill", metavar="DATASET",
                    help="D1-2 只跑数据补全(不走常规抓取链): " + "|".join(BACKFILLS))
    args = ap.parse_args()

    if args.backfill:
        r = run_backfill(args.backfill, args.start)   # start空 → 数据集默认起始日
        print("@@BACKFILL@@ " + json.dumps(r, ensure_ascii=False))
        return

    cfg = load_cfg()
    fetch_index(cfg, args.index_start or cfg["index_start"])

    conn = connect()
    start = args.start or cfg["pool_start"]
    start = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    end = args.end
    end = f"{end[:4]}-{end[4:6]}-{end[6:8]}" if end else None
    dates = trading_dates(conn, cfg["index"]["primary"], start=start, end=end)
    fetch_pools(cfg, dates, force=args.force)
    if not args.no_breadth:
        fetch_breadth_today()
        fetch_breadth_today_from_tushare()
    if not args.no_sectors:
        fetch_sectors()
    fetch_calendar()
    fetch_margin()
    fetch_tushare_mood_today()
    if args.ts_backfill:
        backfill_tushare(args.ts_backfill)
    if args.breadth_backfill:
        breadth_from_tushare(args.breadth_backfill)
    if args.zt_est_backfill:
        backfill_zt_est(args.zt_est_backfill)


if __name__ == "__main__":
    main()
