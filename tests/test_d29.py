# -*- coding: utf-8 -*-
"""D1-29: 数据简报 — 生成器七模块/视图复用/双时调度/手动触发/页面接线"""
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


def test_briefing_html_generated(tmp_path, monkeypatch):
    """生成器: 真库只读组装 → HTML含七模块关键标记"""
    import briefing as BR
    out = tmp_path / "b.html"
    p = BR.generate_html(str(out))
    html = out.read_text(encoding="utf-8")
    for mk in ("数据简报", "情绪分", "全A PE", "全A PB", "巴菲特", "两融", "宽基ETF",
               "近24小时事件", "板块冷热"):
        assert mk in html, f"缺模块: {mk}"


def test_briefing_type_registered():
    """vtype=briefing 在视图体系可创建+调度"""
    from server.models import ViewDef
    assert hasattr(ViewDef, "type")
    src = (REPO / "server" / "jobs.py").read_text(encoding="utf-8")
    assert "task_briefing" in src
    assert "briefing_evening" in src and "briefing_morning" in src
    assert 'CronTrigger(hour=19, minute=0' in src
    assert 'CronTrigger(hour=7, minute=0' in src


def test_manual_trigger_api(server_app):
    c = server_app["client"]
    from server import jobs
    r = c.post("/admin/api/briefing/run")
    assert r.status_code == 200 and r.get_json()["ok"]
    item = jobs._q.get_nowait()
    assert item["kind"] == "generate" and "简报" in item["target"]


def test_page_gates():
    gen = (REPO / "server" / "templates" / "admin" / "generate.html").read_text(encoding="utf-8")
    assert 'value="briefing"' in gen and "btnBriefing" in gen
    dash = (REPO / "server" / "templates" / "admin" / "dashboard.html").read_text(encoding="utf-8")
    assert "数据简报" in dash and "19:00/07:00" in dash


def test_task_briefing_creates_rundata(server_app, monkeypatch):
    """task_briefing执行 → ViewDef自动创建 + ViewRun落库 + HTML产出"""
    from server import jobs
    from server.models import ViewDef, ViewRun
    import types

    def fake_run(cmd, append, cwd=None, env_extra=None):
        # 实际执行briefing生成(轻量)
        out_path = cmd[cmd.index("--out") + 1]
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("<html>test</html>", encoding="utf-8")
        return 0
    monkeypatch.setattr(jobs, "_run", fake_run)
    fn = jobs.task_briefing("manual")
    with server_app["app"].app_context():
        ViewDef.query.filter_by(type="briefing").delete()
        server_app["app"].extensions["sqlalchemy"].session.commit()
        rc = fn(lambda s: None)
        assert rc == 0
        vd = ViewDef.query.filter_by(type="briefing").first()
        assert vd is not None and vd.env == "prod" and vd.auto_publish == 1
        run = ViewRun.query.filter_by(vtype="briefing").order_by(ViewRun.id.desc()).first()
        assert run is not None and run.status == "done" and run.published == 1
        # 清理测试数据
        ViewRun.query.filter_by(vtype="briefing").delete()
        ViewDef.query.filter_by(type="briefing").delete()
        server_app["app"].extensions["sqlalchemy"].session.commit()
