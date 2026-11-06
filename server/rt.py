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
from datetime import datetime, timedelta
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
            if not snap:
                return "stale"                  # D1-25: qt失败跳过本tick
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
        _heal_gaps(now, snap)                   # 盘中断块自愈(前缀+中间缺口)
        return "ok"
    except Exception as e:                      # noqa: BLE001
        print(f"[rt] tick失败(轮询继续): {e}")
        return "err"


_gap_cooldown = {"until": None}                 # 断块自愈失败冷却(源恢复重试)
_GAP_MIN = 3                                    # 相邻样本间隔>N交易分钟=断块


def _tmin(t: str) -> int:
    """'HH:MM[:SS]' → 交易分钟序号(09:30=0; 剔午休, 上午121分钟)"""
    mo = int(t[:2]) * 60 + int(t[3:5])
    if mo < 12 * 60:
        return mo - (9 * 60 + 30)
    return mo - 13 * 60 + 121


def _heal_gaps(now: datetime = None, snap: dict = None) -> int:
    """盘中断块自愈(定轴断块批): 开盘前缀(tick晚启动) + **中间缺口**(服务器
    盘中停机重启)统一处理 — 相邻样本交易分钟间隔>GAP_MIN 或首样本>09:35
    → 分钟线源拉缺失块原位拼接(严格介于两侧样本之间的整分钟, 不与tick重叠)
    + rt_series逐块情绪 + ETF分钟映射。检测O(n)内存级每tick可跑, 网络仅
    检出时; 失败冷却10分钟(源恢复重试); 午休边界(11:30→13:00)经交易分钟
    序号化后间隔=1不算缺口; 补后缺口闭合天然幂等。返回补入点数。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    cu = _gap_cooldown.get("until")
    if cu is not None and now < cu:
        return 0
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 40:                        # 09:40前不判(tick起步留余量)
        return 0
    with _lock:
        _hydrate_locked()
        cur = _state["samples"]
        if _state["date"] != today or not cur:
            return 0
        cur = list(cur)
    # 锁外检测: 开盘前缀 + 相邻间隔断块
    segs = []                                    # [(lo|None, hi)] hi=锚样本t
    if cur[0]["t"] > "09:35":
        segs.append((None, cur[0]["t"]))
    prev = None
    for x in cur:
        if prev is not None and _tmin(x["t"]) - _tmin(prev["t"]) > _GAP_MIN:
            segs.append((prev["t"], x["t"]))
        prev = x
    if not segs:
        return 0
    stage = cur[-1].get("stage")
    try:
        minutes = _minute_samples((snap or {}).get("amount1") or 0)
        if not minutes:
            raise RuntimeError("分钟源空")
        blocks = {}                              # hi锚 → 补入块(严格介于)
        for lo, hi in segs:
            lo5 = lo[:5] if lo else None
            blk = [dict(m) for m in minutes
                   if (lo5 is None or m["t"] > lo5) and m["t"] < hi[:5]]
            if not blk:
                continue
            blocks[hi] = blk
        if not blocks:
            _gap_cooldown["until"] = now + timedelta(minutes=10)
            return 0
        import rt_score
        from db import connect
        conn = connect()
        try:
            for blk in blocks.values():         # 逐块情绪(框架改末行)
                scores = rt_score.rt_series(conn, snap or {}, blk)
                for m, sc in zip(blk, scores):
                    if sc is not None:
                        m["score"] = m["v1"] = sc
                    if m.get("stage") is None and stage:
                        m["stage"] = stage
        finally:
            conn.close()
        etf = _fetch_etf_amount_yi()            # ETF分钟累计(失败容忍)
        if etf:
            em = _minute_etf(etf)
            for blk in blocks.values():
                for m in blk:
                    if m["t"] in em:
                        m["etf"] = em[m["t"]]
        added = 0
        with _lock:
            if _state["date"] != today or not _state["samples"]:
                return 0
            out = []
            for x in _state["samples"]:
                blk = blocks.get(x["t"])
                if blk:
                    out.extend(blk)
                    added += len(blk)
                out.append(x)
            _state["samples"] = out
            _state["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            _persist_locked()
            print(f"[rt] 盘中断块自愈: 补{added}点({len(blocks)}块)")
        _gap_cooldown["until"] = None
        return added
    except Exception as e:                      # noqa: BLE001
        print(f"[rt] 盘中断块自愈失败: {e}")
        _gap_cooldown["until"] = now + timedelta(minutes=10)
        return 0


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


def _minute_etf(etf_final: float) -> dict:
    """D1-23: 8只ETF分钟线累计量 → 累计成交额估算(终值qt真值锚定)。
    返回 {"HH:MM": 亿}; 失败返回空(仅收盘点有真值)。"""
    from probe_rt import _curl
    import json as _json
    volsum, keys = {}, []
    for code in ("sz159901", "sz159915", "sz159919", "sz159949",
                 "sh510050", "sh510300", "sh510500", "sh512100"):
        _, body = _curl(f"https://web.ifzq.gtimg.cn/appstock/app/minute/query"
                        f"?code={code}", timeout=8)
        try:
            arr = _json.loads(body)["data"][code]["data"]["data"]
        except (ValueError, KeyError, TypeError):
            return {}
        for x in arr:
            p = x.split()
            if len(p) < 3:
                continue
            hhmm = f"{p[0][:2]}:{p[0][2:]}"
            volsum[hhmm] = volsum.get(hhmm, 0.0) + float(p[2])
    if not volsum or not etf_final:
        return {}
    tot = max(volsum.values()) or 1.0
    out, mx = {}, 0.0
    for k, v in sorted(volsum.items()):     # D1-25: 累计口径cummax防源毛刺回退
        mx = max(mx, round(etf_final * v / tot, 1))
        out[k] = mx
    return out


def _minute_samples(amt1f: float) -> list:
    """R5b: ifzq 上证分钟线 → 分时样本(价格/累计量; 累计额按量比线性
    估算 — 分钟线无分时额字段; **口径=上证**, 与实时tick的cumamt(qt
    amount1)一致, 两种模式同图不跳变; 末点融合终值后为真值)"""
    from probe_rt import _curl
    _, body = _curl("https://web.ifzq.gtimg.cn/appstock/app/minute/query"
                    "?code=sh000001", timeout=10)
    try:
        arr = json.loads(body)["data"]["sh000001"]["data"]["data"]
    except (ValueError, KeyError, TypeError):
        return []
    base = {p[0]: p for p in (x.split() for x in arr) if len(p) >= 3}
    if not base:
        return []
    keys = sorted(base)
    out = [{"t": f"{h[:2]}:{h[2:]}", "close": float(base[h][1]),
            "cumvol": float(base[h][2]), "cumamt": None, "score": None,
            "v1": None, "stage": None, "zt": None, "jl": None, "etf": None}
           for h in keys]
    vol_f = out[-1]["cumvol"]
    if amt1f and vol_f > 0:
        amt_f = amt1f / 1e8
        for smp in out:
            smp["cumamt"] = round(amt_f * smp["cumvol"] / vol_f, 1)
    return out


_day_cache = {}


def backfill_today(now: datetime = None) -> str | None:
    """R6a 盘后/盘前补采(目标日制): 目标日=快照源当前服务的交易日。
    - 目标=今天且未收盘 → None(盘中由tick负责)
    - 已有行 n≥100(健康) → have(按日缓存不再试)
    - 否则重建: 当日盘后空行 / **盘前服务昨日的瘦行**(降级日自愈,
      原守卫"快照日≠今天即放弃"浪费了预凌晨窗口)
    返回 ok=已建/重建 / have=已有健康行 / None=条件不满足"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    hm = now.hour * 60 + now.minute
    snap = rt = etf = None
    try:
        import rt_score
        from db import connect
        conn = connect()
        try:
            snap = rt_score.fetch_snapshot(conn)
            target = snap.get("date")
            if not target or snap.get("close") is None:
                return None                     # D1-25: 无价格快照不补采
            if target == today and hm <= 15 * 60 + 5:
                return None                  # 盘中当日: tick负责
            rt = rt_score.compute_rt(conn, snap)
        finally:
            conn.close()
        etf = _fetch_etf_amount_yi()
    except Exception as e:                    # noqa: BLE001
        print(f"[rt] 补采终值失败: {e}")
        return None
    if _day_cache.get(target):
        return "have"
    existing = day(target)
    if existing and len(existing["samples"]) >= 100:
        # D1-23b: 午间重启会丢上午(实测10-30首样本12:55) — 盘后若首样本
        # 晚于09:35(缺早盘)仍重建为全日分钟线
        first_t = existing["samples"][0]["t"] if existing["samples"] else "99"
        if first_t <= "09:35:00":
            _day_cache[target] = True
            return "have"
        print(f"[rt] {target} 为半日样本(自{first_t}起), 盘后重建全日")
    samples = _minute_samples(snap.get("amount1") or 0)
    final = _sample_from("15:00:00", snap, rt, etf)
    if samples:
        # D1-23: 逐分钟情绪(框架一次+末行改写)与ETF累计(量比估算)
        try:
            import rt_score
            from db import connect as _conn2
            c2 = _conn2()
            try:
                scores = rt_score.rt_series(c2, snap, samples)
            finally:
                c2.close()
            for smp, sc in zip(samples, scores):
                if sc is not None:
                    smp["score"], smp["v1"], smp["stage"] = sc, sc, rt["stage"]
        except Exception as e:                  # noqa: BLE001
            print(f"[rt] 分钟情绪重算失败(仅收盘点): {e}")
        etf_m = _minute_etf(etf) if etf else {}
        for smp in samples:
            if smp.get("etf") is None and smp["t"] in etf_m:
                smp["etf"] = etf_m[smp["t"]]
        samples[-1].update({k: v for k, v in final.items()
                            if v is not None and samples[-1].get(k) is None})
    else:
        samples = [final]
    with _lock:
        _state.update(date=target, samples=samples,
                      updated_at=now.strftime("%Y-%m-%d %H:%M:%S"))
        _persist_locked()
    _day_cache[target] = True
    print(f"[rt] 补采 {target}: {len(samples)} 点(分钟重建+终值)")
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


