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


# ---------------- D1-11 F2: 基础数据写入守卫(批次校验+事务零污染) ----------------

def _d8(d) -> str:
    """ISO/YYYYMMDD → YYYYMMDD(日期比对口径)"""
    return str(d).replace("-", "")[:8]


def _is_bad_pk(v) -> bool:
    """主键空值判定: None/空串/NaN(pandas缺失, v!=v; sqlite会把nan存成NULL主键)"""
    return v is None or v == "" or v != v


def guarded_day_write(conn, sql, rows, day, *, table, date_col="trade_date",
                      date_idx=1, pk_idx=0) -> int:
    """逐日批次守卫写入(raw库 daily/moneyflow): 校验→单事务→行数核对→commit。

    校验: 批次非空 / 日期列全等于目标日 / 批内主键无重复且非空;
    写后: 该日行数 ≥ 批次主键数, 不符即 rollback 抛 RuntimeError(主表零污染)。
    网络异常日由调用方既有 try/except 吞掉计数; 守卫拒绝同样按"该日失败"处理,
    连续异常会触发调用方的中断阈值 → 坏数据不会静默入库。
    """
    if not rows:
        raise RuntimeError(f"[guard] {table} {day}: 空批次(拒绝写入)")
    d8 = _d8(day)
    bad_date = [r[date_idx] for r in rows if _d8(r[date_idx]) != d8]
    if bad_date:
        raise RuntimeError(
            f"[guard] {table} {day}: 日期列混入他日 {bad_date[:3]}(拒绝写入)")
    n_pk = len({r[pk_idx] for r in rows})
    # 空值判定含 NaN: pandas缺失(float'nan') != 自身, 且sqlite会把nan存成NULL主键行
    if any(_is_bad_pk(r[pk_idx]) for r in rows):
        raise RuntimeError(f"[guard] {table} {day}: 主键存在空值/NaN(拒绝写入)")
    if n_pk != len(rows):
        raise RuntimeError(
            f"[guard] {table} {day}: 批内主键重复 {len(rows) - n_pk} 行(拒绝写入)")
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.executemany(sql, rows)
        n_after = cur.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {date_col}=?",
            (str(rows[0][date_idx]),)).fetchone()[0]
        if n_after < n_pk:
            raise RuntimeError(
                f"[guard] {table} {day}: 写后行数 {n_after} < 批次 {n_pk}(回滚)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def guarded_batch(conn, sql, rows, *, label, check=None) -> int:
    """通用批次守卫(calendar 等): check(rows)->拒写原因(None=通过) + 单事务写入。

    空批次直接拒; check 抛出原因或事务失败均 rollback, 主表零污染。
    """
    if not rows:
        raise RuntimeError(f"[guard] {label}: 空批次(拒绝写入)")
    if check is not None:
        why = check(rows)
        if why:
            raise RuntimeError(f"[guard] {label}: {why}(拒绝写入)")
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        cur.executemany(sql, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


# ---------------- D1-12 F2收尾: 可组合批次校验构件 ----------------

def chk_same(idx: int, expect):
    """列恒等校验: 所有行 rows[r][idx] == expect(单日/单码批次防混入)"""
    def f(rows):
        bad = [r[idx] for r in rows if r[idx] != expect][:3]
        return None if not bad else f"{idx}列混入异值 {bad}"
    return f


def chk_unique(*idxs):
    """组合键批内唯一校验(防上游重复行被OR REPLACE吞掉)"""
    def f(rows):
        keys = [tuple(r[i] for i in idxs) for r in rows]
        return None if len(set(keys)) == len(keys) \
            else f"组合键{idxs}批内重复 {len(keys) - len(set(keys))} 行"
    return f


def chk_range(idx: int, lo, hi):
    """值域校验(容None; NaN按越界处理)"""
    def f(rows):
        for r in rows:
            v = r[idx]
            if v is None:
                continue
            if v != v or v < lo or v > hi:   # v!=v 即 NaN
                return f"{idx}列越界 {v!r} (合法 {lo}~{hi})"
        return None
    return f


def all_checks(*fns):
    """组合多个check → 依次执行, 首个非None原因返回"""
    def f(rows):
        for fn in fns:
            why = fn(rows)
            if why:
                return why
        return None
    return f


# mood 单行值域(越界=上游脏值, 拒写防静默污染; 界限宽松只拦明显异常)
_MOOD_DOMAIN = {"zt_count": (0, 20000), "dt_count": (0, 20000), "zb_count": (0, 20000),
                "lb2_count": (0, 20000), "yzt_count": (0, 20000),
                "up_count": (0, 20000), "down_count": (0, 20000),
                "lb_max": (0, 60), "zb_rate": (0, 100), "jl_rate": (0, 1000),
                "prev_zt_chg": (-100, 100)}
_MOOD_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


def upsert_mood(conn, date, **kw):
    """部分字段更新: 只写传入的字段, 不覆盖其它列。

    D1-12 F2: 值域校验(家数/率/连板高度/日期格式), 越界抛RuntimeError —
    调用方(fetch_pools等)按日容错记失败, 脏值不再静默入库。
    """
    if not kw:
        return
    if not _MOOD_DATE_RE.match(str(date or "")):
        raise RuntimeError(f"mood 日期非法: {date!r}")
    for k, v in kw.items():
        dom = _MOOD_DOMAIN.get(k)
        if dom and v is not None and (v != v or v < dom[0] or v > dom[1]):
            raise RuntimeError(f"mood {date} {k}={v!r} 越界(合法 {dom[0]}~{dom[1]})")
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
