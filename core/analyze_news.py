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
    """读AI配置(优先环境变量, 后备AppSetting via server)"""
    try:
        from server import jobs as J
        with_j = None
        # server在sys.path时读AppSetting
        import server
        from flask import Flask
        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{ROOT / 'data' / 'app.db'}"
        from server.models import db
        db.init_app(app)
        with app.app_context():
            base = J.get_setting("ai_local_base_url", "http://localhost:11434/v1")
            model = J.get_setting("ai_local_model", "qwen3.8:27b")
            key = J.get_setting("ai_local_api_key", "")
            enabled = J.get_setting("ai_local_enabled", "0") == "1"
    except Exception:
        base, model, key, enabled = "http://localhost:11434/v1", "qwen3.8:27b", "", False
    return {"base_url": base, "model": model, "api_key": key, "enabled": enabled}


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
        return {"pending": 0, "done": 0, "fail": 0, "cloud_done": 0, "total_raw": 0}
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
    except sqlite3.Error:
        pending = done = fail = cloud_done = total_raw = 0
    finally:
        con.close()
    return {"pending": pending, "done": done, "fail": fail,
            "cloud_done": cloud_done, "total_raw": total_raw}


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