def _meta(display_date: str = None) -> dict:
    """对比基准: 昨收/昨日ETF合计 — 取**显示日之前**的最近交易日。
    D1-23修复: 显示10-29时原取到10-29收盘(=今收)致参考线错位。"""
    try:
        import yaml
        cfg = yaml.safe_load((CORE / "config.yaml").read_text(encoding="utf-8"))
        pri = cfg["index"]["primary"]
        con = sqlite3.connect(f"file:{CORE / 'data' / 'sentiment.db'}?mode=ro", uri=True)
        try:
            if display_date:
                row = con.execute(
                    "SELECT date,close FROM index_daily WHERE code=? AND date<? "
                    "ORDER BY date DESC LIMIT 1", (pri, display_date)).fetchone()
                etf = con.execute(
                    "SELECT date, SUM(amount) FROM etf_daily WHERE date<? "
                    "GROUP BY date ORDER BY date DESC LIMIT 1",
                    (display_date,)).fetchone()
                rows = [row] if row else []
            else:
                rows = con.execute(
                    "SELECT date,close FROM index_daily WHERE code=? "
                    "ORDER BY date DESC LIMIT 1", (pri,)).fetchall()
                etf = con.execute(
                    "SELECT date, SUM(amount) FROM etf_daily GROUP BY date "
                    "ORDER BY date DESC LIMIT 1").fetchone()
        finally:
            con.close()
        out = {"prev_date": rows[0][0] if rows else None,
               "prev_close": rows[0][1] if rows else None}
        if etf and etf[1]:                      # etf_daily.amount 单位=千元
            out["prev_etf_yi"] = round(etf[1] / 1e5, 0)
            out["prev_etf_date"] = etf[0]
        return out
    except Exception as e:                      # noqa: BLE001
        return {"error": str(e)}


