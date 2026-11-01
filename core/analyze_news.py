# -*- coding: utf-8 -*-
"""D1-28 N2: 本地AI新闻分析 — news_raw → AI分析 → news_local + macro_events成品"""
import json
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def ensure_tables(conn):
    # D1-28 N3r: macro_events也在此确保(独立CLI跑时无server/macro建表,
    # UPDATE/INSERT会炸"no such table")
    conn.execute("""CREATE TABLE IF NOT EXISTS macro_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT, level INTEGER, content TEXT, source TEXT,
      date TEXT, type TEXT, created_at TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS news_local(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      news_id INTEGER NOT NULL,
      status TEXT DEFAULT 'pending',       -- pending|done|fail
      found_title TEXT, found_time TEXT, found_content TEXT,
      level INTEGER, type TEXT, impact TEXT, direction TEXT,
      analysis_json TEXT,
      model TEXT, analyzed_at TEXT,
      UNIQUE(news_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS news_cloud(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      news_id INTEGER NOT NULL, local_id INTEGER,
      status TEXT DEFAULT 'pending',
      found_title TEXT, found_time TEXT, found_content TEXT,
      level INTEGER, type TEXT, impact TEXT, direction TEXT,
      analysis_json TEXT,
      model TEXT, analyzed_at TEXT,
      UNIQUE(news_id))""")
    conn.commit()


def _cfg(key, default=""):
    """读AppSetting(从macro.db或独立app.db — 这里直接走轻量文件)"""
    import os
    return os.environ.get(f"AI_{key.upper()}", default)


def get_ai_config() -> dict:
    """读AI配置 — 直读app.db的app_settings表(轻量, 不起Flask)"""
    defaults = {"ai_local_base_url": "http://localhost:11434/v1",
                "ai_local_model": "", "ai_local_api_key": "",
                "ai_local_enabled": "0"}
    try:
        import sqlite3 as _sq
        con = _sq.connect(f"file:{ROOT / 'data' / 'app.db'}?mode=ro", uri=True)
        for row in con.execute(
                "SELECT skey, value FROM app_settings WHERE skey LIKE 'ai_local_%'"):
            defaults[row[0]] = row[1]
        con.close()
    except Exception:                                     # noqa: BLE001
        pass
    return {"base_url": defaults["ai_local_base_url"],
            "model": defaults["ai_local_model"],
            "api_key": defaults["ai_local_api_key"],
            "enabled": defaults["ai_local_enabled"] == "1"}

def analyze_batch(batch_size: int = 5) -> dict:
    """取N条待分析 → AI调用 → news_local + macro_events成品upsert"""
    import ai_client
    cfg = get_ai_config()
    if not cfg["enabled"]:
        return {"analyzed": 0, "msg": "本地AI未启用"}
    db_path = BASE / "data" / "macro.db"
    con = sqlite3.connect(str(db_path))
    ensure_tables(con)
    # 取待分析(无news_local记录或status=fail)
    rows = con.execute("""
        SELECT n.id, n.title, COALESCE(n.content,''), n.pub_time
        FROM news_raw n
        LEFT JOIN news_local l ON l.news_id = n.id
        WHERE l.id IS NULL OR l.status = 'fail'
        ORDER BY n.pub_time DESC LIMIT ?""", (batch_size,)).fetchall()
    if not rows:
        con.close()
        return {"analyzed": 0, "msg": "无待分析"}
    analyzed, failed = 0, 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for news_id, title, content, pub_time in rows:
        try:
            result = ai_client.analyze_news(
                cfg["base_url"], cfg["model"], cfg["api_key"],
                title, content[:2000], timeout=90)
            analysis_json = json.dumps(result, ensure_ascii=False)
            con.execute("""INSERT INTO news_local
                (news_id, status, level, type, impact, direction,
                 analysis_json, model, analyzed_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(news_id) DO UPDATE SET
                status='done', level=excluded.level, type=excluded.type,
                impact=excluded.impact, direction=excluded.direction,
                analysis_json=excluded.analysis_json,
                model=excluded.model, analyzed_at=excluded.analyzed_at""",
                (news_id, "done", result["level"], result["type"],
                 result["impact"], result["direction"],
                 analysis_json, cfg["model"], now))
            # upsert到成品库macro_events(先查后插, 不依赖UNIQUE索引)
            date = pub_time[:10] if pub_time else now[:10]
            existing = con.execute(
                "SELECT id FROM macro_events WHERE title=? AND date=?",
                (title, date)).fetchone()
            if existing:
                con.execute(
                    "UPDATE macro_events SET level=?, type=?, source=?, updated_at=? WHERE id=?",
                    (result["level"], result["type"],
                     f"ai_local:{cfg['model']}", now, existing[0]))
            else:
                con.execute(
                    "INSERT INTO macro_events(title, level, content, source, date, type, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (title, result["level"], result["impact"],
                     f"ai_local:{cfg['model']}", date, result["type"], now, now))
            analyzed += 1
        except Exception as e:
            con.execute("""INSERT INTO news_local
                (news_id, status, analysis_json, model, analyzed_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(news_id) DO UPDATE SET
                status='fail', analysis_json=excluded.analysis_json,
                analyzed_at=excluded.analyzed_at""",
                (news_id, "fail", json.dumps({"error": str(e)[:200]}),
                 cfg["model"], now))
            failed += 1
    con.commit()
    # 统计
    pending = con.execute("""
        SELECT COUNT(*) FROM news_raw n
        LEFT JOIN news_local l ON l.news_id = n.id
        WHERE l.id IS NULL OR l.status = 'fail'""").fetchone()[0]
    done = con.execute("SELECT COUNT(*) FROM news_local WHERE status='done'").fetchone()[0]
    con.close()
    return {"analyzed": analyzed, "failed": failed, "pending": pending, "done": done}


