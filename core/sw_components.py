# -*- coding: utf-8 -*-
"""申万历史成分区间表 (D1-6 P0): SW2021 L1/L2/L3 成分股 in/out 区间落地

用途: 板块级历史指标(涨跌家数/资金流/涨停结构)的成分还原底座——
      区间查询还原任意交易日真实成分, 杜绝"用最新成分回算历史"的未来函数偏差。

语义: 每行一条成分区间 [in_date, out_date) 半开; out_date 为空 = 在册(is_new=Y)。
注意: 与 sw1_map(东财BK→SW, BK行用)方向不同, 并存。

用法:
  python sw_components.py build   # 全量构建(L1+L2+L3, ~511次调用≈6分钟)
  python sw_components.py show    # 各层级统计
  python sw_components.py check   # 口径自洽校验
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

LEVELS = ("L1", "L2", "L3")


def _norm_date(s) -> str:
    s = str(s or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and "-" not in s else s


def _clean(v) -> str | None:
    """pandas NaN/None → None; 其余转字符串"""
    if v is None or (isinstance(v, float) and v != v) or str(v) in ("", "nan", "None"):
        return None
    return str(v)


def _ymd(s) -> str:
    return _norm_date(s).replace("-", "")


def build(levels=LEVELS):
    """index_classify → 逐指数 index_member 区间记录入库(PK幂等可重跑)"""
    conn = connect()
    conn.execute("""CREATE TABLE IF NOT EXISTS sw_components(
      index_code TEXT, level TEXT, industry_name TEXT, con_code TEXT,
      in_date TEXT, out_date TEXT, is_new TEXT,
      PRIMARY KEY(index_code, con_code, in_date))""")
    total, t0 = 0, time.time()
    for lv in levels:
        cls = T.call("index_classify", "", level=lv, src="SW2021")
        names = dict(zip(cls["index_code"], cls["industry_name"]))
        print(f"[sw-comp] {lv}: {len(cls)} 个指数")
        for i, idx in enumerate(cls["index_code"], 1):
            df = T.call("index_member", "", index_code=idx)
            if df is None or df.empty:
                continue
            rows = [(idx, lv, names.get(idx, ""), r["con_code"],
                     _clean(r["in_date"]), _clean(r["out_date"]),
                     _clean(r.get("is_new")))
                    for _, r in df.iterrows()]
            conn.executemany(
                "INSERT OR REPLACE INTO sw_components VALUES(?,?,?,?,?,?,?)", rows)
            total += len(rows)
            if i % 40 == 0:
                conn.commit()
                print(f"  [{i}/{len(cls)}] {time.time()-t0:.0f}s 累计{total}区间")
        conn.commit()
    conn.commit()
    conn.close()
    print(f"[sw-comp] 完成: {total} 条成分区间 ({time.time()-t0:.0f}s)")


def members_on(date, index_code=None, level=None, conn=None) -> dict:
    """还原某交易日的成分: {index_code: set(con_code)}; date 支持 YYYYMMDD/ISO

    区间语义: in_date <= d < out_date(out为空=在册)
    """
    own = conn is None
    conn = conn or connect()
    d = _ymd(date)
    sql = ("SELECT index_code, con_code FROM sw_components "
           "WHERE in_date<=? AND (out_date IS NULL OR out_date>?)")
    args = [d, d]
    if index_code:
        sql += " AND index_code=?"
        args.append(index_code)
    if level:
        sql += " AND level=?"
        args.append(level)
    out = {}
    for idx, con in conn.execute(sql, args):
        out.setdefault(idx, set()).add(con)
    if own:
        conn.close()
    return out


def validate(conn=None) -> bool:
    """口径自洽校验: 在册数==最新交易日还原数 / 跨行业同日重叠 / 同日切换边界统计"""
    own = conn is None
    conn = conn or connect()
    ok = True
    for lv in LEVELS:
        n_idx = conn.execute(
            "SELECT COUNT(DISTINCT index_code) FROM sw_components WHERE level=?",
            (lv,)).fetchone()[0]
        n_new = conn.execute(
            "SELECT COUNT(*) FROM sw_components WHERE level=? AND is_new='Y'", (lv,)).fetchone()[0]
        today = time.strftime("%Y%m%d")
        live = sum(len(v) for v in members_on(today, level=lv, conn=conn).values())
        match = "✓" if n_new == live else "✗"
        ok &= n_new == live
        print(f"[check] {lv}: {n_idx} 指数, 在册{n_new} == 今日还原{live} {match}")
    # 跨同层级行业同日重叠(同层一股应只属一业, L3偶有过渡期重叠)
    for lv in LEVELS:
        rows = conn.execute(
            "SELECT con_code, COUNT(DISTINCT index_code) c FROM sw_components "
            "WHERE level=? AND is_new='Y' GROUP BY con_code HAVING c>1 LIMIT 3", (lv,)).fetchall()
        if rows:
            print(f"[check] {lv} 在册跨行业重叠样本: {[(r[0], r[1]) for r in rows]}")
    # 同日切换边界: 前区间out_date == 后区间in_date(半开约定下不双计)
    n_switch = conn.execute(
        "SELECT COUNT(*) FROM sw_components a JOIN sw_components b "
        "ON a.con_code=b.con_code AND a.level=b.level AND a.out_date=b.in_date").fetchone()[0]
    print(f"[check] 同日切换样本(前out==后in, 半开区间不双计): {n_switch}")
    if own:
        conn.close()
    return ok


def show(conn=None):
    own = conn is None
    conn = conn or connect()
    for lv in LEVELS:
        r = conn.execute(
            "SELECT COUNT(DISTINCT index_code), COUNT(*), COUNT(DISTINCT con_code), "
            "MIN(in_date), MAX(in_date) FROM sw_components WHERE level=?", (lv,)).fetchone()
        if r[0]:
            print(f"  {lv}: {r[0]:>3} 指数 | {r[1]:>6} 区间 | {r[2]:>5} 个股 | {r[3]} ~ {r[4]}")
    if own:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "show", "check"])
    args = ap.parse_args()
    if args.cmd == "build":
        build()
    elif args.cmd == "show":
        show()
    else:
        sys.exit(0 if validate() else 1)


if __name__ == "__main__":
    main()