def latest() -> dict:
    """页面数据: 当日样本(无则回退最近存档日·fallback标注) + live态 +
    对比基准 + 采集开关态"""
    with _lock:
        _hydrate_locked()                 # R5a: 重启后恢复当日已采数据
        state = {"date": _state["date"], "samples": list(_state["samples"]),
                 "updated_at": _state["updated_at"]}
    if not state["samples"]:              # R6b: 盘前/休市回退最近交易日
        recent = days()[0] if days() else None
        if recent:
            d = day(recent)
            if d and d["samples"]:
                state = {"date": d["date"], "samples": d["samples"],
                         "updated_at": d["updated_at"], "fallback": True}
    state["fallback"] = bool(state["samples"]) and state["date"] != time.strftime("%Y-%m-%d")
    state["live"] = bool(state["samples"]) and is_market_time() and not state["fallback"]
    state["meta"] = _meta(state["date"])   # D1-23: 按显示日取前一日基准
    state["meta"]["yday"] = _yday_curve(state["date"])   # D1-23b: 昨日累计额曲线
    state["meta"]["yday_etf"] = _yday_etf_curve(state["date"])   # D1-26: 昨日ETF
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
            "samples": json.loads(row[1]).get("samples", []),
            "meta": {**_meta(date_iso), "yday": _yday_curve(date_iso),
                     "yday_etf": _yday_etf_curve(date_iso)}}


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


