# -*- coding: utf-8 -*-
"""修复两件: ①数据生成页briefing误产情绪图(task_generate分派) ②事件重复(dedup_events)"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import analyze_news as AN  # noqa: E402


# ── ① task_generate: briefing类型走briefing.py ──

def test_generate_briefing_dispatch(server_app, monkeypatch):
    """ViewDef type=briefing经task_generate → cmd为briefing.py(原落入else产情绪图)"""
    from server import jobs
    from server.models import ViewDef
    cmds = []

    def fake_run(cmd, append, cwd=None, env_extra=None):
        cmds.append(cmd)
        out = cmd[cmd.index("--out") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text("<html>b</html>", encoding="utf-8")
        return 0
    monkeypatch.setattr(jobs, "_run", fake_run)
    with server_app["app"].app_context():
        ViewDef.query.filter_by(type="briefing").delete()
        server_app["app"].extensions["sqlalchemy"].session.commit()
        vd = ViewDef(name="简报修复测试", type="briefing", env="prod",
                     auto_publish=1, enabled=1)
        db = server_app["app"].extensions["sqlalchemy"]
        db.session.add(vd)
        db.session.commit()
        vid = vd.id
        rc = jobs.task_generate(vid)(lambda s: None)
        assert rc == 0 and len(cmds) == 1
        assert "briefing.py" in cmds[0][2] and "viewer.py" not in str(cmds[0])
        from server.models import ViewRun
        run = ViewRun.query.filter_by(vtype="briefing").order_by(ViewRun.id.desc()).first()
        assert run is not None and run.status == "done"
        # 清理
        ViewRun.query.filter_by(vtype="briefing").delete()
        ViewDef.query.filter_by(id=vid).delete()
        db.session.commit()


def test_generate_sentiment_dispatch_unchanged(server_app, monkeypatch):
    """回归: 普通sentiment类型仍走viewer.py(修复不破坏原分派)"""
    from server import jobs
    from server.models import ViewDef
    cmds = []

    def fake_run(cmd, append, cwd=None, env_extra=None):
        cmds.append(cmd)
        out = cmd[cmd.index("--out") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text("<html>v</html>", encoding="utf-8")
        return 0
    monkeypatch.setattr(jobs, "_run", fake_run)
    with server_app["app"].app_context():
        db = server_app["app"].extensions["sqlalchemy"]
        vd = ViewDef(name="情绪回归测试", type="sentiment", env="test", enabled=1)
        db.session.add(vd)
        db.session.commit()
        vid = vd.id
        rc = jobs.task_generate(vid)(lambda s: None)
        assert rc == 0 and "viewer.py" in cmds[0][2]
        ViewDef.query.filter_by(id=vid).delete()
        db.session.commit()


# ── ② dedup_events: (title,date)去重 ──

def test_dedup_events_keeps_newest(tmp_path, monkeypatch):
    """同(title,date)组保留最新id(云端覆盖态); 单条不动; 幂等"""
    db = tmp_path / "macro.db"
    import sqlite3
    con = sqlite3.connect(str(db))
    AN.ensure_tables(con)
    for i in range(2):                       # 欧盟事件双版本(清洗收敛暴露)
        con.execute("INSERT INTO macro_events(title,level,content,date) VALUES(?,?,?,?)",
                    ("欧盟委员会公布2027年工作计划", 1, f"v{i}", "2026-11-05"))
    con.execute("INSERT INTO macro_events(title,level,date) VALUES(?,?,?)",
                ("单一事件", 2, "2026-11-05"))
    con.commit()
    con.close()
    assert AN.dedup_events(db) == 1
    con = sqlite3.connect(str(db))
    rows = con.execute("SELECT title, content FROM macro_events ORDER BY id").fetchall()
    con.close()
    assert rows == [("欧盟委员会公布2027年工作计划", "v1"), ("单一事件", None)]  # 保留最新id=2
    assert AN.dedup_events(db) == 0          # 幂等


def test_auto_analyze_wires_dedup(monkeypatch):
    """夜间链auto_analyze: 内容对齐→去重→分析"""
    calls = []
    monkeypatch.setattr(AN, "sync_event_content", lambda *a, **kw: calls.append("sync"))
    monkeypatch.setattr(AN, "dedup_events", lambda *a, **kw: calls.append("dedup"))
    monkeypatch.setattr(AN, "analyze_batch", lambda *a, **kw: {})
    monkeypatch.setattr(AN, "analyze_cloud_batch", lambda *a, **kw: {})
    AN.auto_analyze()
    assert calls == ["sync", "dedup"]
