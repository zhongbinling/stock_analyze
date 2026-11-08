# -*- coding: utf-8 -*-
"""避雷针A1: 个股数据链 — stock.db(独立库) + 自选池模式

自选池语义: 用户查过的股票才全量落地(日线+复权因子)+挂日增,
避免全市场灌库。K线前复权在查询时计算(因子归一到最新, 不落库固化 —
未来分红除权后历史前复权自动更新)。

限频实测(2026-11-09): daily/adj_factor/stock_basic均为分钟档高频接口
(15连发×3全成功), 自选池日增无配额风险 — 与major_news(40/天)不同量级。

用法: python fetch_stock.py --add 000001.SZ   # 入池(全史)
      python fetch_stock.py --daily            # 池内日增(回看7日幂等)
      python fetch_stock.py --basic            # 全市场基础信息缓存(搜索源)
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tushare_api as T  # noqa: E402

STOCK_DB = BASE / "data" / "stock.db"
MAS = (5, 10, 20, 30, 60, 180, 250)          # A1: 七均线(可勾选)


def _conn(path=None):
    p = path or STOCK_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.executescript("""
      CREATE TABLE IF NOT EXISTS stock_basic(
        ts_code TEXT PRIMARY KEY, name TEXT, industry TEXT,
        list_date TEXT, updated_at TEXT);
      CREATE TABLE IF NOT EXISTS stock_pool(
        ts_code TEXT PRIMARY KEY, name TEXT, industry TEXT,
        added_at TEXT, last_daily TEXT);
      CREATE TABLE IF NOT EXISTS stock_daily(
        ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
        close REAL, vol REAL, amount REAL, adj_factor REAL,
        PRIMARY KEY(ts_code, trade_date));
    """)
    con.commit()
    return con


def refresh_basic(path=None) -> int:
    """全市场基础信息缓存(搜索源, ~5300行, 单call)"""
    try:
        df = T.call("stock_basic", "ts_code,name,industry,list_date",
                    list_status="L")
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 基础信息失败: {str(e)[:80]}")
        return -1
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con = _conn(path)
    con.executemany(
        "INSERT INTO stock_basic(ts_code,name,industry,list_date,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(ts_code) DO UPDATE SET "
        "name=excluded.name, industry=excluded.industry, "
        "list_date=excluded.list_date, updated_at=excluded.updated_at",
        [(str(r["ts_code"]), str(r["name"]), str(r.get("industry") or ""),
          str(r.get("list_date") or ""), now) for _, r in df.iterrows()])
    con.commit()
    con.close()
    print(f"[stock] 基础信息: {len(df)}只")
    return len(df)


def _iso(d) -> str:
    """trade_date归一ISO: 20261107→2026-11-07(已是ISO原样) — 全系统统一口径"""
    s = str(d or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def _norm_code(code: str) -> str:
    """输入归一: 000001 → 000001.SZ / 600000 → 600000.SH (六位数字智能补后缀)"""
    c = str(code or "").strip().upper()
    if "." in c:
        return c
    if len(c) == 6 and c.isdigit():
        suffix = {"6": ".SH", "5": ".SH", "9": ".SH",
                  "0": ".SZ", "3": ".SZ", "1": ".SZ", "2": ".SZ"}.get(c[0])
        return c + suffix if suffix else c
    return c


def add_stock(code: str, path=None) -> dict:
    """入池: 全史日线+复权因子(实测6000行/35年单call)。幂等(已入池只补增量)。"""
    ts_code = _norm_code(code)
    if not T.has_token():
        return {"ok": False, "msg": "无tushare token"}
    info = con = None
    try:
        info = T.call("stock_basic", "ts_code,name,industry",
                      ts_code=ts_code).head(1)
        if info.empty:
            return {"ok": False, "msg": f"未找到 {ts_code}"}
        name = str(info.iloc[0]["name"])
        industry = str(info.iloc[0].get("industry") or "")
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "msg": f"基础信息失败: {str(e)[:60]}"}
    try:
        rows = []
        for api, fill in (("daily", None), ("adj_factor", None)):
            if api == "daily":
                df = T.call("daily", "ts_code,trade_date,open,high,low,close,vol,amount",
                            ts_code=ts_code, start_date="19900000",
                            end_date=time.strftime("%Y%m%d"))
                rows = [(ts_code, _iso(r["trade_date"]),
                         float(r["open"]), float(r["high"]), float(r["low"]),
                         float(r["close"]), float(r["vol"] or 0),
                         float(r["amount"] or 0), None) for _, r in df.iterrows()]
            else:
                dfa = T.call("adj_factor", "ts_code,trade_date,adj_factor",
                             ts_code=ts_code, start_date="19900000",
                             end_date=time.strftime("%Y%m%d"))
                fac = {_iso(r["trade_date"]): float(r["adj_factor"]) for _, r in dfa.iterrows()}
                rows = [r[:8] + (fac.get(r[1]),) for r in rows]
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "msg": f"行情拉取失败: {str(e)[:60]}"}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con = _conn(path)
    con.executemany(
        "INSERT INTO stock_daily(ts_code,trade_date,open,high,low,close,"
        "vol,amount,adj_factor) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ts_code,trade_date) DO UPDATE SET "
        "open=excluded.open,high=excluded.high,low=excluded.low,"
        "close=excluded.close,vol=excluded.vol,amount=excluded.amount,"
        "adj_factor=excluded.adj_factor", rows)
    con.execute(
        "INSERT INTO stock_pool(ts_code,name,industry,added_at,last_daily) "
        "VALUES(?,?,?,?,?) ON CONFLICT(ts_code) DO UPDATE SET "
        "name=excluded.name, industry=excluded.industry, "
        "last_daily=excluded.last_daily",
        (ts_code, name, industry, now, now))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM stock_daily WHERE ts_code=?",
                    (ts_code,)).fetchone()[0]
    con.close()
    return {"ok": True, "msg": f"{name}({ts_code}) 入池: {n}日", "name": name}


def refresh_daily(lookback: int = 7, path=None) -> int:
    """池内日增: 回看N自然日幂等upsert(错过日自动补, 同fetch_etf语义)"""
    import datetime as dt
    if not T.has_token():
        return -1
    start = (dt.date.today() - dt.timedelta(days=lookback)).strftime("%Y%m%d")
    end = time.strftime("%Y%m%d")
    con = _conn(path)
    pool = [r[0] for r in con.execute("SELECT ts_code FROM stock_pool")]
    total, now = 0, time.strftime("%Y-%m-%d %H:%M:%S")
    for ts_code in pool:
        try:
            df = T.call("daily", "ts_code,trade_date,open,high,low,close,vol,amount",
                        ts_code=ts_code, start_date=start, end_date=end)
            dfa = T.call("adj_factor", "ts_code,trade_date,adj_factor",
                         ts_code=ts_code, start_date=start, end_date=end)
            fac = {_iso(r["trade_date"]): float(r["adj_factor"]) for _, r in dfa.iterrows()}
            rows = [(ts_code, _iso(r["trade_date"]), float(r["open"]),
                     float(r["high"]), float(r["low"]), float(r["close"]),
                     float(r["vol"] or 0), float(r["amount"] or 0),
                     fac.get(_iso(r["trade_date"]))) for _, r in df.iterrows()]
            con.executemany(
                "INSERT INTO stock_daily(ts_code,trade_date,open,high,low,close,"
                "vol,amount,adj_factor) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ts_code,trade_date) DO UPDATE SET "
                "close=excluded.close,vol=excluded.vol,amount=excluded.amount,"
                "adj_factor=excluded.adj_factor", rows)
            total += len(rows)
            con.execute("UPDATE stock_pool SET last_daily=? WHERE ts_code=?",
                        (now, ts_code))
        except Exception as e:                               # noqa: BLE001
            print(f"  ✗ {ts_code}: {str(e)[:60]}")
    con.commit()
    con.close()
    print(f"[stock] 日增: {len(pool)}只 +{total}行")
    return total


def search(q: str, limit: int = 15, path=None) -> list:
    """代码/名称前缀搜索(基础信息缓存; 缓存空时自动刷一次)"""
    con = _conn(path)
    n = con.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
    con.close()
    if n == 0:
        refresh_basic(path)
    con = _conn(path)
    q = str(q or "").strip()
    rows = con.execute(
        "SELECT ts_code, name, industry FROM stock_basic "
        "WHERE ts_code LIKE ? OR name LIKE ? ORDER BY ts_code LIMIT ?",
        (f"{q}%", f"%{q}%", limit)).fetchall()
    con.close()
    return [{"code": r[0], "name": r[1], "industry": r[2]} for r in rows]


def _ma(closes: list, n: int) -> list:
    """滚动均线(前n-1个None); closes含None(停牌日)时跳过计数"""
    out, s, cnt = [], 0.0, 0
    buf = []
    for c in closes:
        buf.append(c)
        s += c if c is not None else 0
        cnt += 1 if c is not None else 0
        if len(buf) > n:
            v = buf.pop(0)
            if v is not None:
                s -= v
                cnt -= 1
        out.append(round(s / n, 2) if cnt == n else None)
    return out


def kline(code: str, adj: str = "qfq", path=None) -> dict:
    """K线载荷: 复权(展示时计算)/七均线/量额。adj: qfq前复权|hfq后复权|none"""
    ts_code = _norm_code(code)
    con = _conn(path)
    rows = con.execute(
        "SELECT trade_date, open, high, low, close, vol, amount, adj_factor "
        "FROM stock_daily WHERE ts_code=? ORDER BY trade_date",
        (ts_code,)).fetchall()
    pool = con.execute("SELECT name FROM stock_pool WHERE ts_code=?",
                       (ts_code,)).fetchone()
    con.close()
    if not rows:
        return {"ok": False, "msg": f"{ts_code} 不在池(先add_stock)"}
    if adj == "qfq":
        base = max((r[7] for r in rows if r[7]), default=1.0) or 1.0
    elif adj == "hfq":
        base = 1.0
    closes_raw = [r[4] for r in rows]
    # qfq: factor/最新factor(归一); hfq: factor本身; none: 1
    def _fac(r):
        if adj == "none" or r[7] is None:
            return 1.0
        return r[7] / base if adj == "qfq" else r[7]
    k = [[round(r[1] * _fac(r), 2), round(r[4] * _fac(r), 2),
          round(r[3] * _fac(r), 2), round(r[2] * _fac(r), 2)] for r in rows]
    closes = [round(c * _fac(rows[i]), 2) for i, c in enumerate(closes_raw)]
    return {
        "ok": True, "code": ts_code, "name": pool[0] if pool else ts_code,
        "adj": adj, "n": len(rows),
        "dates": [r[0] for r in rows],
        "k": k, "closes": closes,
        "mas": {f"ma{n}": _ma(closes, n) for n in MAS},
        "vol": [round(r[5], 0) for r in rows],           # 手
        "amount": [round(r[6] / 1e5, 1) for r in rows],   # 千元→亿
        "last": {"date": rows[-1][0], "close": closes[-1]},
    }


def pool_list(path=None) -> list:
    con = _conn(path)
    rows = con.execute("SELECT ts_code, name, added_at, last_daily "
                       "FROM stock_pool ORDER BY added_at DESC").fetchall()
    con.close()
    return [{"code": r[0], "name": r[1], "added_at": r[2], "last_daily": r[3]}
            for r in rows]


def main():
    ap = argparse.ArgumentParser(description="避雷针个股数据(A1)")
    ap.add_argument("--add", default=None, help="入池股票代码")
    ap.add_argument("--daily", action="store_true", help="池内日增")
    ap.add_argument("--basic", action="store_true", help="全市场基础信息缓存")
    a = ap.parse_args()
    if a.add:
        print(add_stock(a.add))
    elif a.daily:
        refresh_daily()
    elif a.basic:
        refresh_basic()
    return 0


if __name__ == "__main__":
    sys.exit(main())
