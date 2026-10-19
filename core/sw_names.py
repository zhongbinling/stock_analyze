# -*- coding: utf-8 -*-
"""个股名称历史区间表 (D1-6 P9): tushare namechange → name_hist → ST 判定底座

namechange 实测(2026-10-19): 区间式(ts_code,name,start_date,end_date; end空=至今),
按公告日过滤, 全年~450行 → 按年拉1990-2026全量区间, PK去重幂等。
ST 判定 = 名称含 'ST'(覆盖 ST/*ST/S*ST)。

用法: python sw_names.py build|check
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


def build(year_from=1990, year_to=None) -> bool:
    """按年拉 namechange → name_hist(区间式, PK(ts_code,start_date) 幂等)"""
    conn = connect()
    conn.execute("""CREATE TABLE IF NOT EXISTS name_hist(
      ts_code TEXT, name TEXT, start_date TEXT, end_date TEXT,
      PRIMARY KEY(ts_code, start_date))""")
    year_to = year_to or time.localtime().tm_year
    total, t0 = 0, time.time()
    for y in range(year_from, year_to + 1):
        try:
            df = T.call("namechange", "",
                        start_date=f"{y}0101", end_date=f"{y}1231")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {y}: {e}")
            time.sleep(2)
            continue
        if df is None or df.empty:
            continue
        rows = [(r["ts_code"], str(r["name"]), str(r["start_date"]),
                 None if not str(r["end_date"] or "") or str(r["end_date"]) == "nan"
                 else str(r["end_date"]))
                for _, r in df.iterrows()]
        conn.executemany("INSERT OR REPLACE INTO name_hist VALUES(?,?,?,?)", rows)
        conn.commit()
        total += len(rows)
    n = conn.execute("SELECT COUNT(*) FROM name_hist").fetchone()[0]
    n_st = conn.execute("SELECT COUNT(*) FROM name_hist WHERE name LIKE '%ST%'").fetchone()[0]
    conn.close()
    print(f"[names] 拉取{total}条 → name_hist {n} 区间 (含ST名称 {n_st}, {time.time()-t0:.0f}s)")
    return True


class NameTimeline:
    """内存名称时间线: {ts: [(start,end,name)按start升序]}; name_at 单调日期均摊O(1)"""

    def __init__(self, conn=None):
        import sqlite3
        own = conn is None
        conn = conn or connect()
        self.tl = {}
        try:
            for ts, name, s, e in conn.execute(
                    "SELECT ts_code, name, start_date, end_date FROM name_hist"):
                self.tl.setdefault(ts, []).append((s, e, name))
        except sqlite3.Error:
            pass                    # name_hist 未建(sw_names build 前) → 无ST感知, 走原口径
        for ts in self.tl:
            self.tl[ts].sort(key=lambda x: x[0])
        if own:
            conn.close()
        self._pos = {}

    def name_at(self, ts, d_ymd: str):
        """ts 在 d_ymd 的名称(区间[start,end); 无记录返回None; d须单调不减)"""
        ivs = self.tl.get(ts)
        if not ivs:
            return None
        i = self._pos.get(ts, 0)
        if i >= len(ivs):
            i = len(ivs) - 1
        while i + 1 < len(ivs) and ivs[i + 1][0] <= d_ymd:
            i += 1
        while i > 0 and ivs[i][0] > d_ymd:
            i -= 1
        self._pos[ts] = i
        s, e, name = ivs[i]
        if s <= d_ymd and (not e or d_ymd <= e):
            return name
        return None


def check():
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*), COUNT(DISTINCT ts_code) FROM name_hist").fetchone()
        st = conn.execute("SELECT COUNT(*) FROM name_hist WHERE name LIKE '%ST%'").fetchone()[0]
        lo = conn.execute("SELECT MIN(start_date) FROM name_hist").fetchone()[0]
        print(f"[names] name_hist: {n[0]} 区间 / {n[1]} 股 (含ST {st}), 最早 {lo}")
    except Exception:
        print("[names] name_hist 未建")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "check"])
    args = ap.parse_args()
    if args.cmd == "build":
        sys.exit(0 if build() else 1)
    check()


if __name__ == "__main__":
    main()
