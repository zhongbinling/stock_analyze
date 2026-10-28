# -*- coding: utf-8 -*-
"""D1-17 R3+R4: 实时行情采集 — 独立库 realtime.db, rt_day 按天一行

轮询线程: 交易时段判定 → 快照采样(量价+实时情绪分+ETF合计) → 内存缓冲
→ 当日行 upsert。页面/回看 API 的数据源。独立于核心库: 不写 sentiment.db
(只读), 备份矩阵注册后自动纳入日/周/手动备份。
"""
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "core"
sys.path.insert(0, str(CORE))

RT_DB = CORE / "data" / "realtime.db"
ETF_QT_URL = ("https://qt.gtimg.cn/q=sz159901,sz159915,sz159919,sz159949,"
              "sh510050,sh510300,sh510500,sh512100")

_state = {"date": None, "samples": [], "updated_at": None}
_lock = threading.Lock()
_thread = None
INTERVAL_S = 15                      # 采样间隔(秒), 盘中档


def ensure_db(path=None) -> None:
    path = path or RT_DB                     # 运行时取模块属性(可测注入)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE IF NOT EXISTS rt_day(
      date TEXT PRIMARY KEY, updated_at TEXT, n INTEGER, payload TEXT)""")
    con.commit()
    con.close()


def is_market_time(now: datetime = None) -> bool:
    """交易时段判定: 周一~五 且 (09:15~11:35 或 12:55~15:05)。
    节假日由"快照日≠今天"兜底(qt 非交易日返回上一交易日数据)。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= hm <= 11 * 60 + 35 or 12 * 60 + 55 <= hm <= 15 * 60 + 5


def _fetch_etf_amount_yi() -> float | None:
    """8只国家队ETF 当日累计成交额合计(亿) — qt 多码单call"""
    from probe_rt import _curl, parse_qt
    code, body = _curl(ETF_QT_URL, timeout=6)
    if code != 200:
        return None
    segs = [parse_qt(s + ";") for s in body.split(";") if "v_" in s]
    try:
        return round(sum(float(s["amount_wan"]) for s in segs if s) / 1e4, 2)
    except (KeyError, TypeError, ValueError):
        return None


def tick(now: datetime = None) -> str:
    """单次采样(可测): idle=非交易时段, stale=快照日乖离(节假日/盘前),
    ok=已采样落库; 异常吞掉返回 err(轮询不中断)"""
    now = now or datetime.now()
    if not is_market_time(now):
        return "idle"
    try:
        import rt_score
        from db import connect
        conn = connect()
        try:
            snap = rt_score.fetch_snapshot(conn)
            today = now.strftime("%Y-%m-%d")
            if snap.get("date") != today:
                return "stale"                  # 节假日/盘前: qt返回上日数据
            rt = rt_score.compute_rt(conn, snap)
        finally:
            conn.close()
        sample = {"t": now.strftime("%H:%M:%S"),
                  "close": snap.get("close"), "cumvol": snap.get("volume1"),
                  "cumamt": round((snap.get("amount1") or 0) / 1e8, 1),
                  "score": rt["score"], "v1": rt["v1"], "stage": rt["stage"],
                  "zt": snap.get("zt_count"), "jl": snap.get("jl_rate"),
                  "etf": _fetch_etf_amount_yi()}
        with _lock:
            if _state["date"] != today:         # 跨日滚动
                _state.update(date=today, samples=[])
            _state["samples"].append(sample)
            _state["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            _persist_locked()
        return "ok"
    except Exception as e:                      # noqa: BLE001
        print(f"[rt] tick失败(轮询继续): {e}")
        return "err"


def _persist_locked(path=None) -> None:
    path = path or RT_DB                     # 运行时取模块属性(可测注入)
    ensure_db(path)
    con = sqlite3.connect(str(path))
    con.execute(
        "INSERT INTO rt_day(date,updated_at,n,payload) VALUES(?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET updated_at=excluded.updated_at,"
        "n=excluded.n,payload=excluded.payload",
        (_state["date"], _state["updated_at"], len(_state["samples"]),
         json.dumps({"samples": _state["samples"]}, ensure_ascii=False)))
    con.commit()
    con.close()


def _meta() -> dict:
    """对比基准: 昨收/昨日累计额(指数)/昨日ETF合计额 — 只读 sentiment.db"""
    try:
        import yaml
        cfg = yaml.safe_load((CORE / "config.yaml").read_text(encoding="utf-8"))
        pri = cfg["index"]["primary"]
        con = sqlite3.connect(f"file:{CORE / 'data' / 'sentiment.db'}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT date,close,amount FROM index_daily WHERE code=? "
                "ORDER BY date DESC LIMIT 2", (pri,)).fetchall()
            etf = con.execute(
                "SELECT date, SUM(amount) FROM etf_daily GROUP BY date "
                "ORDER BY date DESC LIMIT 1").fetchone()
        finally:
            con.close()
        out = {"prev_date": rows[0][0] if rows else None,
               "prev_close": rows[0][1] if rows else None,
               "prev_amt_yi": round(rows[0][2] / 1e8, 0) if rows and rows[0][2] else None}
        if etf and etf[1]:                      # etf_daily.amount 单位=千元
            out["prev_etf_yi"] = round(etf[1] / 1e5, 0)
            out["prev_etf_date"] = etf[0]
        return out
    except Exception as e:                      # noqa: BLE001
        return {"error": str(e)}


def latest() -> dict:
    """页面数据: 当日样本 + live态 + 对比基准 + 采集开关态"""
    with _lock:
        state = {"date": _state["date"], "samples": list(_state["samples"]),
                 "updated_at": _state["updated_at"]}
    state["live"] = bool(state["samples"]) and is_market_time()
    state["meta"] = _meta()
    try:
        if _app is not None:
            with _app.app_context():
                from server import jobs as J
                state["enabled"] = J.get_setting("rt_enabled", "1") == "1"
    except Exception:                           # noqa: BLE001
        pass
    return state


def day(date_iso: str) -> dict | None:
    """R4 回看: 读取已存档交易日"""
    ensure_db()
    con = sqlite3.connect(str(RT_DB))
    row = con.execute("SELECT updated_at, payload FROM rt_day WHERE date=?",
                      (date_iso,)).fetchone()
    con.close()
    if not row:
        return None
    return {"date": date_iso, "updated_at": row[0],
            "samples": json.loads(row[1]).get("samples", [])}


def days() -> list:
    ensure_db()
    con = sqlite3.connect(str(RT_DB))
    out = [r[0] for r in con.execute(
        "SELECT date FROM rt_day ORDER BY date DESC LIMIT 120")]
    con.close()
    return out


_app = None


def _loop() -> None:
    from server import jobs as J
    while True:
        try:
            if _app is not None:
                with _app.app_context():       # get_setting 需 app 上下文(同调度器)
                    enabled = J.get_setting("rt_enabled", "1") == "1"
            else:
                enabled = True
            if enabled:
                tick()
            time.sleep(INTERVAL_S)
        except Exception:                       # noqa: BLE001
            time.sleep(INTERVAL_S)


def ensure_started(app=None) -> None:
    """create_app 挂载(TESTING/SA_NO_JOBS 之外); 单例守护线程"""
    global _thread, _app
    if _thread is not None and _thread.is_alive():
        return
    _app = app
    _thread = threading.Thread(target=_loop, name="rt-poller", daemon=True)
    _thread.start()
