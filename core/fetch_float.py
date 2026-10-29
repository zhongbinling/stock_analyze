# -*- coding: utf-8 -*-
"""D1-20 F1: 月度解禁 — share_float 事件层(float_raw) + 月度聚合(float_month)

口径(用户调研): 解禁市值 = float_share(万股) × 价格
  已发生(≤raw最新日): × 解禁日前收(raw_daily.pre_close, 每日一查)
  未发生(当月剩余/未来): × 最新收盘价(预判口径, 滚动更新)
全部入 sentiment.db; raw_daily 只读 ATTACH。

用法:
  python fetch_float.py                # 回补 2006-01 起全史(已聚合月跳过)
  python fetch_float.py --refresh      # 刷新近6月+未来4月(计划滚动修订)
"""
import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db import connect                                    # noqa: E402
import tushare_api as T                                   # noqa: E402

RAW_DB = BASE / "data" / "raw_daily.db"
# 实测(2026-10): 返回列为 share_type(解禁类型), 无 holder_type;
# start/end 过滤**公告日ann_date** — 解禁日可在未来, 按公告拉按解禁日分桶
FIELDS = "ts_code,ann_date,float_date,float_share,float_ratio,share_type"


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS float_raw(
      ann_date TEXT, ts_code TEXT, float_date TEXT, float_share REAL,
      float_ratio REAL, share_type TEXT,
      PRIMARY KEY(ts_code, float_date, share_type))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS float_month(
      month TEXT PRIMARY KEY, amount_yi REAL, n_events INTEGER, updated_at TEXT)""")
    conn.commit()


def _attach(conn):
    # db.connect()未开uri=True, URI文件名会被当字面路径 — 用普通路径ATTACH
    conn.execute(f"ATTACH DATABASE '{RAW_DB.as_posix()}' AS rawdb")


def _months(start_ym: str, end_ym: str) -> list:
    out, y, m = [], int(start_ym[:4]), int(start_ym[4:6])
    ey, em = int(end_ym[:4]), int(end_ym[4:6])
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _latest_raw_day(conn) -> str:
    """raw库最新交易日(ISO) — 已发生/未发生的分界"""
    try:
        d = conn.execute("SELECT MAX(trade_date) FROM rawdb.daily").fetchone()[0]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d else None
    except Exception:                                      # noqa: BLE001
        return None


def _preclose_map(conn, ymd: str) -> dict:
    """某解禁日的全A前收字典(解禁日前一交易日收盘 = 该日pre_close)"""
    try:
        return dict(conn.execute(
            "SELECT ts_code, pre_close FROM rawdb.daily WHERE trade_date=?"
            " AND pre_close IS NOT NULL", (ymd,)).fetchall())
    except Exception:                                      # noqa: BLE001
        return {}


def _latest_close_map(conn) -> tuple:
    """raw最新日全A收盘字典 + 该日(ISO); 无raw库→( {}, None)"""
    day = _latest_raw_day(conn)
    if not day:
        return {}, None
    m = dict(conn.execute(
        "SELECT ts_code, close FROM rawdb.daily WHERE trade_date=?"
        " AND close IS NOT NULL", (day.replace("-", ""),)).fetchall())
    return m, day


def aggregate_month(conn, month: str) -> dict:
    """月度聚合: 已发生=前收价, 未发生=最新价; 无价跳过计数"""
    lo, hi = f"{month}-01", f"{month}-31"
    evs = conn.execute(
        "SELECT ts_code, float_date, float_share FROM float_raw "
        "WHERE float_date>=? AND float_date<=? AND float_share>0", (lo, hi)).fetchall()
    latest_map, latest_day = _latest_close_map(conn)
    total, n, no_price, pre_days = 0.0, 0, 0, {}
    for ts, fd, share in evs:
        if fd <= (latest_day or "9999"):
            d = pre_days.get(fd)
            if d is None:
                d = _preclose_map(conn, fd.replace("-", ""))
                pre_days[fd] = d
            price = d.get(ts)
        else:
            price = latest_map.get(ts)
        if not price or price <= 0:
            no_price += 1
            continue
        total += share * price / 1e8      # 股×元 → 亿(实测2024-08≈2913亿✓)
        n += 1
    amount = round(total, 1) if n else 0.0
    conn.execute(
        "INSERT INTO float_month(month,amount_yi,n_events,updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(month) DO UPDATE SET "
        "amount_yi=excluded.amount_yi,n_events=excluded.n_events,"
        "updated_at=excluded.updated_at",
        (month, amount, n, time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    return {"month": month, "amount_yi": amount, "n_events": n,
            "no_price": no_price}


PAGE = 6000            # 实测: 单月公告量超6000会被截断, 必须翻页


def _iso(d) -> str:
    """YYYYMMDD → YYYY-MM-DD(share_float 返回紧凑日期)"""
    d = str(d or "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d


def _pull_month(conn, month: str, replace: bool = False) -> int:
    """拉取单月(按公告日ann_date) share_float → float_raw(翻页取全);
    replace=True 先清该公告月(修订防双计)。返回入库行数/-1失败。"""
    lo, hi = f"{month}-01", f"{month}-31"

    def _win(w_lo: str, w_hi: str) -> bool:
        """单窗口翻页拉全; False=撞offset上限需拆窗"""
        offset = 0
        while True:
            df = T.call("share_float", FIELDS, start_date=w_lo.replace("-", ""),
                        end_date=w_hi.replace("-", ""), limit=PAGE, offset=offset)
            frames.append(df)
            if len(df) < PAGE:
                return True
            offset += PAGE
            if offset >= 100000:
                return False

    frames = []

    def _rec(w_lo: str, w_hi: str) -> None:
        """递归拆窗: tushare offset上限10万 — 注册制后单月公告可超10万行
        (2021-10实测), 半月也不够(2023-10实测) → 撞限对半拆到单日"""
        import datetime as _dt
        if _win(w_lo, w_hi):
            return
        d1 = _dt.date.fromisoformat(w_lo)
        d2 = _dt.date.fromisoformat(w_hi)
        if (d2 - d1).days <= 0:
            raise RuntimeError(f"单日公告超10万行 {w_lo}")
        mid = d1 + (d2 - d1) / 2
        _rec(w_lo, mid.isoformat())
        _rec((mid + _dt.timedelta(days=1)).isoformat(), w_hi)

    try:
        _rec(lo, hi)
    except Exception as e:                                 # noqa: BLE001
        print(f"[float] {month} 拉取失败: {e}")
        return -1
    if replace:
        conn.execute("DELETE FROM float_raw WHERE ann_date>=? AND ann_date<=?",
                     (lo, hi))
        conn.commit()
    # 事件级聚合入库: 接口按持有人拆行(~6万行/月, 全史千万级) — 按
    # (code,解禁日,类型)Σ(同额持有人PK误并问题也随之消除), ~3千行/月
    ev = {}
    for df in frames:
        for _, r in df.iterrows():
            share = r.get("float_share")
            if share is None or float(share) <= 0:
                continue
            k = (str(r.get("ts_code") or ""), _iso(r.get("float_date")),
                 str(r.get("share_type") or ""))
            ann = _iso(r.get("ann_date"))
            d = ev.setdefault(k, {"share": 0.0, "ratio": 0.0, "ann": ann})
            d["share"] += float(share)
            d["ratio"] += float(r["float_ratio"] or 0)
            if ann and (not d["ann"] or ann < d["ann"]):   # 最早公告日
                d["ann"] = ann
    n = 0
    for (ts, fd, st), d in ev.items():
        conn.execute(
            "INSERT OR IGNORE INTO float_raw(ann_date,ts_code,float_date,"
            "float_share,float_ratio,share_type) VALUES(?,?,?,?,?,?)",
            (d["ann"], ts, fd, round(d["share"], 0),
             round(d["ratio"], 4) or None, st))
        n += 1
    conn.commit()
    return n


def _touched_months(conn, lo: str, hi: str) -> list:
    """该公告窗口入库事件涉及的解禁月(升序去重)"""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(float_date,1,7) FROM float_raw "
        "WHERE ann_date>=? AND ann_date<=? AND float_date IS NOT NULL "
        "ORDER BY 1", (lo, hi))]


def run(start: str = "20060101") -> bool:
    """全史回补: 已聚合月跳过, 每月一调用"""
    if not T.has_token():
        print("[float] 无 tushare token, 跳过")
        return True
    t0 = time.time()
    conn = connect()
    ensure_tables(conn)
    _attach(conn)
    done = {r[0] for r in conn.execute("SELECT month FROM float_month")}
    end_ym = time.strftime("%Y%m")
    months = _months(start, end_ym)                 # 公告月(已拉取月由raw判重)
    have_ann = {r[0] for r in conn.execute(
        "SELECT DISTINCT substr(ann_date,1,7) FROM float_raw")}
    months = [m for m in months if m not in have_ann]
    print(f"[float] {len(months)} 公告月待回补 (自 {start})")
    for i, m in enumerate(months, 1):
        n = _pull_month(conn, m)
        if n < 0:
            break                                  # 失败由人工重跑续传
        for fm in _touched_months(conn, f"{m}-01", f"{m}-31"):
            if fm not in done:
                r = aggregate_month(conn, fm)
                done.add(fm)
                if r["amount_yi"] > 0 and i % 12 == 0:
                    print(f"  [{i}/{len(months)}] 公告{m}→解禁{fm}: "
                          f"{r['amount_yi']}亿/{r['n_events']}条")
    conn.execute("DETACH DATABASE rawdb")
    conn.close()
    print(f"[float] 结束 {time.time()-t0:.0f}s")
    return True


def _shift_month(ym: str, delta: int) -> str:
    """ISO月位移(返回YYYY-MM — 曾返回紧凑月致float_month写入垃圾行)"""
    y, m = int(ym[:4]), int(ym[5:7]) if "-" in ym else int(ym[4:6])
    m += delta
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def refresh() -> bool:
    """近6月+未来4月: 修订滚动更新(replace防新旧双计)"""
    if not T.has_token():
        return True
    import time as _t
    now_ym = _t.strftime("%Y%m")
    conn = connect()
    ensure_tables(conn)
    _attach(conn)
    for m in _months(_shift_month(now_ym, -6), now_ym):     # 公告月滚动修订
        if _pull_month(conn, m, replace=True) < 0:
            continue
        for fm in _touched_months(conn, f"{m}-01", f"{m}-31"):
            aggregate_month(conn, fm)                        # 解禁月重聚合
    cur = time.strftime("%Y-%m")
    for i in range(5):                                       # 当月+未来4月(预判口径)
        aggregate_month(conn, _shift_month(cur, i))
    conn.execute("DETACH DATABASE rawdb")
    conn.close()
    return True


def main():
    ap = argparse.ArgumentParser(description="月度解禁回补/刷新(D1-20 F1)")
    ap.add_argument("--start", default="20060101")
    ap.add_argument("--refresh", action="store_true", help="近6+未来4月滚动刷新")
    a = ap.parse_args()
    refresh() if a.refresh else run(a.start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
