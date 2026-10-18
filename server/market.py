# -*- coding: utf-8 -*-
"""行情库(core/data/sentiment.db)只读统计: 数据集状态 / 最新日期 / 缺失天数

只读连接(URI mode=ro), 不import core模块(避免拉起pandas); 配置读 core/config.yaml。
"""
import sqlite3
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "core"
SENT_DB = CORE / "data" / "sentiment.db"

_cfg_cache = None


def _core_cfg() -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        with open(CORE / "config.yaml", encoding="utf-8") as f:
            _cfg_cache = yaml.safe_load(f)
    return _cfg_cache


def _conn():
    if not SENT_DB.exists():
        return None
    con = sqlite3.connect(f"file:{SENT_DB.as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=3000")
    return con


def latest_index_date():
    cfg = _core_cfg()
    con = _conn()
    if con is None:
        return None
    try:
        return con.execute("SELECT MAX(date) FROM index_daily WHERE code=?",
                           (cfg["index"]["primary"],)).fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        con.close()


def is_trading_day_today() -> bool:
    """交易日判定: 优先 tushare 交易日历(前验); 无日历时回退'行情最新日期==今天'(后验)"""
    today = time.strftime("%Y%m%d")
    try:
        con = _conn()
        if con is None:
            return latest_index_date() == time.strftime("%Y-%m-%d")
        row = con.execute("SELECT is_open FROM calendar WHERE cal_date=?",
                          (today,)).fetchone()
        con.close()
        if row is not None:
            return bool(row[0])
    except sqlite3.Error:
        pass
    return latest_index_date() == time.strftime("%Y-%m-%d")


def _stale_days(last: str) -> int:
    """最新数据距今天数(粗略, 用于健康度)"""
    if not last:
        return 99
    try:
        from datetime import date
        y, m, d = map(int, last.split("-"))
        return (date.today() - date(y, m, d)).days
    except ValueError:
        return 99


def pipeline_stats() -> dict:
    """更新链各步关键计数(D2-1 分步日志): 轻量只读, 返回 {key: {rows,last}}"""
    con = _conn()
    if con is None:
        return {}
    try:
        out = {}

        def q(key, sql):
            try:
                out[key] = con.execute(sql).fetchone()
            except sqlite3.Error:
                out[key] = (0, None)
        q("index", "SELECT COUNT(*), MAX(date) FROM index_daily")
        q("mood", "SELECT COUNT(*), MAX(date) FROM mood_raw")
        q("sentiment", "SELECT COUNT(*), MAX(date) FROM sentiment")
        q("sector_daily", "SELECT COUNT(*), MAX(date) FROM sector_daily")
        q("bk", "SELECT COUNT(*), MAX(date) FROM sector_scores WHERE code LIKE 'BK%'")
        q("sw1", "SELECT COUNT(*), MAX(date) FROM sector_scores WHERE code LIKE 'SW1_%'")
        q("sw2", "SELECT COUNT(*), MAX(date) FROM sector_scores WHERE code LIKE 'SW2_%'")
        return {k: {"rows": r[0] or 0, "last": r[1]} for k, r in out.items()}
    finally:
        con.close()


def score_datasets() -> list:
    """评分数据状态(D2-2): 每类评分数据的区间/行数/最新/健康

    健康基准=行情最新日期(index_daily 主力指数MAX): 评分未跟上行情即warn。
    """
    con = _conn()
    if con is None:
        return [{"code": "score:db", "name": "评分数据", "source": "--", "rows": 0,
                 "first": None, "last": None, "health": "bad", "note": "行情库不存在"}]
    try:
        base = latest_index_date()          # 行情最新日期(健康基准)
        out = []

        def _lag(last):
            """落后行情天数; 行情基准缺失时回落自然日陈旧度口径(>4天视为落后)"""
            if not last:
                return 0
            if not base:
                return 0 if _stale_days(last) <= 4 else _stale_days(last)
            try:
                from datetime import date
                b = date(*map(int, base.split("-")))
                l = date(*map(int, last.split("-")))
                return (b - l).days
            except ValueError:
                return 0

        rows, first, last = con.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM sentiment").fetchone()
        lag = _lag(last)
        out.append({"code": "score:sentiment", "name": "大盘情绪分", "source": "score.py",
                    "rows": rows or 0, "first": first, "last": last, "codes": None,
                    "days": rows or 0,
                    "health": ("bad" if not last else "warn" if lag > 0 else "ok"),
                    "note": ("无数据" if not last else
                             f"{'与行情同步' if lag == 0 else f'落后行情 {lag} 天'}·重算评分后刷新")})

        for code, pat, name in (("score:bk", "BK%", "板块温度·行业板块"),
                                ("score:sw1", "SW1_%", "板块温度·申万一级聚合"),
                                ("score:sw2", "SW2_%", "板块温度·申万二级聚合")):
            rows, codes, days, first, last = con.execute(
                "SELECT COUNT(*), COUNT(DISTINCT code), COUNT(DISTINCT date), "
                "MIN(date), MAX(date) FROM sector_scores WHERE code LIKE ?",
                (pat,)).fetchone()
            lag = _lag(last)
            note = ("" if not last else
                    ("与行情同步" if lag == 0 else f"落后行情 {lag} 天"))
            if code == "score:bk" and last:
                comp_last = con.execute(
                    "SELECT MAX(date) FROM sector_scores WHERE components IS NOT NULL "
                    "AND code LIKE 'BK%'").fetchone()[0]
                note += f"·分量明细覆盖至 {comp_last or '--'}(保留260日)"
            out.append({"code": code, "name": name, "source": "sector.py calc",
                        "rows": rows or 0, "first": first, "last": last,
                        "codes": codes or 0, "days": days or 0,
                        "health": ("bad" if not last else "warn" if lag > 0 else "ok"),
                        "note": note or "无数据"})
        return out
    except sqlite3.Error as e:
        return [{"code": "score:db", "name": "评分数据", "source": "--", "rows": 0,
                 "first": None, "last": None, "health": "bad", "note": f"读取失败: {e}"}]
    finally:
        con.close()


