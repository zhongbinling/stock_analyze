# -*- coding: utf-8 -*-
"""D1-18 M1/M2: 宏观舆情事件库 — 独立库 macro.db(与核心行情库解耦)

表 macro_events: id自增/title/level(1-5)/content/source/date/type/时间戳。
批量导入通道(外部工具/第三方数据)按 (title,date) 去重, 逐条校验。
等级口径: L5股灾级 L4重大级 L3重要级 L2关注级 L1普通级。
"""
import re
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "core"
MACRO_DB = CORE / "data" / "macro.db"

LEVELS = {
    5: ("L5·股灾级", "流动性危机/千股跌停/系统性风险"),
    4: ("L4·重大级", "政策组合拳(9·24类)/超预期重磅"),
    3: ("L3·重要级", "加息降息/重要经济数据/重大监管"),
    2: ("L2·关注级", "部委喊话/行业新闻/边际变化"),
    1: ("L1·普通级", "一般性事件/常规数据发布"),
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_lock = threading.Lock()

_SEED = [
    {"date": "2024-02-05", "title": "微盘股流动性危机·中证2000单日重挫(模拟样例)",
     "level": 5, "type": "市场", "source": "模拟样例",
     "content": "微盘股连续踩踏引发流动性危机, 千股跌停, 中证2000单日跌逾6%, 监管随后出手维稳。"},
    {"date": "2024-09-24", "title": "9·24政策组合拳: 降准降息+互换便利(模拟样例)",
     "level": 4, "type": "政策", "source": "模拟样例",
     "content": "一行一局一会联合发布重磅组合政策, 市场风险偏好急速扭转, 上证单日涨超4%。"},
    {"date": "2024-09-19", "title": "美联储降息50bp开启宽松周期(模拟样例)",
     "level": 3, "type": "货币", "source": "模拟样例",
     "content": "美联储超预期降息50个基点, 全球流动性宽松预期升温, 人民币资产吸引力提升。"},
    {"date": "2025-03-12", "title": "部委喊话支持低空经济产业(模拟样例)",
     "level": 2, "type": "产业", "source": "模拟样例",
     "content": "主管部门表态加快低空经济标准体系建设, 相关板块短线活跃。"},
    {"date": "2026-10-27", "title": "某行业季度运行数据发布(模拟样例)",
     "level": 1, "type": "产业", "source": "模拟样例",
     "content": "行业协会发布三季度运行数据, 同比小幅增长, 市场反应平淡。"},
]


def ensure_db(path=None) -> None:
    path = path or MACRO_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE IF NOT EXISTS macro_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL, level INTEGER NOT NULL,
      content TEXT, source TEXT, date TEXT NOT NULL, type TEXT,
      created_at TEXT, updated_at TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_macro_date ON macro_events(date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_macro_level ON macro_events(level)")
    if con.execute("SELECT COUNT(*) FROM macro_events").fetchone()[0] == 0:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        con.executemany(
            "INSERT INTO macro_events(title,level,content,source,date,type,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            [(e["title"], e["level"], e["content"], e["source"], e["date"],
              e["type"], now, now) for e in _SEED])
    con.commit()
    con.close()


def _conn(path=None):
    ensure_db(path)
    return sqlite3.connect(str(path or MACRO_DB))


def _row(r):
    return {"id": r[0], "title": r[1], "level": r[2], "content": r[3],
            "source": r[4], "date": r[5], "type": r[6],
            "updated_at": r[8]}


def list_events(level=None, d_from=None, d_to=None, type_=None, q=None,
                limit=500, path=None) -> list:
    """M2 检索: 等级/日期区间/类型/关键词(标题+内容), 日期倒序"""
    sql, args = "SELECT * FROM macro_events WHERE 1=1", []
    if level:
        sql += " AND level=?"
        args.append(int(level))
    if d_from:
        sql += " AND date>=?"
        args.append(d_from)
    if d_to:
        sql += " AND date<=?"
        args.append(d_to)
    if type_:
        sql += " AND type=?"
        args.append(type_)
    if q:
        sql += " AND (title LIKE ? OR content LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY date DESC, id DESC LIMIT ?"
    args.append(int(limit))
    con = _conn(path)
    rows = [_row(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return rows


def stats(path=None) -> dict:
    con = _conn(path)
    by = {str(l): n for l, n in con.execute(
        "SELECT level, COUNT(*) FROM macro_events GROUP BY level").fetchall()}
    total = con.execute("SELECT COUNT(*) FROM macro_events").fetchone()[0]
    con.close()
    return {"total": total, "by_level": by}


def _validate(e: dict, i: int) -> tuple | None:
    """单条校验: 返回 (错误信息) 或 None"""
    title = str(e.get("title") or "").strip()
    if not title or len(title) > 200:
        return f"[{i}] title 必填且≤200字"
    try:
        level = int(e.get("level"))
        if not 1 <= level <= 5:
            raise ValueError
    except (TypeError, ValueError):
        return f"[{i}] level 应为1-5"
    d = str(e.get("date") or "").strip()
    if not _DATE_RE.match(d):
        return f"[{i}] date 应为 YYYY-MM-DD"
    return None


def add_event(e: dict, path=None) -> int:
    """单条新增(校验失败抛 ValueError)"""
    err = _validate(e, 0)
    if err:
        raise ValueError(err)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con = _conn(path)
    cur = con.execute(
        "INSERT INTO macro_events(title,level,content,source,date,type,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (str(e["title"]).strip(), int(e["level"]),
         str(e.get("content") or "")[:5000], str(e.get("source") or "")[:100],
         e["date"].strip(), str(e.get("type") or "")[:32], now, now))
    con.commit()
    eid = cur.lastrowid
    con.close()
    return eid


def delete_event(eid: int, path=None) -> bool:
    con = _conn(path)
    n = con.execute("DELETE FROM macro_events WHERE id=?", (int(eid),)).rowcount
    con.commit()
    con.close()
    return n > 0


def import_events(events: list, path=None) -> dict:
    """批量导入通道(外部工具/第三方数据): 逐条校验, (title,date)去重,
    合法条目整批落库, 返回 {inserted, skipped, errors}"""
    out = {"inserted": 0, "skipped": 0, "errors": []}
    if not isinstance(events, list):
        out["errors"].append("events 应为数组")
        return out
    clean, seen = [], set()
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            out["errors"].append(f"[{i}] 应为对象")
            continue
        err = _validate(e, i)
        if err:
            out["errors"].append(err)
            continue
        key = (str(e["title"]).strip(), e["date"].strip())
        if key in seen:
            out["skipped"] += 1
            continue
        seen.add(key)
        clean.append(e)
    if not clean:
        return out
    con = _conn(path)
    have = {(r[0], r[1]) for r in con.execute(
        "SELECT title, date FROM macro_events").fetchall()}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        for e in clean:
            key = (str(e["title"]).strip(), e["date"].strip())
            if key in have:
                out["skipped"] += 1
                continue
            con.execute(
                "INSERT INTO macro_events(title,level,content,source,date,"
                "type,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (key[0], int(e["level"]), str(e.get("content") or "")[:5000],
                 str(e.get("source") or "")[:100], key[1],
                 str(e.get("type") or "")[:32], now, now))
            have.add(key)
            out["inserted"] += 1
        con.commit()
    con.close()
    return out
