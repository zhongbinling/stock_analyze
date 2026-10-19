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

-- ---------------- 第二阶段: 板块温度计数据底座 (S5) ----------------

CREATE TABLE IF NOT EXISTS sectors(            -- 板块字典(东财BK)
  code TEXT PRIMARY KEY,                       -- BKxxxx
  name TEXT, kind TEXT,                        -- industry | concept
  enabled INTEGER DEFAULT 1,                   -- 概念板块默认0, 启用后才抓日线
  first_date TEXT, last_date TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS sector_daily(       -- 板块日行情
  code TEXT, date TEXT,
  open REAL, close REAL, high REAL, low REAL,
  amount REAL,                                -- 成交额(元)
  up_count INTEGER, down_count INTEGER,        -- 板块涨跌家数(清单快照, 采集日起积累)
  PRIMARY KEY(code, date));

CREATE TABLE IF NOT EXISTS sector_pool_stat(   -- 板块级涨停结构(THS hybk聚合)
  date TEXT, code TEXT,
  zt_count INTEGER, dt_count INTEGER, zb_count INTEGER,
  lb_max INTEGER, lb2_count INTEGER,
  PRIMARY KEY(date, code));

CREATE TABLE IF NOT EXISTS sector_fundflow(    -- 板块资金流历史(约最近100日)
  code TEXT, date TEXT,
  main_net REAL, super_net REAL, large_net REAL,
  PRIMARY KEY(code, date));

CREATE TABLE IF NOT EXISTS sector_alias(       -- THS行业名 → BK码 别名
  name TEXT PRIMARY KEY, code TEXT);

CREATE TABLE IF NOT EXISTS calendar(            -- 交易日历(tushare, 有token时同步)
  cal_date TEXT PRIMARY KEY,                   -- YYYYMMDD
  is_open INTEGER,                             -- 1交易日
  pretrade_date TEXT);

CREATE TABLE IF NOT EXISTS margin_daily(        -- 融资融券汇总(tushare, 全市场+分所)
  date TEXT PRIMARY KEY,
  rzye REAL, rzmre REAL, rqye REAL, rzrqye REAL,   -- 全市场合计(亿→元)
  sse_rzrqye REAL, szse_rzrqye REAL);

CREATE TABLE IF NOT EXISTS sector_scores(       -- 板块温度结果(S6, 重算整表重建)
  date TEXT, code TEXT,
  score REAL,            -- 纵向温度 -100~100
  rank_pct REAL,         -- 横向热度: 当日全板块百分位 0~100
  stage TEXT, components TEXT,   -- JSON分量明细
  PRIMARY KEY(date, code));
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # 老库迁移: 新增列
    for col_def in ("jl_rate REAL", "yzt_count INTEGER", "zt_source TEXT"):
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


# ---------------- D1-7: 全量重建的 staging 原子切换 ----------------

def make_staging(conn, table):
    """建/清空 staging 表(动态复制schema); 返回表名"""
    st = f"{table}_staging"
    conn.execute(f"DROP TABLE IF EXISTS {st}")
    conn.execute(f"CREATE TABLE {st} AS SELECT * FROM {table} WHERE 0")
    conn.commit()
    return st


def atomic_swap(conn, table, min_keep=0.95, idx_cols=("date",)):
    """staging→正式 原子切换(D1-7): 校验(非空/行数≥旧×min_keep/最新日期不回退)
    → 单事务 RENAME 三连; 校验不过抛 RuntimeError 保留旧表(旧表全程可读)。
    idx_cols: 切换后重建索引(staging CREATE AS 会丢PK/索引, 左前缀覆盖常用查询)。"""
    st = f"{table}_staging"
    n_old = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    n_new = conn.execute(f"SELECT COUNT(*) FROM {st}").fetchone()[0]
    d_old = conn.execute(f"SELECT MAX(date) FROM {table}").fetchone()[0]
    d_new = conn.execute(f"SELECT MAX(date) FROM {st}").fetchone()[0]
    why = []
    if n_new == 0:
        why.append("staging 为空")
    if n_old and n_new < n_old * min_keep:
        why.append(f"行数 {n_new} < 旧 {n_old}×{min_keep}")
    if d_old and (not d_new or str(d_new) < str(d_old)):
        why.append(f"最新日期回退 {d_old}→{d_new}")
    if why:
        raise RuntimeError(f"[swap] {table} 校验不过, 保留旧表: {'; '.join(why)}")
    conn.commit()                                    # 清理隐式事务, 确保切换原子
    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
    cur.execute(f"ALTER TABLE {st} RENAME TO {table}")
    cur.execute(f"DROP TABLE {table}_old")
    conn.commit()
    for cols in (idx_cols or ()):                    # 重建查询索引(唯一性由全量重建保证)
        cols = (cols,) if isinstance(cols, str) else tuple(cols)
        name = f"ix_{table}_{'_'.join(cols)}"
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({','.join(cols)})")
    conn.commit()
    print(f"[swap] {table}: {n_old} → {n_new} 行 (原子切换, 索引已重建)")


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
