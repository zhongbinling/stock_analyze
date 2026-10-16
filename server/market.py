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


def datasets() -> list:
    """数据集状态列表(数据管理页)"""
    cfg = _core_cfg()
    today = time.strftime("%Y-%m-%d")
    con = _conn()
    if con is None:
        return [{"code": "db", "name": "行情库", "source": "--", "rows": 0,
                 "first": None, "last": None, "health": "bad",
                 "note": "core/data/sentiment.db 不存在, 请先执行数据更新"}]
    out = []
    try:
        primary = cfg["index"]["primary"]
        for code, label in ((primary, "指数日线·上证"), (cfg["index"]["secondary"], "指数日线·深证综指")):
            rows, first, last = con.execute(
                "SELECT COUNT(*), MIN(date), MAX(date) FROM index_daily WHERE code=?",
                (code,)).fetchone()
            stale = _stale_days(last)
            out.append({"code": f"index:{code}", "name": label, "source": "东财/腾讯",
                        "rows": rows, "first": first, "last": last,
                        "health": "ok" if stale <= 4 else "warn",
                        "note": f"距今日 {stale} 天"})

        ps = str(cfg.get("pool_start", ""))
        pool_start = f"{ps[:4]}-{ps[4:6]}-{ps[6:8]}" if ps else "2000-01-01"
        have = {r[0] for r in con.execute("SELECT date FROM mood_raw WHERE zt_count IS NOT NULL")}
        idx_dates = [r[0] for r in con.execute(
            "SELECT date FROM index_daily WHERE code=? AND date>=? ORDER BY date",
            (primary, pool_start))]
        missing = [d for d in idx_dates if d not in have and d <= today]
        rows, last = con.execute(
            "SELECT COUNT(*), MAX(date) FROM mood_raw WHERE zt_count IS NOT NULL").fetchone()
        out.append({"code": "pool", "name": "涨停复盘池(同花顺口径)", "source": "同花顺",
                    "rows": rows, "first": pool_start, "last": last,
                    "health": "warn" if len(missing) > 3 else "ok",
                    "note": f"缺失 {len(missing)} 天(源历史自 {pool_start} 起, 仅约一年滚动)"
                            + (f"; 近期缺: {','.join(missing[-3:])}" if missing else "")})

        rows, last = con.execute("SELECT COUNT(*), MAX(date) FROM sentiment").fetchone()
        out.append({"code": "sentiment", "name": "情绪分(评分结果)", "source": "本系统计算",
                    "rows": rows or 0, "first": None,
                    "last": last, "health": "ok" if (last and _stale_days(last) <= 4) else "warn",
                    "note": "重算评分后刷新"})

        rows, last = con.execute(
            "SELECT COUNT(*), MAX(date) FROM mood_raw WHERE up_count IS NOT NULL").fetchone()
        out.append({"code": "breadth", "name": "当日涨跌家数", "source": "乐咕(可选)",
                    "rows": rows or 0, "first": None, "last": last,
                    "health": "ok",
                    "note": "仅最新一天可得, 不参与评分历史分量"})

        # ---- S5: 板块数据底座 ----
        try:
            n_ind, n_con = con.execute(
                "SELECT SUM(kind='industry'), SUM(kind='concept') FROM sectors").fetchone()
            out.append({"code": "sectors", "name": "板块清单(东财BK)", "source": "东财clist",
                        "rows": (n_ind or 0) + (n_con or 0), "first": None,
                        "last": con.execute("SELECT MAX(updated_at) FROM sectors").fetchone()[0],
                        "health": "ok" if (n_ind or 0) >= 100 else "warn",
                        "note": f"行业{n_ind or 0}(启用) + 概念{n_con or 0}(停用)"})
            rows, codes, first, last = con.execute(
                "SELECT COUNT(*), COUNT(DISTINCT code), MIN(date), MAX(date) FROM sector_daily").fetchone()
            out.append({"code": "sector_daily", "name": "板块日线", "source": "东财push2his",
                        "rows": rows or 0, "first": first, "last": last,
                        "health": "ok" if _stale_days(last) <= 4 else "warn",
                        "note": f"{codes} 板块; 涨跌家数采集日起积累"})
            rows, last = con.execute(
                "SELECT COUNT(*), MAX(date) FROM sector_pool_stat").fetchone()
            out.append({"code": "sector_pool", "name": "板块涨停聚合(THS行业)", "source": "同花顺",
                        "rows": rows or 0, "first": None, "last": last,
                        "health": "ok" if _stale_days(last) <= 4 else "warn",
                        "note": "东财池口径·历史约1个月起逐日积累"})
            rows, codes, last = con.execute(
                "SELECT COUNT(*), COUNT(DISTINCT code), MAX(date) FROM sector_fundflow").fetchone()
            out.append({"code": "sector_fflow", "name": "板块资金流", "source": "东财fflow",
                        "rows": rows or 0, "first": None, "last": last,
                        "health": "ok" if _stale_days(last) <= 7 else "warn",
                        "note": f"{codes or 0} 板块·约最近120日·周频刷新"})
        except sqlite3.Error:
            pass                    # 板块表未建(旧库)时跳过该组

        # ---- tushare 数据源状态 ----
        try:
            ts_token = (CORE / "data" / "tushare_token.txt")
            rows, first, last = con.execute(
                "SELECT COUNT(*), MIN(cal_date), MAX(cal_date) FROM calendar").fetchone()
            out.append({"code": "tushare_cal", "name": "交易日历(tushare)", "source": "tushare",
                        "rows": rows or 0, "first": first, "last": last,
                        "health": "ok" if rows else "warn",
                        "note": f"token{'已配置' if ts_token.exists() and ts_token.read_text(encoding='utf-8').strip() else '未配置'}"
                                "·limit_list_d等需更高积分"})
        except sqlite3.Error:
            pass
        return out
    except sqlite3.Error as e:
        return [{"code": "db", "name": "行情库", "source": "--", "rows": 0,
                 "first": None, "last": None, "health": "bad", "note": f"读取失败: {e}"}]
    finally:
        con.close()
