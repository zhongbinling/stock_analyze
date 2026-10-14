# -*- coding: utf-8 -*-
"""SQLite 存储层: 指数日线 / 短线情绪原始数据 / 情绪分结果"""
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "sentiment.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_daily(
  code TEXT NOT NULL,
  date TEXT NOT NULL,            -- YYYY-MM-DD
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL, pct_chg REAL,
  PRIMARY KEY(code, date));

CREATE TABLE IF NOT EXISTS mood_raw(
  date TEXT PRIMARY KEY,
  zt_count INTEGER,     -- 涨停家数
  dt_count INTEGER,     -- 跌停家数
  zb_count INTEGER,     -- 炸板家数(东财口径)
  zb_rate REAL,         -- 炸板率% = 炸板/(涨停+炸板)
  lb_max INTEGER,       -- 最高连板数
  lb2_count INTEGER,    -- 2连板及以上家数
  prev_zt_chg REAL,     -- 昨日涨停股今日平均涨幅%(仅东财近月可得, 预留V2.1)
  jl_rate REAL,         -- 接力率% = 今日2板+家数/昨日涨停家数(同花顺口径)
  yzt_count INTEGER,    -- 昨日涨停家数(同花顺)
  up_count INTEGER,     -- 上涨家数(仅当日可得)
  down_count INTEGER);  -- 下跌家数(仅当日可得)

CREATE TABLE IF NOT EXISTS sentiment(
  date TEXT PRIMARY KEY,
  score REAL, stage TEXT,
  components TEXT,      -- JSON: 各分量分位分
  weights TEXT);        -- JSON: 实际生效权重(归一化后)
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # 老库迁移: 新增列
    for col_def in ("jl_rate REAL", "yzt_count INTEGER"):
        try:
            conn.execute(f"ALTER TABLE mood_raw ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # 已存在
    return conn


def upsert_index(conn, code, date, o, h, l, c, vol, amount, pct_chg):
    cur = conn.execute(
        "INSERT INTO index_daily(code,date,open,high,low,close,volume,amount,pct_chg) "
        "VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(code,date) DO UPDATE SET open=excluded.open,high=excluded.high,"
        "low=excluded.low,close=excluded.close,volume=excluded.volume,"
        "amount=excluded.amount,pct_chg=excluded.pct_chg",
        (code, date, o, h, l, c, vol, amount, pct_chg))
    conn.commit()
    return cur.rowcount


def upsert_mood(conn, date, **kw):
    """部分字段更新: 只写传入的字段, 不覆盖其它列"""
    if not kw:
        return
    cols, vals = list(kw.keys()), [kw[c] for c in kw]
    sets = ",".join(f"{c}=excluded.{c}" for c in cols)
    conn.execute(
        f"INSERT INTO mood_raw(date,{','.join(cols)}) VALUES(?,{','.join('?' * len(cols))}) "
        f"ON CONFLICT(date) DO UPDATE SET {sets}",
        [date] + vals)
    conn.commit()


def get_mood_dates(conn) -> set:
    rows = conn.execute("SELECT date FROM mood_raw WHERE zt_count IS NOT NULL").fetchall()
    return {r[0] for r in rows}


def trading_dates(conn, primary_code, start=None, end=None):
    """主指数的交易日序列(升序), 可选闭区间过滤, 格式 YYYY-MM-DD"""
    sql = "SELECT date FROM index_daily WHERE code=?"
    args = [primary_code]
    if start:
        sql += " AND date>=?"
        args.append(start)
    if end:
        sql += " AND date<=?"
        args.append(end)
    sql += " ORDER BY date"
    return [r[0] for r in conn.execute(sql, args)]
