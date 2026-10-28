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
        sample = _sample_from(now.strftime("%H:%M:%S"), snap, rt,
                              _fetch_etf_amount_yi())
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


def _sample_from(t: str, snap: dict, rt: dict, etf) -> dict:
    """tick/盘后补采共用的样本构造"""
    return {"t": t, "close": snap.get("close"), "cumvol": snap.get("volume1"),
            "cumamt": round((snap.get("amount1") or 0) / 1e8, 1),
            "score": rt["score"], "v1": rt["v1"], "stage": rt["stage"],
            "zt": snap.get("zt_count"), "jl": snap.get("jl_rate"), "etf": etf}


def _hydrate_locked() -> None:
    """R5a: 空态时从 rt_day 恢复当日内存态(重启后已采数据可见/续采)"""
    today = datetime.now().strftime("%Y-%m-%d")
    if _state["date"] == today and _state["samples"]:
        return
    d = day(today)
    if d and d["samples"]:
        _state.update(date=d["date"], samples=d["samples"],
                      updated_at=d["updated_at"])


def _minute_samples(amt1f: float, amt2f: float) -> list:
    """R5b: ifzq 双指数分钟线 → 分时样本(价格/累计量; 累计额按双指数
    总量比的线性估算 — 分钟线无分时额字段, 形状近似够用, 终值点为真值)"""
    from probe_rt import _curl
    import json as _json
    out = []
    series = {}
    for code in ("sh000001", "sz399106"):
        _, body = _curl(f"https://web.ifzq.gtimg.cn/appstock/app/minute/query"
                        f"?code={code}", timeout=10)
        try:
            arr = _json.loads(body)["data"][code]["data"]["data"]
        except (ValueError, KeyError, TypeError):
            return []
        series[code] = {x.split()[0]: x.split() for x in arr}
    v1f = None
    base = series["sh000001"]
    last_v = {}
    for hhmm in sorted(base):
        p1, v1 = base[hhmm][1], float(base[hhmm][2])
        last_v = {"v1": v1}
        v2 = float(series["sz399106"].get(hhmm, ["", "", last_v.get("v2", 0)])[2]
                   or 0)
        last_v["v2"] = v2
        if v1f is None:
            v1f = v1              # 首点即基准(开市分钟)
        out.append({"t": f"{hhmm[:2]}:{hhmm[2:]}",
                    "close": float(p1), "cumvol": v1,
                    "score": None, "v1": None, "stage": None,
                    "zt": None, "jl": None, "etf": None,
                    "cumamt": None})
    # 额度线性估算: 用双指数末点总量与终值额标定
    if out and (amt1f or amt2f) and last_v:
        tot_f = (last_v.get("v1") or 0) + (last_v.get("v2") or 0)
        if tot_f > 0:
            amt_f = ((amt1f or 0) + (amt2f or 0)) / 1e8
            v2 = 0.0
            for smp, hhmm in zip(out, sorted(base)):
                v1 = smp["cumvol"]
                v2 = float(series["sz399106"].get(hhmm, ["", "", v2])[2] or v2)
                smp["cumamt"] = round(amt_f * (v1 + v2) / tot_f, 1)
    return out


_day_cache = {}


def backfill_today(now: datetime = None) -> str | None:
    """R5b 盘后补采: 交易日收盘后当日无存档 → 分钟线重建+终值融合, 一次。
    返回 ok=已补 / have=已有 / None=条件不满足(未收盘/周末/快照日乖离)"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or now.hour * 60 + now.minute <= 15 * 60 + 5:
        return None
    if _day_cache.get(today) or day(today):
        _day_cache[today] = True
        return "have"
    snap = rt = etf = None
    try:
        import rt_score
        from db import connect
        conn = connect()
        try:
            snap = rt_score.fetch_snapshot(conn)
            if snap.get("date") != today:
                return None              # 盘前/节假日: qt返回上日, 不造幻影日
            rt = rt_score.compute_rt(conn, snap)
        finally:
            conn.close()
        etf = _fetch_etf_amount_yi()
    except Exception as e:                # noqa: BLE001
        print(f"[rt] 盘后补采终值失败: {e}")
        return None
    samples = _minute_samples(snap.get("amount1") or 0, snap.get("amount2") or 0)
    final = _sample_from("15:00:00", snap, rt, etf)
    if samples:
        # 分时样本末点融合终值(score/etf等分时线没有的字段)
        samples[-1].update({k: v for k, v in final.items()
                            if v is not None and samples[-1].get(k) is None})
    else:
        samples = [final]
    with _lock:
        _state.update(date=today, samples=samples,
                      updated_at=now.strftime("%Y-%m-%d %H:%M:%S"))
        _persist_locked()
    _day_cache[today] = True
    print(f"[rt] 盘后补采 {today}: {len(samples)} 点(分时重建+收盘终值)")
    return "ok"


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
        _hydrate_locked()                 # R5a: 重启后恢复当日已采数据
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
                if tick() == "idle":
                    backfill_today()      # R5b: 盘后一次(内部自检幂等)
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