def progress() -> dict:
    """分析进度"""
    db_path = BASE / "data" / "macro.db"
    if not db_path.exists():
        return {"pending": 0, "done": 0, "fail": 0, "cloud_done": 0,
                "cloud_pending": 0, "total_raw": 0}
    con = sqlite3.connect(str(db_path))
    try:
        ensure_tables(con)
        pending = con.execute("""
            SELECT COUNT(*) FROM news_raw n
            LEFT JOIN news_local l ON l.news_id = n.id
            WHERE l.id IS NULL OR l.status = 'fail'""").fetchone()[0]
        done = con.execute("SELECT COUNT(*) FROM news_local WHERE status='done'").fetchone()[0]
        fail = con.execute("SELECT COUNT(*) FROM news_local WHERE status='fail'").fetchone()[0]
        cloud_done = con.execute("SELECT COUNT(*) FROM news_cloud WHERE status='done'").fetchone()[0]
        total_raw = con.execute("SELECT COUNT(*) FROM news_raw").fetchone()[0]
        cloud_pending = con.execute("""
            SELECT COUNT(*) FROM news_local l
            LEFT JOIN news_cloud c ON c.news_id = l.news_id
            WHERE l.status='done' AND l.level >= 3
            AND (c.id IS NULL OR c.status = 'fail')""").fetchone()[0]
    except sqlite3.Error:
        pending = done = fail = cloud_done = total_raw = cloud_pending = 0
    finally:
        con.close()
    return {"pending": pending, "done": done, "fail": fail,
            "cloud_done": cloud_done, "cloud_pending": cloud_pending,
            "total_raw": total_raw}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="本地AI新闻分析(D1-28 N2)")
    ap.add_argument("--batch", type=int, default=5)
    ap.add_argument("--progress", action="store_true")
    a = ap.parse_args()
    if a.progress:
        print(json.dumps(progress(), ensure_ascii=False))
        return 0
    r = analyze_batch(a.batch)
    print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())


def get_cloud_config() -> dict:
    """读云端AI配置 — 直读app.db(同get_ai_config轻量模式)"""
    defaults = {"ai_cloud_base_url": "", "ai_cloud_model": "",
                "ai_cloud_api_key": "", "ai_cloud_enabled": "0"}
    try:
        import sqlite3 as _sq
        con = _sq.connect(f"file:{ROOT / 'data' / 'app.db'}?mode=ro", uri=True)
        for row in con.execute(
                "SELECT skey, value FROM app_settings WHERE skey LIKE 'ai_cloud_%'"):
            defaults[row[0]] = row[1]
        con.close()
    except Exception:                                     # noqa: BLE001
        pass
    return {"base_url": defaults["ai_cloud_base_url"],
            "model": defaults["ai_cloud_model"],
            "api_key": defaults["ai_cloud_api_key"],
            "enabled": defaults["ai_cloud_enabled"] == "1"}


