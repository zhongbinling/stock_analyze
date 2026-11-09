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
      CREATE TABLE IF NOT EXISTS stock_finance(          -- A2: 季度财务(fina_indicator)
        ts_code TEXT, end_date TEXT, ann_date TEXT,
        profit_dedt REAL, netprofit_yoy REAL, or_yoy REAL,
        grossprofit_margin REAL, roe REAL, debt_to_assets REAL,
        PRIMARY KEY(ts_code, end_date));
      CREATE TABLE IF NOT EXISTS stock_holders(          -- A2: 十大流通股东
        ts_code TEXT, end_date TEXT, holder_name TEXT,
        hold_amount REAL, ratio REAL,
        PRIMARY KEY(ts_code, end_date, holder_name));
      CREATE TABLE IF NOT EXISTS stock_holdernum(        -- A2: 股东户数
        ts_code TEXT, end_date TEXT, holder_num REAL,
        PRIMARY KEY(ts_code, end_date));
      CREATE TABLE IF NOT EXISTS stock_margin(           -- A2: 两融明细
        ts_code TEXT, trade_date TEXT, rzye REAL, rqye REAL,
        rzmre REAL, rqyl REAL,
        PRIMARY KEY(ts_code, trade_date));
      CREATE TABLE IF NOT EXISTS stock_val(              -- A2: 每日估值(daily_basic)
        ts_code TEXT, trade_date TEXT, pe_ttm REAL, pb REAL,
        total_mv REAL, circ_mv REAL,
        PRIMARY KEY(ts_code, trade_date));
      CREATE TABLE IF NOT EXISTS stock_float(            -- A3: 个股解禁(share_float)
        ts_code TEXT, ann_date TEXT, float_date TEXT,
        float_share REAL, float_ratio REAL, holder_name TEXT,
        PRIMARY KEY(ts_code, float_date, holder_name));
      CREATE TABLE IF NOT EXISTS stock_events(           -- A3: 个股大事件(统一归一)
        ts_code TEXT, ev_date TEXT, ev_type TEXT,
        title TEXT, detail TEXT, risk TEXT,
        PRIMARY KEY(ts_code, ev_date, ev_type, title));
      CREATE TABLE IF NOT EXISTS stock_pledge(           -- A3: 质押比例(状态类, 不入K线事件)
        ts_code TEXT, end_date TEXT, pledge_ratio REAL,
        PRIMARY KEY(ts_code, end_date));
      CREATE TABLE IF NOT EXISTS stock_bs(               -- A4: 资产负债表关键列
        ts_code TEXT, end_date TEXT, goodwill REAL,
        total_assets REAL, total_liab REAL,
        PRIMARY KEY(ts_code, end_date));
      CREATE TABLE IF NOT EXISTS stock_ai(               -- A4: AI财报分析缓存(一股一行)
        ts_code TEXT PRIMARY KEY, end_date TEXT, ann_date TEXT,
        rule_grade TEXT, rule_score REAL, rules_json TEXT,
        ai_json TEXT, model TEXT, analyzed_at TEXT);
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


def _ann(v) -> str | None:
    """ann_date归一ISO并滤脏值(pandas NaN→str'nan')"""
    s = str(v or "").strip()
    return _iso(s) if s and s not in ("nan", "None") else None


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


# ══════════ A2: 财务/股东/两融/估值 ══════════

def _f(v):
    try:
        x = float(v)
        return x if x == x else None            # NaN→None
    except (TypeError, ValueError):
        return None


def _start(lookback: int = None) -> str:
    """全史或近N自然日起始(A5: 夜间增量用lookback, 手动全量None)"""
    if not lookback:
        return "19900000"
    import datetime as _dt
    return (_dt.date.today() - _dt.timedelta(days=lookback)).strftime("%Y%m%d")