def stats() -> dict:
    """R6c 数据管理卡: 实时库状态概览"""
    ensure_db()
    con = sqlite3.connect(str(RT_DB))
    try:
        rows = con.execute("SELECT date, n FROM rt_day ORDER BY date DESC").fetchall()
    finally:
        con.close()
    today = time.strftime("%Y-%m-%d")
    out = {
        "days": len(rows),
        "first": rows[-1][0] if rows else None,
        "latest": rows[0][0] if rows else None,
        "latest_n": rows[0][1] if rows else 0,
        "today_n": next((n for d, n in rows if d == today), 0),
        "size_kb": round(RT_DB.stat().st_size / 1024, 1) if RT_DB.exists() else 0,
        "enabled": _enabled_cached(),
        "poller_alive": _thread is not None and _thread.is_alive(),
        "recent": recent_days(5),             # D1-27: 近5交易日(chips日历化)
    }
    try:                                      # D1-27: realtime资产最近备份
        mf = (ROOT / "backups" / "manifest.json")
        if mf.exists():
            m = json.loads(mf.read_text(encoding="utf-8"))
            e = next((x for x in reversed(m.get("entries", []))
                      if x.get("asset") == "realtime"), None)
            if e:
                out["backup_at"] = (e.get("file", "").split("/")[0] or "")[:16]
    except Exception:                                      # noqa: BLE001
        pass
    return out


def _enabled_cached() -> bool:
    try:
        if _app is not None:
            with _app.app_context():
                from server import jobs as J
                return J.get_setting("rt_enabled", "1") == "1"
    except Exception:                       # noqa: BLE001
        pass
    return True


def backfill_now() -> str:
    """R6c 数据管理卡「立即补采」入口(同步执行, 秒级)"""
    return backfill_today() or "idle"


# ── D1-23b: 同花顺式量额副图数据 ──

_mkline_cache = {}          # code -> (day, bars)
_yday_anchor_cache = {}
_yday_curve_cache = {}


def _mkline(code: str = "sh000001", n: int = 640) -> list:
    """腾讯mkline m1分钟柱(数组: [dt,open,close,high,low,vol,...]); 按code+日缓存"""
    today = time.strftime("%Y-%m-%d")
    c = _mkline_cache.get(code)
    if c and c[0] == today and c[1]:
        return c[1]
    from probe_rt import _curl
    import json as _json
    _, body = _curl(f"https://ifzq.gtimg.cn/appstock/app/kline/mkline"
                    f"?param={code},m1,,{n}", timeout=10)
    try:
        bars = _json.loads(body)["data"][code]["m1"]
    except (ValueError, KeyError, TypeError):
        return []
    _mkline_cache[code] = (today, bars)
    return bars


def _prev_mkday(days: dict, display_date: str):
    """mkline日组中 < display_date 的最近日(YYYYMMDD)"""
    prev = None
    for d in sorted(days):
        if d < display_date.replace("-", ""):
            prev = d
    return prev