# ---- D1-1: 数据资产注册表 (代码级清单; 第四阶段运维动态注册时再迁 app.db) ----
# (code, 名称, 表, 日期口径where, 来源, 陈旧阈值天, 基础说明)
_ASSET_DATE_TABLES = [
    ("index", "指数日线(上证+深综)", "index_daily", None, "东财/腾讯", 4,
     "三源回退·上证为评分主力指数"),
    ("pool", "涨停复盘池(同花顺口径)", "mood_raw", "zt_count IS NOT NULL", "同花顺", 4,
     "源历史约一年滚动·2012-2019为估算回补"),
    ("breadth", "当日涨跌家数", "mood_raw", "up_count IS NOT NULL", "乐咕(可选)", 4,
     "仅最新一天可得, 不参与评分历史分量"),
    ("sector_daily", "板块日线", "sector_daily", None, "东财push2his", 4,
     "涨跌家数采集日起积累"),
    ("sector_pool", "板块涨停聚合(THS行业)", "sector_pool_stat", None, "同花顺", 4,
     "tushare行业聚合·2020起(东财池每日增量补充)"),
    ("sector_fflow", "板块资金流", "sector_fundflow", None, "东财fflow", 7,
     "约最近120日滚动"),
    ("margin", "融资融券(tushare)", "margin_daily", None, "tushare", 7, ""),
]
_HEALTH_RANK = {"ok": 0, "warn": 1, "bad": 2}


def _norm_date(s) -> str:
    """calendar.cal_date(YYYYMMDD) → YYYY-MM-DD"""
    s = str(s)
    if len(s) == 8 and "-" not in s:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _expected_days(con, first, last, primary) -> list:
    """区间内交易日(完成度基准)。

    日历**完整覆盖**区间时用日历; 否则(部分覆盖/表缺失/旧行少)回退主力指数日期
    量全区间——避免静默只量日历覆盖的那一小段。回退时指数自身恒为100%(自为基准,
    其缺口会体现在其他数据集的完成度上)。
    """
    if not first or not last:
        return []
    lo, hi = first.replace("-", ""), last.replace("-", "")
    try:
        cov = con.execute(
            "SELECT MIN(cal_date), MAX(cal_date) FROM calendar WHERE is_open=1").fetchone()
        if cov and cov[0] and cov[0] <= lo and cov[1] >= hi:
            rows = con.execute(
                "SELECT cal_date FROM calendar WHERE is_open=1 AND cal_date>=? AND cal_date<=?",
                (lo, hi)).fetchall()
            if rows:
                return [_norm_date(r[0]) for r in rows]
    except sqlite3.Error:
        pass                        # 日历表缺失(旧库) → 回退指数基准
    return [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM index_daily WHERE code=? AND date>=? AND date<=? ORDER BY date",
        (primary, first, last))]