def analyze_cloud_batch(batch_size: int = 5, cfg: dict = None,
                         db_path=None) -> dict:
    """N3: 云端二次分析 — 仅L3+本地分析过的, 控成本;
    输入=新闻+本地AI结果, 输出可修正等级; 成品库以云端为准
    cfg/db_path可选参数供测试注入(生产走默认模块级)"""
    import ai_client
    cfg = cfg or get_cloud_config()
    if not cfg["enabled"] or not cfg["base_url"] or not cfg["model"]:
        return {"analyzed": 0, "msg": "云端AI未配置或未启用"}
    db_path = db_path or BASE / "data" / "macro.db"
    con = sqlite3.connect(str(db_path))
    ensure_tables(con)
    # 取L3+且有本地done分析、无cloud记录的
    rows = con.execute("""
        SELECT l.news_id, l.id, n.title, COALESCE(n.content,''),
               n.pub_time, l.level, l.type, l.impact, l.direction
        FROM news_local l
        JOIN news_raw n ON n.id = l.news_id
        LEFT JOIN news_cloud c ON c.news_id = l.news_id
        WHERE l.status='done' AND l.level >= 3
        AND (c.id IS NULL OR c.status = 'fail')
        ORDER BY l.level DESC, n.pub_time DESC LIMIT ?""", (batch_size,)).fetchall()
    if not rows:
        con.close()
        return {"analyzed": 0, "msg": "无待云端分析(L3+)"}
    analyzed, failed = 0, 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for (news_id, local_id, title, content, pub_time,
         l_level, l_type, l_impact, l_direction) in rows:
        local = {"level": l_level, "type": l_type,
                 "impact": l_impact, "direction": l_direction}
        try:
            result = ai_client.analyze_news_cloud(
                cfg["base_url"], cfg["model"], cfg["api_key"],
                title, content[:2000], local, timeout=120)
            analysis_json = json.dumps(result, ensure_ascii=False)
            con.execute("""INSERT INTO news_cloud
                (news_id, local_id, status, level, type, impact, direction,
                 analysis_json, model, analyzed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(news_id) DO UPDATE SET
                status='done', level=excluded.level, type=excluded.type,
                impact=excluded.impact, direction=excluded.direction,
                analysis_json=excluded.analysis_json,
                model=excluded.model, analyzed_at=excluded.analyzed_at""",
                (news_id, local_id, "done", result["level"], result["type"],
                 result["impact"], result["direction"],
                 analysis_json, cfg["model"], now))
            # 成品库以云端结果覆盖(等级可能修正)
            date = pub_time[:10] if pub_time else now[:10]
            existing = con.execute(
                "SELECT id FROM macro_events WHERE title=? AND date=?",
                (title, date)).fetchone()
            if existing:
                con.execute(
                    "UPDATE macro_events SET level=?, type=?, source=?, updated_at=? WHERE id=?",
                    (result["level"], result["type"],
                     f"ai_cloud:{cfg['model']}", now, existing[0]))
            analyzed += 1
        except Exception as e:
            con.execute("""INSERT INTO news_cloud
                (news_id, local_id, status, analysis_json, model, analyzed_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(news_id) DO UPDATE SET
                status='fail', analysis_json=excluded.analysis_json,
                analyzed_at=excluded.analyzed_at""",
                (news_id, local_id, "fail",
                 json.dumps({"error": str(e)[:200]}), cfg["model"], now))
            failed += 1
    con.commit()
    pending = con.execute("""
        SELECT COUNT(*) FROM news_local l
        LEFT JOIN news_cloud c ON c.news_id = l.news_id
        WHERE l.status='done' AND l.level >= 3
        AND (c.id IS NULL OR c.status = 'fail')""").fetchone()[0]
    con.close()
    return {"analyzed": analyzed, "failed": failed, "pending": pending}


def level_counts() -> dict:
    """N4: 成品库按等级计数(AI/手工分列)"""
    db_path = BASE / "data" / "macro.db"
    if not db_path.exists():
        return {"ai": {}, "manual": {}}
    con = sqlite3.connect(str(db_path))
    try:
        ensure_tables(con)
        out = {"ai": {}, "manual": {}}
        for lv, src, n in con.execute(
                "SELECT level, source LIKE 'ai_%', COUNT(*) FROM macro_events "
                "GROUP BY level, source LIKE 'ai_%'"):
            key = "ai" if src else "manual"
            out[key][str(lv)] = n
    except sqlite3.Error:
        out = {"ai": {}, "manual": {}}
    finally:
        con.close()
    return out


def auto_analyze(local_batch: int = 20, cloud_batch: int = 10) -> dict:
    """N4: 自动分析(夜间链挂载) — 先本地全量, 后云端L3+"""
    r1 = analyze_batch(local_batch)
    r2 = analyze_cloud_batch(cloud_batch)
    return {"local": r1, "cloud": r2}