def refresh_finance(code: str, path=None, lookback: int = None) -> int:
    """季度财务(fina_indicator全史单call): 扣非/增速/毛利率/ROE/负债率"""
    ts_code = _norm_code(code)
    try:
        df = T.call("fina_indicator",
                    "ts_code,end_date,ann_date,profit_dedt,netprofit_yoy,or_yoy,"
                    "grossprofit_margin,roe,debt_to_assets",
                    ts_code=ts_code, start_date=_start(lookback),
                    end_date=time.strftime("%Y%m%d"))
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 财务失败 {ts_code}: {str(e)[:60]}")
        return -1
    rows = [(ts_code, _iso(r["end_date"]), _ann(r.get("ann_date")),
             _f(r.get("profit_dedt")), _f(r.get("netprofit_yoy")),
             _f(r.get("or_yoy")), _f(r.get("grossprofit_margin")),
             _f(r.get("roe")), _f(r.get("debt_to_assets")))
            for _, r in df.iterrows()]
    con = _conn(path)
    con.executemany(
        "INSERT INTO stock_finance VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ts_code,end_date) DO UPDATE SET ann_date=excluded.ann_date,"
        "profit_dedt=excluded.profit_dedt,netprofit_yoy=excluded.netprofit_yoy,"
        "or_yoy=excluded.or_yoy,grossprofit_margin=excluded.grossprofit_margin,"
        "roe=excluded.roe,debt_to_assets=excluded.debt_to_assets", rows)
    con.commit()
    con.close()
    return len(rows)


def refresh_holders(code: str, path=None) -> int:
    """十大流通股东(top10_floatholders全史) + 股东户数(stk_holdernumber)"""
    ts_code = _norm_code(code)
    rows, hnums = [], []
    try:
        df = T.call("top10_floatholders", "ts_code,end_date,holder_name,hold_amount,hold_ratio",
                    ts_code=ts_code, start_date="19900000",
                    end_date=time.strftime("%Y%m%d"))
        rows = [(ts_code, _iso(r["end_date"]), str(r["holder_name"]).strip(),
                 _f(r.get("hold_amount")), _f(r.get("hold_ratio")))
                for _, r in df.iterrows()]
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 十大流通失败 {ts_code}: {str(e)[:60]}")
    try:
        dfn = T.call("stk_holdernumber", "ts_code,end_date,holder_num",
                     ts_code=ts_code, start_date="19900000",
                     end_date=time.strftime("%Y%m%d"))
        hnums = [(ts_code, _iso(r["end_date"]), _f(r.get("holder_num")))
                 for _, r in dfn.iterrows()]
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 股东户数失败 {ts_code}: {str(e)[:60]}")
    con = _conn(path)
    if rows:
        con.executemany("INSERT INTO stock_holders VALUES(?,?,?,?,?) "
                        "ON CONFLICT(ts_code,end_date,holder_name) DO UPDATE SET "
                        "hold_amount=excluded.hold_amount,ratio=excluded.ratio", rows)
    if hnums:
        con.executemany("INSERT INTO stock_holdernum VALUES(?,?,?) "
                        "ON CONFLICT(ts_code,end_date) DO UPDATE SET "
                        "holder_num=excluded.holder_num", hnums)
    con.commit()
    con.close()
    return len(rows) + len(hnums)


def refresh_margin(code: str, path=None, lookback: int = None) -> int:
    """两融明细(margin_detail, 分页防行数上限; 夜间增量lookback)"""
    ts_code = _norm_code(code)
    frames, offset = [], 0
    try:
        while True:
            df = T.call("margin_detail", "ts_code,trade_date,rzye,rqye,rzmre,rqyl",
                        ts_code=ts_code, start_date=_start(lookback),
                        end_date=time.strftime("%Y%m%d"),
                        limit=4000, offset=offset)
            frames.append(df)
            if len(df) < 4000:
                break
            offset += 4000
            if offset >= 40000:
                break
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 两融失败 {ts_code}: {str(e)[:60]}")
        return -1
    rows = [(ts_code, _iso(r["trade_date"]), _f(r.get("rzye")), _f(r.get("rqye")),
             _f(r.get("rzmre")), _f(r.get("rqyl")))
            for df in frames for _, r in df.iterrows()]
    con = _conn(path)
    con.executemany("INSERT INTO stock_margin VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(ts_code,trade_date) DO UPDATE SET "
                    "rzye=excluded.rzye,rqye=excluded.rqye,"
                    "rzmre=excluded.rzmre,rqyl=excluded.rqyl", rows)
    con.commit()
    con.close()
    return len(rows)


