# -*- coding: utf-8 -*-
"""B1 服务端备份测试: task_backup 落库 / API(入队·列表·设置校验·恢复守卫) / 周备cron解析"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(REPO))

import backup as BK  # noqa: E402


@pytest.fixture()
def app_env(server_app, tmp_path, monkeypatch):
    """server_app(共享) + 备份资产指向 tmp(仅1个小sqlite, 其余缺失)"""
    sent = tmp_path / "sentiment.db"
    conn = sqlite3.connect(str(sent))
    conn.execute("CREATE TABLE sentiment(date TEXT PRIMARY KEY, score REAL)")
    conn.execute("INSERT INTO sentiment VALUES('2026-09-30', -30.0)")
    conn.commit()
    conn.close()
    bdir = tmp_path / "backups"
    monkeypatch.setattr(BK, "ASSETS", {"sentiment": ("sqlite", sent)})
    monkeypatch.setattr(BK, "BACKUP_DIR", bdir)
    monkeypatch.setattr(BK, "MANIFEST", bdir / "manifest.json")
    monkeypatch.setattr(BK, "LOG_FILE", bdir / "backup.log")
    return {**server_app, "sent": sent, "bdir": bdir}


def _runs(app):
    from server.models import BackupRun
    return BackupRun.query.order_by(BackupRun.id.desc()).all()


def test_task_backup_records_run(app_env):
    from server import jobs
    fn = jobs.task_backup("daily", "schedule")
    lines = []
    with app_env["app"].app_context():
        rc = fn(lines.append)
        assert rc == 0
        runs = _runs(app_env["app"])
        assert len(runs) == 1
        r = runs[0]
        assert r.btype == "daily" and r.trigger == "schedule" and r.status == "done"
        assert r.files_n == 1 and r.total_mb >= 0 and r.duration_s is not None
        assert json.loads(r.files_json)[0]["asset"] == "sentiment"
        assert any("备份结束" in ln for ln in lines)


def test_api_backup_run_enqueues(app_env):
    r = app_env["client"].post("/admin/api/backup/run",
                               json={"type": "weekly"})
    assert r.status_code == 200 and r.get_json()["ok"]
    from server import jobs
    assert jobs.queue_size() == 1
    item = jobs._q.get_nowait()          # 取出(不执行), 避免污染其他测试
    assert item["kind"] == "backup" and item["target"] == "weekly"


def test_api_backup_run_bad_type(app_env):
    r = app_env["client"].post("/admin/api/backup/run", json={"type": "hack"})
    assert r.status_code == 400


def test_api_backups_lists_and_summary(app_env):
    from server import jobs
    fn = jobs.task_backup("daily", "schedule")
    with app_env["app"].app_context():
        fn(lambda s: None)
    r = app_env["client"].get("/admin/api/backups")
    j = r.get_json()
    assert r.status_code == 200
    assert len(j["runs"]) == 1 and j["runs"][0]["btype"] == "daily"
    assert j["runs"][0]["files"][0]["asset"] == "sentiment"
    assert j["last_ok"]["daily"] != ""
    assert j["backed_today"] is True and j["files_n"] == 1


def test_api_backup_settings_validation_and_save(app_env):
    c = app_env["client"]
    bad = c.post("/admin/api/backup/settings", json={
        "backup_enabled": "1", "backup_daily_cron": "25:99",
        "backup_weekly_cron": "xyz 18:45",
        "backup_daily_n": "14", "backup_weekly_n": "8", "backup_manual_n": "5"})
    assert bad.status_code == 400 and "HH:MM" in bad.get_json()["msg"]
    ok = c.post("/admin/api/backup/settings", json={
        "backup_enabled": "0", "backup_daily_cron": "19:00",
        "backup_weekly_cron": "周五 19:10",
        "backup_daily_n": "10", "backup_weekly_n": "6", "backup_manual_n": "3"})
    assert ok.status_code == 200 and ok.get_json()["ok"]
    from server import jobs
    with app_env["app"].app_context():
        assert jobs.get_setting("backup_daily_n") == "10"
        assert jobs.get_setting("backup_weekly_cron") == "周五 19:10"


def test_api_backup_restore_guards(app_env):
    from server import jobs
    with app_env["app"].app_context():
        jobs.task_backup("daily")(lambda s: None)
    c = app_env["client"]
    # app 资产: 构造一条 app 备份
    BK.snapshot("sentiment", "daily")            # 产出 sentiment 备份
    m = BK._load_manifest()
    sent_file = next(e["file"] for e in m["entries"] if e["asset"] == "sentiment")
    app_file = sent_file.replace("sentiment", "app_")
    m["entries"].append({**m["entries"][0], "file": app_file, "asset": "app"})
    BK._save_manifest(m)
    (app_env["bdir"] / app_file).write_bytes(
        (app_env["bdir"] / sent_file).read_bytes())
    # app 资产在线恢复 → 拒
    r = c.post("/admin/api/backup/restore", json={"file": app_file})
    assert r.status_code == 409 and "停服" in r.get_json()["msg"]
    # 路径穿越 → 拒
    r = c.post("/admin/api/backup/restore", json={"file": "../evil.db"})
    assert r.status_code == 400
    # 正在运行的任务(已出队, queue_size=0) → 拒
    from server.models import Job
    with app_env["app"].app_context():
        db_s = app_env["app"].extensions["sqlalchemy"]
        db_s.session.add(Job(kind="update", status="running"))
        db_s.session.commit()
    r = c.post("/admin/api/backup/restore", json={"file": sent_file})
    assert r.status_code == 409 and "任务" in r.get_json()["msg"]
    with app_env["app"].app_context():
        Job.query.filter_by(status="running").delete()
        db_s.session.commit()


def test_api_backup_settings_rejects_out_of_range(app_env):
    c = app_env["client"]
    base = {"backup_enabled": "1", "backup_daily_cron": "19:00",
            "backup_weekly_cron": "fri 19:10",
            "backup_daily_n": "10", "backup_weekly_n": "6", "backup_manual_n": "3"}
    for k, v, frag in [("backup_daily_cron", "25:99", "HH:MM"),
                       ("backup_daily_cron", "9:5", "HH:MM"),
                       ("backup_weekly_cron", "fri 25:00", "HH:MM"),
                       ("backup_weekly_cron", "fri 08:60", "HH:MM")]:
        bad = dict(base, **{k: v})
        r = c.post("/admin/api/backup/settings", json=bad)
        assert r.status_code == 400 and frag in r.get_json()["msg"], f"{k}={v} 未被拦截"


def test_data_html_restore_button_binds_by_run_id():
    """恢复按钮必须按 run id 绑定(修复: 按钮序号与 runs 下标错位会恢复错文件)"""
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert 'data-restore="${r.id}"' in html
    assert "runs.find(r => r.id === parseInt(b.dataset.restore))" in html
    assert "forEach((b, i)" not in html


def test_data_html_script_syntax():
    """模板内嵌 JS 语法门禁(node 解析)"""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node 不可用")
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m, "data.html 缺 script 块"
    r = subprocess.run([shutil.which("node"), "-e", "new Function(process.argv[1])", m.group(1)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"JS 语法错误: {r.stderr[:500]}"


def test_weekly_cron_parse():
    from server import jobs
    assert jobs._parse_weekly("fri 18:45") == ("fri", 18, 45)
    assert jobs._parse_weekly("周五 07:05") == ("fri", 7, 5)
    assert jobs._parse_weekly("garbage") == ("fri", 18, 45)   # 回落默认


def test_backup_retention_from_settings(app_env):
    from server import jobs
    with app_env["app"].app_context():
        jobs.set_setting("backup_daily_n", "9")
        jobs.set_setting("backup_weekly_n", "4")
        r = jobs.backup_retention()
        assert r["daily"] == 9 and r["weekly"] == 4 and r["pre"] == 10