def _yday_etf_curve(display_date: str) -> dict | None:
    """D1-26: 昨日宽基ETF累计成交额曲线(亿, HH:MM) — 8只ETF昨日分钟量比
    × etf_daily昨日精确总额锚定(库内真值, 非估算额)"""
    if not display_date:
        return None
    import collections
    from concurrent.futures import ThreadPoolExecutor
    ETF_CODES = ("sz159901", "sz159915", "sz159919", "sz159949",
                 "sh510050", "sh510300", "sh510500", "sh512100")

    def _one_day_bars(code):
        bars = _mkline(code)
        if not bars:
            return None
        days = collections.defaultdict(list)
        for b in bars:
            days[b[0][:8]].append(b)
        prev = _prev_mkday(days, display_date)
        if not prev:
            return None
        return prev, days[prev]

    # P4③: 8只ETF mkline并行拉取(串行8s→并行~1s首载)
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_one_day_bars, ETF_CODES))
    volsum = collections.defaultdict(float)
    prev_ref = None
    for r in results:
        if r is None:
            continue
        prev, day_bars = r
        if prev_ref is None:
            prev_ref = prev
        elif prev != prev_ref:
            continue
        for b in day_bars:
            hhmm = f"{b[0][8:10]}:{b[0][10:12]}"
            volsum[hhmm] += float(b[5])
    if not volsum or prev_ref is None:
        return None
    prev_iso = f"{prev_ref[:4]}-{prev_ref[4:6]}-{prev_ref[6:8]}"
    try:
        con = sqlite3.connect(
            f"file:{(CORE / 'data' / 'sentiment.db').as_posix()}?mode=ro", uri=True)
        tot = con.execute("SELECT SUM(amount) FROM etf_daily WHERE date=?",
                          (prev_iso,)).fetchone()[0]
        con.close()
    except sqlite3.Error:
        tot = None
    if not tot:
        return None
    total_yi = round(tot / 1e5, 1)              # 千元→亿
    out, cum, all_cum = {}, 0.0, sum(volsum.values())
    for k in sorted(volsum):
        cum += volsum[k]
        out[k] = round(total_yi * cum / all_cum, 1)
    return {"date": prev_iso, "total_yi": total_yi, "points": out}


def _yday_amt_anchor(date_iso: str) -> float | None:
    """昨日上证官方成交额(亿, tushare index_daily千元口径); 按日缓存(终值数据)"""
    if date_iso in _yday_anchor_cache:
        return _yday_anchor_cache[date_iso]
    try:
        import tushare_api as T
        if not T.has_token():
            return None
        df = T.call("index_daily", "trade_date,amount", ts_code="000001.SH",
                    start_date=date_iso.replace("-", ""),
                    end_date=date_iso.replace("-", ""))
        v = float(df.iloc[0]["amount"]) / 1e5 if len(df) else None   # 千元→亿
    except Exception:                                       # noqa: BLE101
        v = None
    if v is not None:                 # 瞬时失败不缓存 — 下次请求重试
        _yday_anchor_cache[date_iso] = v
    return v


def _yday_curve(display_date: str) -> dict | None:
    """昨日累计成交额曲线(亿, 按HH:MM对齐今日时刻): mkline分钟量比形态 ×
    官方总额锚定(指数点数×量非金额, 不能直接乘 — 实测偏差2.3×)"""
    if not display_date:
        return None                              # 空态(latest无任何样本日)
    key = display_date
    if key in _yday_curve_cache:
        return _yday_curve_cache[key]
    bars = _mkline()
    if not bars:
        return None
    import collections
    days = collections.defaultdict(list)
    for b in bars:
        days[b[0][:8]].append(b)
    prev = _prev_mkday(days, display_date)
    if not prev:
        return None
    prev_iso = f"{prev[:4]}-{prev[4:6]}-{prev[6:8]}"
    anchor = _yday_amt_anchor(prev_iso)
    if not anchor:
        return None
    cum, total, pts = 0.0, 0.0, {}
    day_bars = days[prev]
    for b in day_bars:
        v = float(b[5])
        cum += v
        total += v
        hhmm = f"{b[0][8:10]}:{b[0][10:12]}"
        pts[hhmm] = cum
    out = {"date": prev_iso, "total_yi": round(anchor, 0),
           "points": {k: round(anchor * c / total, 1) for k, c in pts.items()}}
    _yday_curve_cache[key] = out
    return out


def recent_days(n: int = 5) -> list:
    """D1-27: 近N个交易日(ISO降序) — chips日历化(无档日也显示, 点击给提示)"""
    try:
        con = sqlite3.connect(
            f"file:{(CORE / 'data' / 'sentiment.db').as_posix()}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT cal_date FROM calendar WHERE is_open=1 AND cal_date<=? "
            "ORDER BY cal_date DESC LIMIT ?",
            (time.strftime("%Y%m%d"), int(n))).fetchall()
        con.close()
        return [_norm(r[0]) for r in rows]
    except Exception:                                      # noqa: BLE001
        return []


def _norm(ymd: str) -> str:
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}" if len(ymd) == 8 else ymd