def refresh_val(code: str, path=None, lookback: int = None) -> int:
    """每日估值(daily_basic): PE_TTM/PB/总市值/流通市值"""
    ts_code = _norm_code(code)
    try:
        df = T.call("daily_basic", "ts_code,trade_date,pe_ttm,pb,total_mv,circ_mv",
                    ts_code=ts_code, start_date=_start(lookback),
                    end_date=time.strftime("%Y%m%d"))
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 估值失败 {ts_code}: {str(e)[:60]}")
        return -1
    rows = [(ts_code, _iso(r["trade_date"]), _f(r.get("pe_ttm")), _f(r.get("pb")),
             _f(r.get("total_mv")), _f(r.get("circ_mv"))) for _, r in df.iterrows()]
    con = _conn(path)
    con.executemany("INSERT INTO stock_val VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(ts_code,trade_date) DO UPDATE SET "
                    "pe_ttm=excluded.pe_ttm,pb=excluded.pb,"
                    "total_mv=excluded.total_mv,circ_mv=excluded.circ_mv", rows)
    con.commit()
    con.close()
    return len(rows)


HOLDER_TAGS = (  # (关键词, 标签) — 股东类型启发式标记(避雷可读性)
    (("香港中央结算", "陆股通"), "北向"),
    (("中央汇金", "中国证券金融", "社保基金", "国家集成电路"), "国家队"),
    (("基金", "资管", "集合理财"), "公募/资管"),
    (("私募", "资产管理"), "私募"),
    (("质押", "银行", "信托"), "金融机构"),
)


def _holder_tag(name: str) -> str:
    for keys, tag in HOLDER_TAGS:
        if any(k in name for k in keys):
            return tag
    return "其他"


def holders_payload(code: str, path=None) -> dict:
    """模块②: 期末列表(倒序) + 各期十大流通(占比降序+类型标记+环比变化) + 户数"""
    ts_code = _norm_code(code)
    con = _conn(path)
    periods = [r[0] for r in con.execute(
        "SELECT DISTINCT end_date FROM stock_holders WHERE ts_code=? "
        "ORDER BY end_date DESC", (ts_code,))]
    prev_ratio = {}                                 # 上期占比(算环比)
    allp = {}
    for end_date in periods:
        allp[end_date] = con.execute(
            "SELECT holder_name, hold_amount, ratio FROM stock_holders "
            "WHERE ts_code=? AND end_date=? ORDER BY ratio DESC",
            (ts_code, end_date)).fetchall()
    hnum = {r[0]: r[1] for r in con.execute(
        "SELECT end_date, holder_num FROM stock_holdernum WHERE ts_code=?",
        (ts_code,))}
    con.close()
    out = []
    for end_date in reversed(periods):        # 旧→新累积prev_ratio(环比=较上期)
        rows = []
        for name, amt, ratio in allp[end_date]:
            chg = None
            p = prev_ratio.get(name)
            if p is not None and ratio is not None:
                chg = round(ratio - p, 2)
            rows.append({"name": name, "amount": amt, "ratio": ratio,
                         "chg": chg, "tag": _holder_tag(name)})
            prev_ratio[name] = ratio
        out.append({"end_date": end_date, "holder_num": hnum.get(end_date),
                    "holders": rows})
    out.reverse()                              # 输出恢复倒序(最新在前)
    return {"ok": True, "code": ts_code, "periods": periods, "data": out}