def datasets() -> list:
    """数据集状态列表(数据管理页) — D1-1 注册表驱动: 区间/行数/完成度%/缺口明细"""
    cfg = _core_cfg()
    con = _conn()
    if con is None:
        return [{"code": "db", "name": "行情库", "source": "--", "rows": 0,
                 "first": None, "last": None, "health": "bad",
                 "note": "core/data/sentiment.db 不存在, 请先执行数据更新"}]
    out = []
    try:
        primary = cfg["index"]["primary"]
        for code, name, table, where, source, stale_max, base_note in _ASSET_DATE_TABLES:
            try:
                cond = f" WHERE {where}" if where else ""
                rows, first, last = con.execute(
                    f"SELECT COUNT(*), MIN(date), MAX(date) FROM {table}{cond}").fetchone()
                have = {r[0] for r in con.execute(
                    f"SELECT DISTINCT date FROM {table}{cond}")}
                expected = _expected_days(con, first, last, primary)
                missing = [d for d in expected if d not in have]
                completeness = (round((len(expected) - len(missing)) / len(expected) * 100)
                                if expected else None)
                codes = None
                if table in ("sector_daily", "sector_pool_stat", "sector_fundflow"):
                    codes = con.execute(
                        f"SELECT COUNT(DISTINCT code) FROM {table}{cond}").fetchone()[0]
                # 健康 = 陈旧度 与 完成度 取差者; 有缺失交易日(<100)即warn, <80 bad
                h = "ok" if _stale_days(last) <= stale_max else "warn"
                if completeness is not None:
                    if completeness < 80:
                        h = "bad"
                    elif missing:
                        h = "warn"
                note = base_note
                if missing:
                    note = (f"缺 {len(missing)} 天(近期: {','.join(missing[-3:])})"
                            + (f"·{base_note}" if base_note else ""))
                extra = ""
                if table == "margin_daily":
                    rzrq = con.execute(
                        "SELECT rzrqye FROM margin_daily ORDER BY date DESC LIMIT 1").fetchone()
                    extra = f"·最新两融余额 {(rzrq[0] or 0) / 1e12:.2f}万亿" if rzrq and rzrq[0] else ""
                out.append({"code": code, "name": name, "source": source,
                            "rows": rows or 0, "codes": codes,
                            "first": first, "last": last,
                            "completeness": completeness,
                            "missing_n": len(missing), "missing_recent": missing[-10:],
                            "health": h, "note": (note + extra) or "--"})
            except sqlite3.Error:
                continue                    # 该表未建(旧库)时跳过

        # ---- 非日期型资产 ----
        try:
            n_ind, n_con = con.execute(
                "SELECT SUM(kind='industry'), SUM(kind='concept') FROM sectors").fetchone()
            out.append({"code": "sectors", "name": "板块清单(东财BK)", "source": "东财clist",
                        "rows": (n_ind or 0) + (n_con or 0), "codes": None,
                        "first": None, "last": con.execute(
                            "SELECT MAX(updated_at) FROM sectors").fetchone()[0],
                        "completeness": None, "missing_n": 0, "missing_recent": [],
                        "health": "ok" if (n_ind or 0) >= 100 else "warn",
                        "note": f"行业{n_ind or 0}(启用) + 概念{n_con or 0}(停用)"})
        except sqlite3.Error:
            pass
        try:
            ts_token = CORE / "data" / "tushare_token.txt"
            has_token = ts_token.exists() and ts_token.read_text(encoding="utf-8").strip()
            rows, first, last = con.execute(
                "SELECT COUNT(*), MIN(cal_date), MAX(cal_date) FROM calendar").fetchone()
            out.append({"code": "calendar", "name": "交易日历(tushare)", "source": "tushare",
                        "rows": rows or 0, "codes": None,
                        "first": _norm_date(first) if first else None,
                        "last": _norm_date(last) if last else None,
                        "completeness": None, "missing_n": 0, "missing_recent": [],
                        "health": "ok" if rows else "warn",
                        "note": f"完成度基准·token{'已配置' if has_token else '未配置'}"})
        except sqlite3.Error:
            pass
        # 申万映射 L1/L2/L3(表存在才显示; L3 构建归专项)
        _lvl_cn = {"sw1": "一级", "sw2": "二级", "sw3": "三级"}
        for lvl, label in (("sw1", "申万一级映射"), ("sw2", "申万二级映射"), ("sw3", "申万三级映射")):
            t = f"{lvl}_map"
            exists = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
            if not exists:
                out.append({"code": lvl, "name": label, "source": "tushare投票",
                            "rows": 0, "codes": None, "first": None, "last": None,
                            "completeness": None, "missing_n": 0, "missing_recent": [],
                            "health": "warn", "note": "未构建(见L3专项计划)"})
                continue
            try:
                rows = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                sws = con.execute(f"SELECT COUNT(DISTINCT {lvl}_name) FROM {t}").fetchone()[0]
                bks = con.execute(f"SELECT COUNT(DISTINCT bk_code) FROM {t}").fetchone()[0]
                out.append({"code": lvl, "name": label, "source": "tushare投票",
                            "rows": rows, "codes": bks, "first": None, "last": None,
                            "completeness": None, "missing_n": 0, "missing_recent": [],
                            "health": "ok" if rows else "warn",
                            "note": f"{sws} 个{_lvl_cn[lvl]} · 覆盖 {bks} 个东财BK"})
            except sqlite3.Error:
                pass
        return out
    except sqlite3.Error as e:
        return [{"code": "db", "name": "行情库", "source": "--", "rows": 0,
                 "first": None, "last": None, "health": "bad", "note": f"读取失败: {e}"}]
    finally:
        con.close()
