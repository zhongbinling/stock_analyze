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
    con = sqlite3.connect(str(p), timeout=30)   # 并发写让锁(夜间链与页面查询)
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
      CREATE TABLE IF NOT EXISTS stock_finance(          -- B1: 季度财务(fina_indicator)
        ts_code TEXT, end_date TEXT, ann_date TEXT,
        profit_dedt REAL, netprofit_yoy REAL, or_yoy REAL,
        grossprofit_margin REAL, roe REAL, debt_to_assets REAL,
        netprofit_margin REAL, inv_turn REAL, ar_turn REAL,
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
      CREATE TABLE IF NOT EXISTS stock_val(              -- B1: 每日估值+股息率
        ts_code TEXT, trade_date TEXT, pe_ttm REAL, pb REAL,
        total_mv REAL, circ_mv REAL, dv_ttm REAL,
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
      CREATE TABLE IF NOT EXISTS stock_bs(               -- B1: 资产负债表全列
        ts_code TEXT, end_date TEXT, money_cap REAL,
        accounts_receiv REAL, acct_payable REAL, inventories REAL,
        goodwill REAL, total_assets REAL, total_liab REAL,
        PRIMARY KEY(ts_code, end_date));
      CREATE TABLE IF NOT EXISTS stock_ai(               -- A4: AI财报分析缓存(一股一行)
        ts_code TEXT PRIMARY KEY, end_date TEXT, ann_date TEXT,
        rule_grade TEXT, rule_score REAL, rules_json TEXT,
        ai_json TEXT, model TEXT, analyzed_at TEXT);
      CREATE TABLE IF NOT EXISTS stock_income(           -- B1: 利润表
        ts_code TEXT, end_date TEXT, revenue REAL,
        total_revenue REAL, n_income REAL,
        PRIMARY KEY(ts_code, end_date));
      CREATE TABLE IF NOT EXISTS stock_cashflow(         -- B1: 现金流量表
        ts_code TEXT, end_date TEXT, n_cashflow_act REAL,
        PRIMARY KEY(ts_code, end_date));
    """)
    # B1: 旧库扩列(PRAGMA检测, 幂等)
    for tbl, col, typ in (
            ("stock_bs", "money_cap", "REAL"), ("stock_bs", "accounts_receiv", "REAL"),
            ("stock_bs", "acct_payable", "REAL"), ("stock_bs", "inventories", "REAL"),
            ("stock_finance", "netprofit_margin", "REAL"),
            ("stock_finance", "inv_turn", "REAL"),
            ("stock_finance", "ar_turn", "REAL"),
            ("stock_val", "dv_ttm", "REAL")):
        try:
            have = {r[1] for r in con.execute(f"PRAGMA table_info({tbl})")}
            if col not in have:
                con.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass                                  # 表未建(全新库由CREATE建)
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
    s = str(d or "").strip()
    if s.endswith(".0"):
        s = s[:-2]                 # 含空值的整数日期列被pandas整体读成浮点: 20261107.0
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


_PAGE_ROWS = T.PAGE_ROWS          # 分页取全的实现与说明见 tushare_api.pull_frames
_MAX_ROWS = T.MAX_ROWS
_pull_frames = T.pull_frames


def _pull_all(api: str, fields: str, ts_code: str):
    """日线类全史分页拉取, 按trade_date升序返回DataFrame。
    D1-35: 单次调用最多6000行 ≈ 24.6年 — 实测自选池9只里5只1994~2001年上市的老股
    日线恰为6000行(600760上市1996年, 日线始于2001-03), "全史K线"实际缺了最早几年;
    复权因子同样被截, 两边末端对不齐 → 最早一两百行adj_factor为空, 前复权价在这段
    直接取原始价。"""
    frames = _pull_frames(api, fields, ts_code=ts_code, start_date="19900000",
                          end_date=time.strftime("%Y%m%d"))
    if len(frames) == 1:
        df = frames[0]
    else:
        import pandas as pd
        df = pd.concat([f for f in frames if f is not None], ignore_index=True)
    return df.sort_values("trade_date") if df is not None and len(df) else df


def _one_per_period(rows: list) -> list:
    """季报表/财务指标: 同一报告期(end_date)只留一行 — 实际公告日最晚的, 同日取更正后的
    (update_flag=1)。

    接口里同一报告期常有两行(原始/更正后), 先后并不固定(财务指标是更正行在前, 三大报表
    是原始行在前), 靠 upsert 后写覆盖的话留下哪一版取决于接口的行序。还可能是三行:
    五粮液2025年中报有 2025-08-28 的原始行、同日更正行, 和一行实际公告日为 2026-04-30 的
    (2025年报发布时追溯调整后: 营收由527.7亿调整为235.1亿)。必须取最后这一行 — 2025年
    年报数(净利润93.2亿)就是调整后口径, 留原披露值的话 9月累计净利润222亿比全年还大,
    滚动12个月净利润会被算成负数。所以要比"实际公告日"f_ann_date, 不能只比 ann_date
    (追溯调整那一行的 ann_date 仍是首次公告日)。"""
    best = {}
    for r in rows:
        k = _iso(r.get("end_date"))
        when = _iso(_text(r.get("f_ann_date"))) or _iso(_text(r.get("ann_date")))
        rank = (when, _text(r.get("update_flag")) == "1")
        if k not in best or rank > best[k][0]:
            best[k] = (rank, r)
    return [v[1] for v in best.values()]


def _pull_stmt(api: str, fields: str, ts_code: str, start: str = "19900000") -> list:
    """三大报表/财务指标的全史拉取(分页 + 每个报告期一行), start 为公告日起始。
    D1-40: 这几个接口单次最多返回100行(实测 balancesheet/cashflow/fina_indicator;
    income 单次可到130行以上), 而同一报告期常有两行(update_flag 0=原始/1=更正后),
    100行只够56~84个报告期: 茅台的财务指标只到2012-12、资产负债表只到2009-09,
    更早的一段在库里一直缺着 — 页面上盈利/现金流/资产负债几组图的"全史"其实是
    最近十几年。"""
    frames = _pull_frames(api, fields, ts_code=ts_code, start_date=start,
                          end_date=time.strftime("%Y%m%d"))
    return _one_per_period([r for df in frames if df is not None
                            for r in df.to_dict("records")])


def add_stock(code: str, path=None) -> dict:
    """入池: 全史日线+复权因子(分页, 见_pull_all)。幂等(已入池同样全量刷新)。"""
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
        d = _pull_all("daily", "ts_code,trade_date,open,high,low,close,vol,amount",
                      ts_code)
        dfa = _pull_all("adj_factor", "ts_code,trade_date,adj_factor", ts_code)
        fac = {_iso(r["trade_date"]): float(r["adj_factor"]) for _, r in dfa.iterrows()}
        rows = [(ts_code, _iso(r["trade_date"]),
                 float(r["open"]), float(r["high"]), float(r["low"]),
                 float(r["close"]), float(r["vol"] or 0),
                 float(r["amount"] or 0), fac.get(_iso(r["trade_date"])))
                for _, r in d.iterrows()]
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
    """池内日增: 回看N自然日幂等upsert(错过日自动补, 同fetch_etf语义)。
    D1-35: upsert更新全部OHLC(原只更新close/vol/amount/复权因子) — 已入库的K线
    在源端修订开高低时也能纠正。
    注: tushare日线在交易日15~16点收盘后入库, 盘中没有当日行(D1-36核对官方文档,
    并把库内144根K线与接口逐根比对无一差异)。此前说明里"盘中点[池日增]写入的
    是当日快照、之后永不纠正"的说法没有依据。"""
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
                "open=excluded.open,high=excluded.high,low=excluded.low,"
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
    facs = [r[7] for r in rows]
    if adj == "qfq":
        base = max((f for f in facs if f), default=1.0) or 1.0
    elif adj == "hfq":
        base = 1.0
    # 复权因子缺失的行(历史上被接口行数上限截掉的那一段, 见_pull_all)顺延取
    # 其后首个已知因子 — 原按1.0处理, 这段前复权价直接成了原始价(600519
    # 2002-02-28收盘38.00 vs 次日4.32, 差一个复权系数的倍数)
    if adj != "none":
        nxt, fill = None, []
        for f in reversed(facs):
            if f:
                nxt = f
            fill.append(nxt)
        facs = fill[::-1]
        # 末端(最新几天)缺失时后面没有已知因子可顺延 — 原留空按1.0处理, 后复权在
        # 最后一根K线上出现断崖(前复权因归一到最新因子而不受影响) → 取前一个已知因子
        prev = None
        for i, f in enumerate(facs):
            if f:
                prev = f
            else:
                facs[i] = prev
    closes_raw = [r[4] for r in rows]
    # qfq: factor/最新factor(归一); hfq: factor本身; none: 1
    def _fac(i):
        if adj == "none" or facs[i] is None:
            return 1.0
        return facs[i] / base if adj == "qfq" else facs[i]
    k = [[round(r[1] * _fac(i), 2), round(r[4] * _fac(i), 2),
          round(r[3] * _fac(i), 2), round(r[2] * _fac(i), 2)]
         for i, r in enumerate(rows)]
    closes = [round(c * _fac(i), 2) for i, c in enumerate(closes_raw)]
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


def _text(v, default: str = "") -> str:
    """接口文本字段清洗: None/NaN → default, 其余去首尾空白。
    pandas的NaN是truthy, `v or "默认值"`挡不住它 — 会原样落库成"nan" """
    if v is None or (isinstance(v, float) and v != v):
        return default
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none") else default


def _start(lookback: int = None) -> str:
    """全史或近N自然日起始(A5: 夜间增量用lookback, 手动全量None)"""
    if not lookback:
        return "19900000"
    import datetime as _dt
    return (_dt.date.today() - _dt.timedelta(days=lookback)).strftime("%Y%m%d")


def refresh_finance(code: str, path=None, lookback: int = None) -> int:
    """季度财务(fina_indicator全史分页): 扣非/增速/毛利率/ROE/负债率"""
    ts_code = _norm_code(code)
    try:
        recs = _pull_stmt("fina_indicator",
                          "ts_code,end_date,ann_date,update_flag,profit_dedt,"
                          "netprofit_yoy,or_yoy,grossprofit_margin,roe,debt_to_assets,"
                          "netprofit_margin,inv_turn,ar_turn",
                          ts_code, _start(lookback))
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 财务失败 {ts_code}: {str(e)[:60]}")
        return -1
    rows = [(ts_code, _iso(r["end_date"]), _ann(r.get("ann_date")),
             _f(r.get("profit_dedt")), _f(r.get("netprofit_yoy")),
             _f(r.get("or_yoy")), _f(r.get("grossprofit_margin")),
             _f(r.get("roe")), _f(r.get("debt_to_assets")),
             _f(r.get("netprofit_margin")), _f(r.get("inv_turn")),
             _f(r.get("ar_turn")))
            for r in recs]
    con = _conn(path)
    con.executemany(
        "INSERT INTO stock_finance(ts_code,end_date,ann_date,profit_dedt,"
        "netprofit_yoy,or_yoy,grossprofit_margin,roe,debt_to_assets,"
        "netprofit_margin,inv_turn,ar_turn) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ts_code,end_date) DO UPDATE SET ann_date=excluded.ann_date,"
        "profit_dedt=excluded.profit_dedt,netprofit_yoy=excluded.netprofit_yoy,"
        "or_yoy=excluded.or_yoy,grossprofit_margin=excluded.grossprofit_margin,"
        "roe=excluded.roe,debt_to_assets=excluded.debt_to_assets,"
        "netprofit_margin=excluded.netprofit_margin,inv_turn=excluded.inv_turn,"
        "ar_turn=excluded.ar_turn", rows)
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


_VAL_FIELDS = "ts_code,trade_date,pe_ttm,pb,total_mv,circ_mv,dv_ttm"


def refresh_val(code: str, path=None, lookback: int = None) -> int:
    """每日估值(daily_basic): PE_TTM/PB/总市值/流通市值。
    D1-35: 全量拉取走分页 — daily_basic与daily同为单次6000行上限(见_pull_all),
    池内5只老股的估值史比K线短2~5年(中航高科K线自1994-05、估值自1999-11)"""
    ts_code = _norm_code(code)
    try:
        if lookback:
            df = T.call("daily_basic", _VAL_FIELDS, ts_code=ts_code,
                        start_date=_start(lookback), end_date=time.strftime("%Y%m%d"))
        else:
            df = _pull_all("daily_basic", _VAL_FIELDS, ts_code)
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 估值失败 {ts_code}: {str(e)[:60]}")
        return -1
    rows = [(ts_code, _iso(r["trade_date"]), _f(r.get("pe_ttm")), _f(r.get("pb")),
             _f(r.get("total_mv")), _f(r.get("circ_mv")), _f(r.get("dv_ttm")))
            for _, r in df.iterrows()]
    con = _conn(path)
    con.executemany("INSERT INTO stock_val(ts_code,trade_date,pe_ttm,pb,total_mv,"
                    "circ_mv,dv_ttm) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(ts_code,trade_date) DO UPDATE SET "
                    "pe_ttm=excluded.pe_ttm,pb=excluded.pb,"
                    "total_mv=excluded.total_mv,circ_mv=excluded.circ_mv,"
                    "dv_ttm=excluded.dv_ttm", rows)
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
    hn_prev = None                          # 最近一期有户数披露的值(as-of)
    for end_date in reversed(periods):        # 旧→新累积prev_ratio(环比=较上期)
        if hnum.get(end_date) is not None:
            hn_prev = hnum[end_date]
        rows = []
        for name, amt, ratio in allp[end_date]:
            chg = None
            p = prev_ratio.get(name)
            if p is not None and ratio is not None:
                chg = round(ratio - p, 2)
            rows.append({"name": name, "amount": amt, "ratio": ratio,
                         "chg": chg, "tag": _holder_tag(name)})
            prev_ratio[name] = ratio
        real = hnum.get(end_date)
        out.append({"end_date": end_date,
                    "holder_num": real,        # 本期真实披露值(无披露=None)
                    # 非标准披露日(如25-11-05临时披露十大股东但不披露户数)
                    # 的沿用值: 仅供展示"户数≈上期"; 不并入holder_num —
                    # D1-31曾直接填充, 页面据此算环比得到"+0.0% 筹码分散⚠"的假预警
                    "holder_num_asof": None if real is not None else hn_prev,
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


# ══════════ B1: 财务指标全景 ══════════

def finance_full_payload(code: str, path=None, n_qua: int | None = None) -> dict:
    """B1: 四组指标全景(单位: 金额亿/比率%/周转次数)
    盈利(营收/净利/扣非/增速/毛利率/净利率/ROE/股息率) · 现金流与营运
    (经营现金流/应收/应付/存货/货币资金/存货周转/应收周转) ·
    资产负债(资产/负债/负债率/商誉及占净资产) · 筹码(机构持仓比例/质押率)
    报告期默认取全史(页面图表); n_qua 为整数时只取最近 n 季(扫雷 prompt 的财务表)。"""
    ts_code = _norm_code(code)
    con = _conn(path)
    inc = {r[0]: r for r in con.execute(
        "SELECT end_date, revenue, n_income FROM stock_income WHERE ts_code=?",
        (ts_code,))}
    cf = {r[0]: r[1] for r in con.execute(
        "SELECT end_date, n_cashflow_act FROM stock_cashflow WHERE ts_code=?",
        (ts_code,))}
    fin = {r[0]: r for r in con.execute(
        "SELECT end_date, profit_dedt, netprofit_yoy, or_yoy, grossprofit_margin,"
        " netprofit_margin, roe, debt_to_assets, inv_turn, ar_turn "
        "FROM stock_finance WHERE ts_code=?", (ts_code,))}
    bs = {r[0]: r for r in con.execute(
        "SELECT end_date, money_cap, accounts_receiv, acct_payable, inventories,"
        " goodwill, total_assets, total_liab FROM stock_bs WHERE ts_code=?",
        (ts_code,))}
    dv = con.execute("SELECT dv_ttm FROM stock_val WHERE ts_code=? "
                     "AND dv_ttm IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
                     (ts_code,)).fetchone()
    pledge = con.execute("SELECT pledge_ratio FROM stock_pledge WHERE ts_code=? "
                         "ORDER BY end_date DESC LIMIT 1", (ts_code,)).fetchone()
    # 机构持仓: 全期十大流通中 机构类(公募/资管/北向/国家队/私募) 持股占比合计
    # (图表修复: 原LIMIT 3仅2026三期且DESC倒序 → 全期+正序)
    inst = []
    hn_prev = None                          # D1-31: as-of股东户数填充携带
    for (end_date,) in con.execute(
            "SELECT DISTINCT end_date FROM stock_holders WHERE ts_code=? "
            "ORDER BY end_date ASC", (ts_code,)).fetchall():
        names = con.execute("SELECT holder_name, ratio FROM stock_holders "
                            "WHERE ts_code=? AND end_date=?",
                            (ts_code, end_date)).fetchall()
        ratio_sum = round(sum((r or 0) for n, r in names
                              if _holder_tag(n) in ("公募/资管", "北向", "国家队",
                                                    "私募", "金融机构")), 2)
        hn = con.execute("SELECT holder_num FROM stock_holdernum WHERE ts_code=? "
                         "AND end_date=?", (ts_code, end_date)).fetchone()
        if hn and hn[0] is not None:
            hn_prev = hn[0]
        inst.append((end_date, ratio_sum,
                     hn[0] if hn and hn[0] is not None else hn_prev))  # as-of填
    con.close()
    # 报告期 = 利润表/财务指标/资产负债表三张表报告期的并集, 各表起点不同
    # (茅台现金流量表始于2005年, 其余1998年; 中航沈飞1993年)。图表看的是全史;
    # 扫雷 prompt 另传 n_qua 只取最近几期 — 曾在载荷里统一截成最近12期,
    # 页面上盈利/现金流/资产负债六张图就只剩最近三年, 缩放拖动也到不了更早
    dates = sorted(set(inc) | set(fin) | set(bs))
    if n_qua:
        dates = dates[-n_qua:]
    E = 1e8                                    # 元→亿
    out = {"ok": True, "code": ts_code, "dates": [],
           "profit": {"revenue": [], "n_income": [], "dedt": [], "or_yoy": [],
                      "net_yoy": [], "gpm": [], "npm": [], "roe": [], "dv_ttm": None},
           "cashflow": {"ocf": [], "ar": [], "ap": [], "inv": [], "cash": [],
                        "inv_turn": [], "ar_turn": []},
           "balance": {"assets": [], "liab": [], "dta": [], "goodwill": [],
                       "gw_eq": [], "liab_pct": []},
           "inst": {"dates": [x[0] for x in inst], "ratio": [x[1] for x in inst],
                    "holder_num": [x[2] for x in inst]},
           "pledge_ratio": pledge[0] if pledge else None}
    if dv:
        out["profit"]["dv_ttm"] = dv[0]
    for d in dates:
        i = inc.get(d)
        f = fin.get(d)
        b = bs.get(d)
        out["dates"].append(d)
        out["profit"]["revenue"].append(round(i[1] / E, 2) if i and i[1] else None)
        out["profit"]["n_income"].append(round(i[2] / E, 2) if i and i[2] else None)
        out["profit"]["dedt"].append(round(f[1] / E, 2) if f and f[1] else None)
        # fin列序: 1dedt 2net_yoy 3or_yoy 4gpm 5npm 6roe 7dta 8inv_turn
        out["profit"]["or_yoy"].append(f[3] if f else None)
        out["profit"]["net_yoy"].append(f[2] if f else None)
        out["profit"]["gpm"].append(f[4] if f else None)
        out["profit"]["npm"].append(f[5] if f else None)
        out["profit"]["roe"].append(f[6] if f else None)
        ocf = cf.get(d)
        out["cashflow"]["ocf"].append(round(ocf / E, 2) if ocf else None)
        # bs列序: 0end_date 1money_cap 2ar 3ap 4inventories
        #         5goodwill 6total_assets 7total_liab
        if b:
            out["cashflow"]["ar"].append(round(b[2] / E, 2) if b[2] else None)
            out["cashflow"]["ap"].append(round(b[3] / E, 2) if b[3] else None)
            out["cashflow"]["inv"].append(round(b[4] / E, 2) if b[4] else None)
            out["cashflow"]["cash"].append(round(b[1] / E, 2) if b[1] else None)
            eq = (b[6] - b[7]) if b[6] and b[7] else None    # 净资产自算
            out["balance"]["assets"].append(round(b[6] / E, 1) if b[6] else None)
            out["balance"]["liab"].append(round(b[7] / E, 1) if b[7] else None)
            out["balance"]["liab_pct"].append(round(b[7] / b[6] * 100, 1)
                                              if b[7] and b[6] else None)   # 负债/资产%
            out["balance"]["dta"].append(f[7] if f else None)
            out["balance"]["goodwill"].append(round(b[5] / E, 2) if b[5] else None)
            out["balance"]["gw_eq"].append(round(b[5] / eq * 100, 1)
                                           if b[5] and eq and eq > 0 else None)
        else:
            for k in ("ar", "ap", "inv", "cash"):
                out["cashflow"][k].append(None)
            for k in ("assets", "liab", "dta", "goodwill", "gw_eq", "liab_pct"):
                out["balance"][k].append(None)
        out["cashflow"]["inv_turn"].append(f[8] if f else None)
        # fin列序: ...8inv_turn 9ar_turn
        out["cashflow"]["ar_turn"].append(f[9] if f else None)
    return out


# ══════════ A3: 解禁与大事件 ══════════

EV_TYPE_NAME = {"float": "解禁", "forecast": "业绩预告", "dividend": "分红",
                "holdertrade": "增减持", "suspend": "停复牌"}
FORECAST_RISK = {"首亏": "high", "续亏": "high", "预减": "high", "略减": "mid",
                 "预增": "low", "略增": "low", "扭亏": "low", "续盈": "low"}


def _float_risk(ratio) -> str:
    if ratio is None:
        return "low"
    return "high" if ratio >= 5 else ("mid" if ratio >= 1 else "low")


def _holdertrade_risk(down: bool, cr) -> str:
    """增减持事件的风险分级: 增持不属风险; 减持按占流通股本比例 ≥1%高 / ≥0.1%中 /
    更低不算风险事件; 占比未知沿用绝对量口径(中)。
    (占比是tushare的change_ratio, 基数为流通股本 — D1-36用"变动量÷占比"反推
    229条样本全部等于当日流通股本; 此前说明误写成总股本。限售股占比高的股票
    如次新股、A+H, 该比例会高于占总股本的比例)

    D1-35: 原只看绝对量(过50万股门槛即中风险)。生产库占比<0.1%的减持里98%
    (115/117)是龙虎榜席位成交(机构专用/券商营业部), 对大盘股不到一天成交量的
    零头; ≥0.1%的91%才是真实股东/董监高减持。新口径扫雷实测把紫金矿业单月4笔
    合计占比0.01%~0.04%的席位卖出写成"[高]机构集中大额减持加速"。"""
    if not down:
        return "low"
    if cr is None:
        return "mid"
    a = abs(cr)
    return "high" if a >= 1 else ("mid" if a >= 0.1 else "low")


def _fmt_float_shares(share) -> str:
    """解禁股数: ≥1000万股用亿股(两位), 否则用万股(两位) — 原一律"0.00亿股",
    小额解禁(如6.1万股)显示成"0.0亿股" """
    if not share:
        return "--"
    return f"{share / 1e8:.2f}亿股" if share >= 1e7 else f"{share / 1e4:.2f}万股"


def refresh_float(code: str, path=None, lookback: int = None) -> int:
    """个股解禁(share_float) + 同步生成解禁事件(未来>5%为大额高风险)
    注: 接口不支持ann_date过滤, lookback仅为签名统一(全量拉, 单股行数少)。
    D1-35: 拉到行则替换该股旧的解禁事件(原只增不删, 标题格式变更后旧行残留);
    事件按(解禁日,股东)去重, 与stock_float主键一致。"""
    ts_code = _norm_code(code)
    try:
        df = T.call("share_float",
                    "ts_code,ann_date,float_date,float_share,float_ratio,holder_name",
                    ts_code=ts_code)
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 解禁失败 {ts_code}: {str(e)[:60]}")
        return -1
    con = _conn(path)
    fr = {}
    for _, r in df.iterrows():
        fd, hn = _iso(r["float_date"]), _text(r.get("holder_name"), "未知股东")
        if len(fd) != 10 or not fd[:4].isdigit():       # 缺解禁日的脏行不入库
            continue
        ratio = _f(r.get("float_ratio"))
        share = _f(r.get("float_share"))
        con.execute("INSERT INTO stock_float VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(ts_code,float_date,holder_name) DO UPDATE SET "
                    "ann_date=excluded.ann_date,float_share=excluded.float_share,"
                    "float_ratio=excluded.float_ratio", (ts_code, _ann(r.get("ann_date")),
                    fd, share, ratio, hn))
        fr[(fd, hn)] = (ts_code, fd, "float",
                        f"{hn}解禁{_fmt_float_shares(share)}"
                        f"({ratio if ratio is not None else '--'}%)",
                        f"解禁股东: {hn}", _float_risk(ratio))
    if fr:
        con.execute("DELETE FROM stock_events WHERE ts_code=? AND ev_type='float'",
                    (ts_code,))
        con.executemany("INSERT INTO stock_events VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(ts_code,ev_date,ev_type,title) DO UPDATE SET "
                        "detail=excluded.detail,risk=excluded.risk", list(fr.values()))
    con.commit()
    con.close()
    return len(fr)


def _fmt_shares(vol: float) -> str:
    """股 → "12.34万股"(<100万股保留两位) / "1274.2万股"(≥100万股保留一位)"""
    w = vol / 1e4
    return f"{w:.2f}万股" if w < 100 else f"{w:.1f}万股"


def _clean_holder_name(raw) -> str:
    """股东名: 压空白; 超长截断不截在括号中间(曾出现"…(非营业场所增持3300万股");
    空值/NaN → ""(由调用方给默认名)"""
    s = " ".join(_text(raw).split())
    if len(s) > 24:
        s = s[:24]
        if s.count("(") > s.count(")") or s.count("（") > s.count("）"):
            import re
            s = re.sub(r"[(（][^(（]*$", "", s)      # 截断点落在未闭合括号内 → 整段括号去掉
        s = s.rstrip(" ")
    return s


_WINDOWED_EVENTS = ("forecast", "holdertrade")     # 按公告日窗口拉取的事件类型


def refresh_events(code: str, path=None, lookback: int = None) -> int:
    """结构化大事件: 预告/分红/增减持/停牌 → 统一stock_events(风险分级)

    D1-35(避雷针体检, 对照真实接口数据):
    - 增减持方向取接口字段 in_de(IN增持/DE减持) — change_vol恒为正。原按
      vol<0判减持 → 全库668条增减持事件全部记成"增持", 减持(最核心的避雷
      信号)从未被识别、从未进扫雷的风险事件列表。无in_de的旧行仍按负号兜底。
    - 增持不属风险(risk=low): 原≥50万股的增持一律mid → K线满屏橙点, 且被
      当作"近一年风险事件"喂给AI
    - 按类型替换旧行: 只动本次拉取成功的类型 — 增量刷新(lookback)只清窗口内
      的行, 全量刷新清该股该类型全部行(接口返回空则不动, 防抖动清库)。
      原只增不删: 标题口径变更/源端修订后旧行永久残留(实测19组同日同股东
      新旧两行并存, 含"增持1274234.0万股"这类脏行)
    - D1-36: 增减持的全量刷新拉全史 — 原写死自2018-01-01起, 而接口有更早的
      数据(茅台最早2005-12、五粮液2002-08): 池内9只共148条真实股东的历史
      增减持(高54/中45/低49)没拉进来, 与"全量刷新"及其他事件类型的口径不符"""
    ts_code = _norm_code(code)
    con = _conn(path)
    ev = []                                 # (ts_code, ev_date, ev_type, title, detail, risk)
    got = {}                                # 类型 → 本次拉到的行数
    win = _iso(_start(lookback)) if lookback else None     # 增量窗口起点(ISO)
    try:                                                     # 业绩预告
        df = T.call("forecast", "ts_code,ann_date,end_date,type,p_change_min,p_change_max",
                    ts_code=ts_code, start_date=_start(lookback),
                    end_date=time.strftime("%Y%m%d"))
        for _, r in df.iterrows():
            ad, ed = _ann(r.get("ann_date")), _ann(r.get("end_date"))
            if not ad or not ed:                             # 无公告日/无报告期的脏行
                continue
            t = _text(r.get("type"), "预告")
            lo, hi = _f(r.get("p_change_min")), _f(r.get("p_change_max"))
            # 无变动幅度的预告(如"不确定")原样落库成"预计变动nan%~nan%"
            chg = f"预计变动{lo}%~{hi}%" if lo is not None and hi is not None \
                else "未给出变动幅度"
            ev.append((ts_code, ad, "forecast",
                       f"业绩预告: {t}",
                       f"{ed}报告期 {chg}", FORECAST_RISK.get(t, "mid")))
        got["forecast"] = len(ev)
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 预告失败: {str(e)[:50]}")
    try:                                                     # 分红送转
        df = T.call("dividend", "ts_code,end_date,ann_date,cash_div,stk_div",
                    ts_code=ts_code)
        n0 = len(ev)
        for _, r in df.iterrows():
            cash, stk = _f(r.get("cash_div")), _f(r.get("stk_div"))
            ad, ed = _ann(r.get("ann_date")), _ann(r.get("end_date"))
            if not cash and not stk or not ad:
                continue
            # 只送转不派息的写成"每10股送N股"(原"每10股派None元 送N股")
            parts = []
            if cash:
                parts.append(f"每10股派{cash}元")
            if stk:
                parts.append(f"送{stk}股" if cash else f"每10股送{stk}股")
            ev.append((ts_code, ad, "dividend",
                       "分红: " + " ".join(parts),
                       f"{ed}年度分配" if ed else "分配方案(报告期未注明)", "low"))
        got["dividend"] = len(ev) - n0
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 分红失败: {str(e)[:50]}")
    try:                                                     # 股东增减持
        df = T.call("stk_holdertrade",
                    "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio",
                    ts_code=ts_code, start_date=_start(lookback),      # 全量=全史, 见docstring
                    end_date=time.strftime("%Y%m%d"))
        n0 = len(ev)
        for _, r in df.iterrows():
            vol = _f(r.get("change_vol"))                   # 单位=股(实测锚定), 恒为正
            cr = _f(r.get("change_ratio"))                  # 占流通股本比例%
            if vol is None or abs(vol) < 5e5:               # <50万股噪音滤除
                continue
            ad = _ann(r.get("ann_date"))
            if not ad:
                continue
            in_de = _text(r.get("in_de")).upper()
            if in_de.startswith("D"):
                down = True
            elif in_de.startswith("I"):
                down = False
            else:
                down = vol < 0 or (cr is not None and cr < 0)   # 无in_de: 负号兜底
            ev.append((ts_code, ad, "holdertrade",
                       f"{_clean_holder_name(r.get('holder_name')) or '未知股东'}"
                       f"{'减持' if down else '增持'}{_fmt_shares(abs(vol))}",
                       f"变动占比{cr}%" if cr is not None else "变动占比未披露",
                       _holdertrade_risk(down, cr)))
        got["holdertrade"] = len(ev) - n0
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 增减持失败: {str(e)[:50]}")
    try:                                                     # 停复牌
        df = T.call("suspend", "ts_code,suspend_date,resume_date", ts_code=ts_code)
        n0 = len(ev)
        for _, r in df.iterrows():
            sd = _iso(r.get("suspend_date"))
            if len(sd) != 10 or not sd[:4].isdigit():    # 缺停牌日的脏行不入库
                continue
            rd = _ann(r.get("resume_date"))              # 尚未复牌时为空: 不写成"至nan复牌"
            ev.append((ts_code, sd, "suspend",
                       "停牌", f"至{rd}复牌" if rd else "复牌日期未定", "mid"))
        got["suspend"] = len(ev) - n0
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
    # 旧行替换(见docstring): 窗口型事件在增量刷新时只清窗口内; 其余清该类型全部
    for t, n in got.items():
        if lookback and t in _WINDOWED_EVENTS:
            con.execute("DELETE FROM stock_events WHERE ts_code=? AND ev_type=? "
                        "AND ev_date>=?", (ts_code, t, win))
        elif n:
            con.execute("DELETE FROM stock_events WHERE ts_code=? AND ev_type=?",
                        (ts_code, t))
    con.executemany("INSERT INTO stock_events VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(ts_code,ev_date,ev_type,title) DO UPDATE SET "
                    "detail=excluded.detail,risk=excluded.risk", ev)
    con.commit()
    con.close()
    return len(ev)


def refresh_bs(code: str, path=None) -> int:
    """B1: 资产负债表全列(货币资金money_cap/应收/应付/存货/商誉/资产负债/
    流动资产负债 — 148字段实测定位, 货币资金= money_cap)"""
    ts_code = _norm_code(code)
    try:
        recs = _pull_stmt("balancesheet",
                          "ts_code,end_date,ann_date,f_ann_date,update_flag,money_cap,"
                          "accounts_receiv,acct_payable,inventories,goodwill,"
                          "total_assets,total_liab,total_cur_assets,total_cur_liab",
                          ts_code)
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 资产负债表失败 {ts_code}: {str(e)[:60]}")
        return -1
    rows = [(ts_code, _iso(r["end_date"]), _f(r.get("money_cap")),
             _f(r.get("accounts_receiv")), _f(r.get("acct_payable")),
             _f(r.get("inventories")), _f(r.get("goodwill")),
             _f(r.get("total_assets")), _f(r.get("total_liab")))
            for r in recs]
    con = _conn(path)
    con.executemany(
        "INSERT INTO stock_bs(ts_code,end_date,money_cap,accounts_receiv,"
        "acct_payable,inventories,goodwill,total_assets,total_liab) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(ts_code,end_date) DO UPDATE SET "
        "money_cap=excluded.money_cap,accounts_receiv=excluded.accounts_receiv,"
        "acct_payable=excluded.acct_payable,inventories=excluded.inventories,"
        "goodwill=excluded.goodwill,total_assets=excluded.total_assets,"
        "total_liab=excluded.total_liab", rows)
    con.commit()
    con.close()
    return len(rows)


def refresh_income(code: str, path=None) -> int:
    """B1: 利润表(营业收入/营业总收入/净利润)"""
    ts_code = _norm_code(code)
    try:
        recs = _pull_stmt("income",
                          "ts_code,end_date,ann_date,f_ann_date,update_flag,revenue,"
                          "total_revenue,n_income", ts_code)
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 利润表失败 {ts_code}: {str(e)[:60]}")
        return -1
    rows = [(ts_code, _iso(r["end_date"]), _f(r.get("revenue")),
             _f(r.get("total_revenue")), _f(r.get("n_income")))
            for r in recs]
    con = _conn(path)
    con.executemany("INSERT INTO stock_income VALUES(?,?,?,?,?) "
                    "ON CONFLICT(ts_code,end_date) DO UPDATE SET "
                    "revenue=excluded.revenue,total_revenue=excluded.total_revenue,"
                    "n_income=excluded.n_income", rows)
    con.commit()
    con.close()
    return len(rows)


def refresh_cashflow(code: str, path=None) -> int:
    """B1: 现金流量表(经营活动现金流净额)"""
    ts_code = _norm_code(code)
    try:
        recs = _pull_stmt("cashflow",
                          "ts_code,end_date,ann_date,f_ann_date,update_flag,n_cashflow_act",
                          ts_code)
    except Exception as e:                                   # noqa: BLE001
        print(f"[stock] 现金流量表失败 {ts_code}: {str(e)[:60]}")
        return -1
    rows = [(ts_code, _iso(r["end_date"]), _f(r.get("n_cashflow_act")))
            for r in recs]
    con = _conn(path)
    con.executemany("INSERT INTO stock_cashflow VALUES(?,?,?) "
                    "ON CONFLICT(ts_code,end_date) DO UPDATE SET "
                    "n_cashflow_act=excluded.n_cashflow_act", rows)
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
    out = [{"date": r[0],
            # 柱高用未舍入的亿股(小额解禁舍入到两位小数就成了0, 柱子高度为0);
            # 文案用 share_txt("6.10万股"/"1.01亿股") — 原图表提示与"未来90天"汇总
            # 也按亿股两位小数显示, 1122条解禁里644条显示成"0.0亿股"
            "share_yi": r[1] / 1e8 if r[1] else None,
            "share_txt": _fmt_float_shares(r[1]),
            "ratio": r[2], "holder": (r[3] or "未知股东")[:12],
            "future": r[0] > today, "big": (r[2] or 0) >= 5} for r in rows]
    soon = [x for x in out if x["future"] and x["date"] <=
            (dt.date.today() + dt.timedelta(days=90)).isoformat()]
    soon_yi = sum(x["share_yi"] or 0 for x in soon)
    return {"ok": True, "code": ts_code, "n": len(out), "data": out,
            "soon": {"n": len(soon),
                     "total_yi": round(soon_yi, 2),
                     "total_txt": _fmt_float_shares(soon_yi * 1e8) if soon else "--",
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
    weekly=True加季频全史(finance/bs/holders/float)且events改全量。
    调度: 19:30每晚 + 周五19:35季频(jobs挂载)。
    D1-35: 周频的events全量刷新(近1年窗口→全史) — 借refresh_events的
    按类型替换清掉旧标题口径的残留行(增量刷新只清窗口内, 清不到更早的)。"""
    import datetime as dt
    r = {"daily": refresh_daily(path=path), "stocks": 0, "weekly": weekly}
    con = _conn(path)
    pool = [r2[0] for r2 in con.execute("SELECT ts_code FROM stock_pool")]
    con.close()
    for ts_code in pool:
        n = 0
        n += max(0, refresh_margin(ts_code, path, lookback=45))
        n += max(0, refresh_val(ts_code, path, lookback=45))
        n += max(0, refresh_events(ts_code, path, lookback=None if weekly else 30))
        if weekly:
            n += max(0, refresh_finance(ts_code, path))
            n += max(0, refresh_bs(ts_code, path))
            n += max(0, refresh_income(ts_code, path))       # D1-35: 营收/净利随季更新
            n += max(0, refresh_cashflow(ts_code, path))     # D1-35: 经营现金流
            n += max(0, refresh_holders(ts_code, path))
            n += max(0, refresh_float(ts_code, path))
        r["stocks"] += 1
        print(f"  {ts_code}: +{n}行")
    print(f"[stock] 夜间链{'(含季频)' if weekly else ''}: {r['stocks']}只 +{r['daily']}日频行")
    return r


def full_refresh(code: str, path=None) -> dict:
    """A5手动单只全量重拉(全史所有数据集)。
    D1-35: 补上利润表/现金流量表 — 自称"所有数据集"却漏了这两张(B1的营收/
    净利/经营现金流图), 周频夜间链同样不含 → 没点过[数据更新]的股票这三项
    永远是空的(页面只在这类数据全无时才自动拉)。"""
    ts_code = _norm_code(code)
    r = add_stock(ts_code, path)
    if not r.get("ok"):
        return r
    n = {"margin": refresh_margin(ts_code, path),
         "val": refresh_val(ts_code, path),
         "finance": refresh_finance(ts_code, path),
         "bs": refresh_bs(ts_code, path),
         "income": refresh_income(ts_code, path),
         "cashflow": refresh_cashflow(ts_code, path),
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