def margin_payload(code: str, path=None) -> dict:
    """模块③: 融资/融券余额(双轴) + 融资余额占流通市值比%(join stock_val)"""
    ts_code = _norm_code(code)
    con = _conn(path)
    m = con.execute("SELECT trade_date, rzye, rqye FROM stock_margin "
                    "WHERE ts_code=? ORDER BY trade_date", (ts_code,)).fetchall()
    val = {r[0]: r[1] for r in con.execute(
        "SELECT trade_date, circ_mv FROM stock_val WHERE ts_code=?", (ts_code,))}
    con.close()
    # 单位实测(2026-11-09茅台): margin_detail.rzye为**元**(150亿=1.5e10),
    # daily_basic.circ_mv为万元 → 亿=元/1e8; 占比=rz元/(circ_mv万元×1e4)×100
    dates, rzye, rqye, ratio = [], [], [], []
    last_mv = None
    for d, rz, rq in m:
        if d in val and val[d]:
            last_mv = val[d]
        dates.append(d)
        rzye.append(round(rz / 1e8, 2) if rz else None)      # 元→亿
        rqye.append(round(rq / 1e8, 2) if rq else None)
        ratio.append(round(rz / (last_mv * 1e4) * 100, 2)
                     if rz and last_mv else None)
    return {"ok": True, "code": ts_code, "n": len(dates), "dates": dates,
            "rzye": rzye, "rqye": rqye, "ratio": ratio,
            "latest": {"date": dates[-1] if dates else None,
                       "rzye": rzye[-1] if rzye else None,
                       "ratio": ratio[-1] if ratio else None}}


def _single_quarters(cum: list) -> list:
    """累计值→单季值: 同年内逐期差分(0331=原值, 0630-H1, 0930-9M, 1231-FY)。
    年内缺基期(如有Q2无Q1)时该期单季不可知 → None(不拿累计值冒充单季)。"""
    out, prev = [], {}
    for date, v in cum:
        year, md = date[:4], date[5:]
        base = prev.get(year)
        if md == "03-31":
            out.append((date, v))
        elif v is not None and base is not None:
            out.append((date, round(v - base, 2)))
        else:
            out.append((date, None))
        prev[year] = v
    return out


def profit_payload(code: str, path=None) -> dict:
    """模块④: 季度扣非净利润(单季/累计两口径) + 本地同比"""
    ts_code = _norm_code(code)
    con = _conn(path)
    rows = con.execute("SELECT end_date, profit_dedt FROM stock_finance "
                       "WHERE ts_code=? AND profit_dedt IS NOT NULL "
                       "ORDER BY end_date", (ts_code,)).fetchall()
    con.close()
    cum = [(r[0], r[1] / 1e8 if r[1] else r[1]) for r in rows]      # 元→亿
    single = _single_quarters(cum)

    def yoy(series):                             # 同期(年+月日)上年对比
        by = {d: v for d, v in series}
        out = []
        for d, v in series:
            py = str(int(d[:4]) - 1) + d[4:]
            pv = by.get(py)
            out.append(round((v - pv) / abs(pv) * 100, 1)
                       if v is not None and pv not in (None, 0) else None)
        return out

    return {"ok": True, "code": ts_code,
            "dates": [d for d, _ in cum],
            "cum": [v for _, v in cum], "cum_yoy": yoy(cum),
            "single": [v for _, v in single], "single_yoy": yoy(single)}


