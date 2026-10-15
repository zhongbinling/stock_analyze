# -*- coding: utf-8 -*-
"""每日数据采集: 指数日线 + 涨停/跌停/炸板/昨日涨停池 → SQLite

用法:
  python fetch_daily.py                    # 指数全量刷新 + 自 pool_start 起回补缺失的池数据
  python fetch_daily.py --start 20260901   # 只回补指定起始日之后的池数据
  python fetch_daily.py --force            # 忽略断点, 重新抓取(覆盖)
首次回补约 300 个交易日需 15~30 分钟, 支持断点续跑(重跑自动跳过已有日期)。
"""
import argparse
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


def fetch_sectors():
    """板块数据底座(S5): 清单/增量日线/资金流/当日涨停行业聚合; 首次自动全量回补"""
    try:
        import sectors
        sectors.update_daily()
    except Exception as e:  # noqa: BLE001
        print(f"[sectors] 失败(不阻塞主链): {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="池回补起始日 YYYYMMDD (默认取 config pool_start)")
    ap.add_argument("--end", default=None, help="池回补结束日 YYYYMMDD (默认今天)")
    ap.add_argument("--index-start", default=None, help="指数起始日 (默认取 config index_start)")
    ap.add_argument("--force", action="store_true", help="忽略断点重新抓池")
    ap.add_argument("--no-breadth", action="store_true", help="跳过当日涨跌家数")
    ap.add_argument("--no-sectors", action="store_true", help="跳过板块数据(S5)")
    args = ap.parse_args()

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
    if not args.no_sectors:
        fetch_sectors()


if __name__ == "__main__":
    main()
