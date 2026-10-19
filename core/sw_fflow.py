# -*- coding: utf-8 -*-
"""SW层级主力资金流 (D1-6 P3): moneyflow(2007+) 逐日回补 → 成分聚合 → sw_fflow

- raw层(raw_daily.db): moneyflow 最小字段集(金额万元), 逐日全市场, 幂等/熔断/进度
- 聚合(sentiment): sw_fflow(level,index_code,industry_name,date,main_net)
  main_net = Σ成员((买超大+买大)-(卖超大-卖大))×10000  万元→元, 对齐EM口径

用法: python sw_fflow.py backfill|agg|run|check [--start 20070101]
"""
import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import tushare_api as T  # noqa: E402
from db import connect  # noqa: E402
from raw_daily import raw_connect  # noqa: E402
from sw_components import members_on  # noqa: E402

_FIELDS = "ts_code,trade_date,buy_elg_amount,sell_elg_amount,buy_big_amount,sell_big_amount"


def _iso(d):
    s = str(d)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def backfill(start="20070101") -> bool:
    """raw moneyflow 逐日回补(幂等: 已有日跳过; 空数据=更早无该数据, 跳过不计失败)"""
    raw = raw_connect()
    raw.execute("""CREATE TABLE IF NOT EXISTS moneyflow(
      ts_code TEXT, trade_date TEXT, buy_elg_amount REAL, sell_elg_amount REAL,
      buy_big_amount REAL, sell_big_amount REAL,
      PRIMARY KEY(ts_code, trade_date))""")
    raw.execute("CREATE INDEX IF NOT EXISTS idx_mf_tradedate ON moneyflow(trade_date)")
    sent = connect()
    days_all = [r[0].replace("-", "") for r in sent.execute(
        "SELECT DISTINCT date FROM index_daily WHERE code='1.000001' AND date>=? "
        "ORDER BY date", (_iso(start),))]
    sent.close()
    have = {r[0] for r in raw.execute("SELECT DISTINCT trade_date FROM moneyflow")}
    todo = [d for d in days_all if d not in have]
    print(f"[fflow-raw] {len(todo)}/{len(days_all)} 日待补 ({start} 起)")
    if not todo:
        raw.close()
        return True
    t0, err_streak, failed = time.time(), 0, 0
    for i, d in enumerate(todo, 1):
        ok_day = False
        try:
            df = T.call("moneyflow", _FIELDS, trade_date=d)
            err_streak = 0
            if df is not None and not df.empty:
                raw.executemany(
                    "INSERT OR REPLACE INTO moneyflow VALUES(?,?,?,?,?,?)",
                    [tuple(r) for r in df.itertuples(index=False)])
                raw.commit()
                ok_day = True
            elif df is not None:
                ok_day = True                      # 更早无数据
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {d}: {e}")
            err_streak += 1
            if err_streak >= 5:
                print("  ✗✗ 连续5次异常(多为网络), 中断")
                raw.close()
                return False
            time.sleep(2)
        if not ok_day:
            failed += 1
        if i % 50 == 0:
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s")
    n = raw.execute("SELECT COUNT(*) FROM moneyflow").fetchone()[0]
    raw.close()
    print(f"[fflow-raw] 结束 {time.time()-t0:.0f}s, 共 {n} 行"
          + (f", 失败 {failed} 日(重跑自愈)" if failed else ""))
    return True


def agg(start="20070101") -> bool:
    """raw × 成分 → sw_fflow(幂等按日跳过)"""
    sent = connect()
    sent.execute("""CREATE TABLE IF NOT EXISTS sw_fflow(
      level TEXT, index_code TEXT, industry_name TEXT, date TEXT, main_net REAL,
      PRIMARY KEY(level, index_code, date))""")
    meta = {ic: (lv, nm) for ic, lv, nm in sent.execute(
        "SELECT index_code, level, industry_name FROM sw_components "
        "GROUP BY index_code, level, industry_name")}
    raw = raw_connect()
    days = [r[0] for r in raw.execute(
        "SELECT DISTINCT trade_date FROM moneyflow WHERE trade_date>=? ORDER BY trade_date",
        (str(start).replace("-", ""),))]
    done = {r[0] for r in sent.execute("SELECT DISTINCT date FROM sw_fflow")}
    todo = [d for d in days if _iso(d) not in done]
    print(f"[fflow-agg] {len(todo)}/{len(days)} 日待聚合")
    if not todo:
        sent.close(); raw.close()
        return True
    t0 = time.time()
    for i, d in enumerate(todo, 1):
        comps = members_on(d, conn=sent)
        inv = {}
        for idx, ms in comps.items():
            for m in ms:
                inv.setdefault(m, []).append(idx)
        agg_ = {}
        for ts, be, se, bb, sb in raw.execute(
                "SELECT ts_code, buy_elg_amount, sell_elg_amount, buy_big_amount, "
                "sell_big_amount FROM moneyflow WHERE trade_date=?", (d,)):
            for idx in inv.get(ts, ()):
                v = ((be or 0) + (bb or 0) - (se or 0) - (sb or 0)) * 10000   # 万元→元
                agg_[idx] = agg_.get(idx, 0.0) + v
        d_iso = _iso(d)
        sent.executemany(
            "INSERT OR REPLACE INTO sw_fflow VALUES(?,?,?,?,?)",
            [(meta[idx][0], idx, meta[idx][1], d_iso, round(v, 0))
             for idx, v in agg_.items() if idx in meta])
        if i % 50 == 0:
            sent.commit()
            print(f"  [{i}/{len(todo)}] {time.time()-t0:.0f}s")
    sent.commit()
    raw.close()
    sent.close()
    print(f"[fflow-agg] 结束 {time.time()-t0:.0f}s")
    return True


def run(start="20070101") -> bool:
    return backfill(start) and agg(start)


def check():
    raw = raw_connect()
    try:
        n, nd, lo, hi = raw.execute(
            "SELECT COUNT(*), COUNT(DISTINCT trade_date), MIN(trade_date), MAX(trade_date) "
            "FROM moneyflow").fetchone()
        print(f"[raw] moneyflow: {n} 行 / {nd} 日 ({lo} ~ {hi})")
    except Exception:
        print("[raw] moneyflow: 未建")
    raw.close()
    sent = connect()
    for lv in ("L1", "L2", "L3"):
        r = sent.execute("SELECT COUNT(*), COUNT(DISTINCT date) FROM sw_fflow WHERE level=?",
                         (lv,)).fetchone()
        if r[0]:
            print(f"[agg] sw_fflow {lv}: {r[0]} 行 / {r[1]} 日")
    sent.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "agg", "run", "check"])
    ap.add_argument("--start", default="20070101")
    args = ap.parse_args()
    if args.cmd == "backfill":
        sys.exit(0 if backfill(args.start) else 1)
    if args.cmd == "agg":
        sys.exit(0 if agg(args.start) else 1)
    if args.cmd == "run":
        sys.exit(0 if run(args.start) else 1)
    check()


if __name__ == "__main__":
    main()