def val_payload(code: str, path=None) -> dict:
    """模块⑤: 季度末PE_TTM/PB + 近3/5年分位(负PE标'亏损')"""
    ts_code = _norm_code(code)
    con = _conn(path)
    vals = con.execute("SELECT trade_date, pe_ttm, pb FROM stock_val "
                       "WHERE ts_code=? ORDER BY trade_date",
                       (ts_code,)).fetchall()
    con.close()
    # 季度末交易日: 按季度分组取组内最大日(尾部次季初噪声天然剔除)
    byq = {}
    for d, *_ in vals:
        q = d[:4] + "Q" + str((int(d[5:7]) - 1) // 3 + 1)
        if d > byq.get(q, ""):
            byq[q] = d
    qend = [byq[k] for k in sorted(byq)]
    by = {d: (pe, pb) for d, pe, pb in vals}
    dates, pes, pbs, pe_p3, pe_p5, pb_p3, pb_p5 = [], [], [], [], [], [], []

    def _pct(window, x):
        xs = [v for v in window if v is not None and v > 0]
        if x is None or x <= 0 or len(xs) < 4:
            return None
        return round(sum(1 for v in xs if v <= x) / len(xs) * 100)

    series = [(d, *by[d]) for d in qend]
    for i, (d, pe, pb) in enumerate(series):
        dates.append(d)
        pes.append(round(pe, 1) if pe else None)             # 负/None→None(前端标亏损)
        pbs.append(round(pb, 2) if pb else None)
        w3 = [v for _, v, _ in series[max(0, i - 11):i + 1]]
        w5 = [v for _, v, _ in series[max(0, i - 19):i + 1]]
        w3b = [v for _, _, v in series[max(0, i - 11):i + 1]]
        w5b = [v for _, _, v in series[max(0, i - 19):i + 1]]
        pe_p3.append(_pct(w3, pe)); pe_p5.append(_pct(w5, pe))
        pb_p3.append(_pct(w3b, pb)); pb_p5.append(_pct(w5b, pb))
    return {"ok": True, "code": ts_code, "dates": dates, "pe": pes, "pb": pbs,
            "pe_p3": pe_p3, "pe_p5": pe_p5, "pb_p3": pb_p3, "pb_p5": pb_p5}


# ══════════ A3: 解禁与大事件 ══════════

EV_TYPE_NAME = {"float": "解禁", "forecast": "业绩预告", "dividend": "分红",
                "holdertrade": "增减持", "suspend": "停复牌"}
FORECAST_RISK = {"首亏": "high", "续亏": "high", "预减": "high", "略减": "mid",
                 "预增": "low", "略增": "low", "扭亏": "low", "续盈": "low"}


def _float_risk(ratio) -> str:
    if ratio is None:
        return "low"
    return "high" if ratio >= 5 else ("mid" if ratio >= 1 else "low")


def refresh_float(code: str, path=None, lookback: int = None) -> int:
    """个股解禁(share_float) + 同步生成解禁事件(未来>5%为大额高风险)
    注: 接口不支持ann_date过滤, lookback仅为签名统一(全量拉, 单股行数少)"""
    ts_code = _norm_code(code)
    try:
        df = T.call("share_float",
                    "ts_code,ann_date,float_date,float_share,float_ratio,holder_name",
                    ts_code=ts_code)
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 解禁失败 {ts_code}: {str(e)[:60]}")
        return -1
    con = _conn(path)
    fr = []
    for _, r in df.iterrows():
        fd, hn = _iso(r["float_date"]), str(r.get("holder_name") or "未知股东").strip()
        ratio = _f(r.get("float_ratio"))
        share = _f(r.get("float_share"))
        con.execute("INSERT INTO stock_float VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(ts_code,float_date,holder_name) DO UPDATE SET "
                    "ann_date=excluded.ann_date,float_share=excluded.float_share,"
                    "float_ratio=excluded.float_ratio", (ts_code, _ann(r.get("ann_date")),
                    fd, share, ratio, hn))
        fr.append((ts_code, fd, "float",
                   f"{hn}解禁{round(share / 1e8, 2) if share else '--'}亿股"
                   f"({ratio if ratio is not None else '--'}%)",
                   f"解禁股东: {hn}", _float_risk(ratio)))
    con.executemany("INSERT INTO stock_events VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(ts_code,ev_date,ev_type,title) DO UPDATE SET "
                    "detail=excluded.detail,risk=excluded.risk", fr)
    con.commit()
    con.close()
    return len(fr)


def refresh_events(code: str, path=None, lookback: int = None) -> int:
    """结构化大事件: 预告/分红/增减持/停牌 → 统一stock_events(风险分级)"""
    ts_code = _norm_code(code)
    con = _conn(path)
    ev = []
    try:                                                     # 业绩预告
        df = T.call("forecast", "ts_code,ann_date,end_date,type,p_change_min,p_change_max",
                    ts_code=ts_code, start_date=_start(lookback),
                    end_date=time.strftime("%Y%m%d"))
        for _, r in df.iterrows():
            t = str(r.get("type") or "预告").strip()
            ev.append((ts_code, _ann(r.get("ann_date")), "forecast",
                       f"业绩预告: {t}",
                       f"{_iso(r['end_date'])}报告期 预计变动{r.get('p_change_min')}%"
                       f"~{r.get('p_change_max')}%", FORECAST_RISK.get(t, "mid")))
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 预告失败: {str(e)[:50]}")
    try:                                                     # 分红送转
        df = T.call("dividend", "ts_code,end_date,ann_date,cash_div,stk_div",
                    ts_code=ts_code)
        for _, r in df.iterrows():
            cash, stk = _f(r.get("cash_div")), _f(r.get("stk_div"))
            if not cash and not stk:
                continue
            ev.append((ts_code, _ann(r.get("ann_date")), "dividend",
                       f"分红: 每10股派{cash}元" + (f" 送{stk}股" if stk else ""),
                       f"{_iso(r['end_date'])}年度分配", "low"))
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 分红失败: {str(e)[:50]}")
    try:                                                     # 股东增减持
        df = T.call("stk_holdertrade",
                    "ts_code,ann_date,holder_name,holder_type,change_vol,change_ratio",
                    ts_code=ts_code, start_date=_start(lookback) if lookback else "20180101",
                    end_date=time.strftime("%Y%m%d"))
        for _, r in df.iterrows():
            vol = _f(r.get("change_vol"))                   # 单位=股(实测:
            cr = _f(r.get("change_ratio"))                  # 0.0011%×12.56亿≈1.38万股=13800股)
            if vol is None or abs(vol) < 5e5:               # <50万股噪音滤除
                continue
            down = vol < 0
            hn = str(r.get("holder_name") or "").strip()[:18]
            risk = "high" if down and (cr is not None and abs(cr) >= 1) else "mid"
            ev.append((ts_code, _ann(r.get("ann_date")), "holdertrade",
                       f"{hn}{'减持' if down else '增持'}{abs(round(vol / 1e4, 0))}万股",
                       f"变动占比{cr}%", risk))
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 增减持失败: {str(e)[:50]}")
    try:                                                     # 停复牌
        df = T.call("suspend", "ts_code,suspend_date,resume_date", ts_code=ts_code)
        for _, r in df.iterrows():
            ev.append((ts_code, _iso(r["suspend_date"]), "suspend",
                       "停牌", f"至{_iso(r.get('resume_date')) or '--'}复牌", "mid"))
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 停复牌失败: {str(e)[:50]}")
    try:                                                     # 质押(状态类独立表)
        df = T.call("pledge_stat", "ts_code,end_date,pledge_ratio", ts_code=ts_code)
        con.executemany("INSERT INTO stock_pledge VALUES(?,?,?) "
                        "ON CONFLICT(ts_code,end_date) DO UPDATE SET "
                        "pledge_ratio=excluded.pledge_ratio",
                        [(ts_code, _iso(r["end_date"]), _f(r.get("pledge_ratio")))
                         for _, r in df.iterrows()])
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 质押失败: {str(e)[:50]}")
    con.executemany("INSERT INTO stock_events VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(ts_code,ev_date,ev_type,title) DO UPDATE SET "
                    "detail=excluded.detail,risk=excluded.risk", ev)
    con.commit()
    con.close()
    return len(ev)


def refresh_bs(code: str, path=None) -> int:
    """A4: 资产负债表关键列(商誉/总资产/总负债 — 净资产自算)"""
    ts_code = _norm_code(code)
    try:
        df = T.call("balancesheet", "ts_code,end_date,goodwill,total_assets,total_liab",
                    ts_code=ts_code, start_date="19900000",
                    end_date=time.strftime("%Y%m%d"))
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 资产负债表失败 {ts_code}: {str(e)[:60]}")
        return -1
    rows = [(ts_code, _iso(r["end_date"]), _f(r.get("goodwill")),
             _f(r.get("total_assets")), _f(r.get("total_liab")))
            for _, r in df.iterrows()]
    con = _conn(path)
    con.executemany("INSERT INTO stock_bs VALUES(?,?,?,?,?) "
                    "ON CONFLICT(ts_code,end_date) DO UPDATE SET "
                    "goodwill=excluded.goodwill,total_assets=excluded.total_assets,"
                    "total_liab=excluded.total_liab", rows)
    con.commit()
    con.close()
    return len(rows)


def float_payload(code: str, path=None) -> dict:
    """模块⑥: 解禁序列(历史/未来分段, 大额>=5%标红)+未来3月汇总"""
    import datetime as dt
    ts_code = _norm_code(code)
    today = dt.date.today().isoformat()
    con = _conn(path)
    rows = con.execute("SELECT float_date, float_share, float_ratio, holder_name "
                       "FROM stock_float WHERE ts_code=? ORDER BY float_date",
                       (ts_code,)).fetchall()
    con.close()
    out = [{"date": r[0], "share_yi": round(r[1] / 1e8, 2) if r[1] else None,
            "ratio": r[2], "holder": r[3][:12],
            "future": r[0] > today, "big": (r[2] or 0) >= 5} for r in rows]
    soon = [x for x in out if x["future"] and x["date"] <=
            (dt.date.today() + dt.timedelta(days=90)).isoformat()]
    return {"ok": True, "code": ts_code, "n": len(out), "data": out,
            "soon": {"n": len(soon),
                     "total_yi": round(sum(x["share_yi"] or 0 for x in soon), 2),
                     "max_ratio": max((x["ratio"] or 0 for x in soon), default=0)}}


def events_payload(code: str, path=None) -> dict:
    """模块⑧: 大事件列表(K线标记+hover详情用), 按日期升序"""
    ts_code = _norm_code(code)
    con = _conn(path)
    rows = con.execute("SELECT ev_date, ev_type, title, detail, risk "
                       "FROM stock_events WHERE ts_code=? ORDER BY ev_date",
                       (ts_code,)).fetchall()
    con.close()
    return {"ok": True, "code": ts_code, "n": len(rows),
            "events": [{"date": r[0], "type": EV_TYPE_NAME.get(r[1], r[1]),
                        "title": r[2], "detail": r[3], "risk": r[4]} for r in rows]}


def risk_payload(code: str, path=None) -> dict:
    """顶部风险提示: 未来3月大额解禁 + 最新质押 + 近180天高风险事件"""
    import datetime as dt
    ts_code = _norm_code(code)
    d180 = (dt.date.today() - dt.timedelta(days=180)).isoformat()
    fl = float_payload(ts_code, path)
    con = _conn(path)
    pledge = con.execute("SELECT end_date, pledge_ratio FROM stock_pledge "
                         "WHERE ts_code=? ORDER BY end_date DESC LIMIT 1",
                         (ts_code,)).fetchone()
    highs = con.execute("SELECT ev_date, title FROM stock_events "
                        "WHERE ts_code=? AND risk='high' AND ev_date>=? "
                        "ORDER BY ev_date DESC LIMIT 8", (ts_code, d180)).fetchall()
    con.close()
    items = []
    if fl["soon"]["n"] and fl["soon"]["max_ratio"] >= 5:
        items.append({"level": "high", "text": f"未来90天解禁{fl['soon']['n']}批/"
                      f"共{fl['soon']['total_yi']}亿股, 最大占总股本{fl['soon']['max_ratio']}%"})
    if pledge and pledge[1] is not None and pledge[1] >= 30:
        items.append({"level": "high", "text": f"股权质押比例{pledge[1]}%"
                      f"({pledge[0]}) — 平仓风险区"})
    for d, t in highs:
        items.append({"level": "high", "text": f"[{d}] {t}"})
    # checked标记: 无风险项≠未拉取(避免每次空items都重新拉接口)
    return {"ok": True, "code": ts_code, "items": items, "checked": True}


def run_nightly(weekly: bool = False, path=None) -> dict:
    """A5夜间链: 日频(daily回看7+margin/val 45日+events 30日) ;
    weekly=True加季频全史(finance/bs/holders/float)。
    调度: 19:30每晚 + 周五19:35季频(jobs挂载)。"""
    import datetime as dt
    r = {"daily": refresh_daily(path=path), "stocks": 0, "weekly": weekly}
    con = _conn(path)
    pool = [r2[0] for r2 in con.execute("SELECT ts_code FROM stock_pool")]
    con.close()
    for ts_code in pool:
        n = 0
        n += max(0, refresh_margin(ts_code, path, lookback=45))
        n += max(0, refresh_val(ts_code, path, lookback=45))
        n += max(0, refresh_events(ts_code, path, lookback=30))
        if weekly:
            n += max(0, refresh_finance(ts_code, path))
            n += max(0, refresh_bs(ts_code, path))
            n += max(0, refresh_holders(ts_code, path))
            n += max(0, refresh_float(ts_code, path))
        r["stocks"] += 1
        print(f"  {ts_code}: +{n}行")
    print(f"[stock] 夜间链{'(含季频)' if weekly else ''}: {r['stocks']}只 +{r['daily']}日频行")
    return r


def full_refresh(code: str, path=None) -> dict:
    """A5手动单只全量重拉(全史所有数据集)"""
    ts_code = _norm_code(code)
    r = add_stock(ts_code, path)
    if not r.get("ok"):
        return r
    n = {"margin": refresh_margin(ts_code, path),
         "val": refresh_val(ts_code, path),
         "finance": refresh_finance(ts_code, path),
         "bs": refresh_bs(ts_code, path),
         "holders": refresh_holders(ts_code, path),
         "events": refresh_events(ts_code, path),
         "float": refresh_float(ts_code, path)}
    r["detail"] = n
    return r


def stats_pool(path=None) -> dict:
    """A5数据管理卡: 池内每股各数据集健康度(last日期/行数/AI评级)"""
    con = _conn(path)
    pool = con.execute("SELECT ts_code, name, last_daily FROM stock_pool "
                       "ORDER BY added_at DESC").fetchall()
    out = []
    for ts_code, name, last_daily in pool:
        one = con.execute(
            "SELECT (SELECT MAX(trade_date) FROM stock_daily WHERE ts_code=:c),"
            " (SELECT COUNT(*) FROM stock_daily WHERE ts_code=:c),"
            " (SELECT MAX(trade_date) FROM stock_margin WHERE ts_code=:c),"
            " (SELECT MAX(trade_date) FROM stock_val WHERE ts_code=:c),"
            " (SELECT MAX(end_date) FROM stock_finance WHERE ts_code=:c),"
            " (SELECT COUNT(*) FROM stock_events WHERE ts_code=:c),"
            " (SELECT rule_grade FROM stock_ai WHERE ts_code=:c)",
            {"c": ts_code}).fetchone()
        out.append({"code": ts_code, "name": name, "last_daily": one[0],
                    "rows_daily": one[1], "last_margin": one[2],
                    "last_val": one[3], "last_finance": one[4],
                    "events_n": one[5], "ai_grade": one[6]})
    con.close()
    return {"pool": out, "n": len(out)}


def main():
    ap = argparse.ArgumentParser(description="避雷针个股数据(A1)")
    ap.add_argument("--add", default=None, help="入池股票代码")
    ap.add_argument("--daily", action="store_true", help="池内日增")
    ap.add_argument("--basic", action="store_true", help="全市场基础信息缓存")
    ap.add_argument("--nightly", action="store_true", help="夜间链(日频增量)")
    ap.add_argument("--weekly", action="store_true", help="夜间链+季频全史")
    ap.add_argument("--stats", action="store_true", help="池健康度")
    ap.add_argument("--full", default=None, help="单只全量重拉")
    a = ap.parse_args()
    if a.add:
        print(add_stock(a.add))
    elif a.full:
        print(full_refresh(a.full))
    elif a.nightly:
        run_nightly()
    elif a.weekly:
        run_nightly(weekly=True)
    elif a.stats:
        import json as _j
        print(_j.dumps(stats_pool(), ensure_ascii=False))
    elif a.daily:
        refresh_daily()
    elif a.basic:
        refresh_basic()
    return 0


if __name__ == "__main__":
    sys.exit(main())
