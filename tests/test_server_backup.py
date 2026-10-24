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
    app_file = sent_file.replace("sentiment.db", "app.db")
    m["entries"].append({**m["entries"][0], "file": app_file, "asset": "app"})
    BK._save_manifest(m)
    # D1-10 B1: zip布局 — 往zip里追加app.db成员
    import zipfile as _zf
    zp = app_env["bdir"] / sent_file.split("/")[0]
    with _zf.ZipFile(zp) as z:
        sent_bytes = z.read("sentiment.db")
    with _zf.ZipFile(zp, "a") as z:
        z.writestr("app.db", sent_bytes)
    # app 资产在线恢复 → 拒(且提示带真实文件名的停服命令)
    r = c.post("/admin/api/backup/restore", json={"file": app_file})
    assert r.status_code == 409 and "停服" in r.get_json()["msg"]
    assert app_file in r.get_json()["msg"], "停服指引应含真实备份文件名"
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
    """D1-11 C4: data.html恢复按钮已移除(prompt流废弃), 恢复入口集中到备份中心;
    本测试锁定: 不再有按run绑定的恢复按钮, 也不回退到按下标绑定"""
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert 'data-restore="${r.id}"' not in html
    assert "prompt(" not in html
    assert "backups_page" in html                    # 备份中心入口存在


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


# ── D1-11 C1/C2: 备份中心 ──

def test_backups_page_and_manifest_api(app_env):
    """C1: /admin/backups 页面 + manifest分组API(块映射/新鲜度/存在性/校验态)"""
    from server import jobs
    with app_env["app"].app_context():
        jobs.task_backup("daily")(lambda s: None)
    c = app_env["client"]
    r = c.get("/admin/backups")
    assert r.status_code == 200 and "备份中心".encode() in r.data
    r = c.get("/admin/api/backups/manifest")
    j = r.get_json()
    assert r.status_code == 200 and j["summary"]["files_n"] == 1
    run = j["runs"][0]
    assert run["files_n"] == 1 and run["type_label"] == "日备"
    m0 = run["members"][0]
    assert m0["asset"] == "sentiment" and m0["block"] == "基础+评分"
    assert m0["verify"] is None and m0["exists"] is True
    assert m0["fresh"].startswith("评分")


def test_api_backup_verify_enqueues_and_job_writes_back(app_env):
    """C2: 校验入队为异步job; job执行后结果写回manifest条目"""
    from server import jobs
    with app_env["app"].app_context():
        jobs.task_backup("daily")(lambda s: None)
    c = app_env["client"]
    r = c.post("/admin/api/backup/verify", json={})
    assert r.status_code == 200 and r.get_json()["ok"]
    item = jobs._q.get_nowait()
    assert item["kind"] == "verify" and item["target"] == "全部备份"
    with app_env["app"].app_context():
        rc = item["fn"](lambda s: None)
    assert rc == 0
    m = json.loads(BK.MANIFEST.read_text(encoding="utf-8"))
    assert m["entries"][-1]["verify"]["ok"] is True


def test_api_backup_verify_folder_target_and_unknown(app_env):
    """C1: 校验此组=目录前缀目标; 未知目标404"""
    from server import jobs
    with app_env["app"].app_context():
        jobs.task_backup("daily")(lambda s: None)
    c = app_env["client"]
    folder = BK._load_manifest()["entries"][-1]["file"].split("/")[0]
    r = c.post("/admin/api/backup/verify", json={"file": folder})
    assert r.status_code == 200
    assert jobs._q.get_nowait()["kind"] == "verify"
    r = c.post("/admin/api/backup/verify", json={"file": "nope.zip"})
    assert r.status_code == 404


def test_api_backup_verify_empty_body_ok(app_env):
    """D1-11r: 页面按钮POST不带body → 不得400(get_json对空body抛错已修)"""
    from server import jobs
    c = app_env["client"]
    r = c.post("/admin/api/backup/verify")           # 无 body
    assert r.status_code == 200 and r.get_json()["ok"]
    assert jobs._q.get_nowait()["kind"] == "verify"


def test_api_backup_run_blocks(app_env):
    """D1-11 C3: blocks白名单(base/account); 非法块400; 子集只备所选资产"""
    from server import jobs
    c = app_env["client"]
    bad = c.post("/admin/api/backup/run", json={"type": "manual", "blocks": ["hack"]})
    assert bad.status_code == 400
    r = c.post("/admin/api/backup/run", json={"type": "daily", "blocks": ["base"]})
    assert r.status_code == 200 and r.get_json()["ok"]
    item = jobs._q.get_nowait()
    assert item["kind"] == "backup" and "base" in item["target"]
    with app_env["app"].app_context():
        rc = item["fn"](lambda s: None)
    assert rc == 0
    m = BK._load_manifest()
    assert {e["asset"] for e in m["entries"]} == {"sentiment"}   # 本环境base块=sentiment


def test_api_backup_restore_success_online(app_env):
    """D1-11 C4: sentiment在线恢复闭环 — 删数据→恢复→数据回来+pre-restore兜底"""
    from server import jobs
    with app_env["app"].app_context():
        jobs.task_backup("daily")(lambda s: None)
    fname = BK._load_manifest()["entries"][-1]["file"]
    conn = sqlite3.connect(str(app_env["sent"]))
    conn.execute("DELETE FROM sentiment")
    conn.commit()
    conn.close()
    r = app_env["client"].post("/admin/api/backup/restore", json={"file": fname})
    assert r.status_code == 200 and r.get_json()["ok"]
    conn = sqlite3.connect(str(app_env["sent"]))
    n = conn.execute("SELECT COUNT(*) FROM sentiment").fetchone()[0]
    conn.close()
    assert n == 1
    assert "pre-restore" in {e["type"] for e in BK._load_manifest()["entries"]}


def test_backups_html_binds_and_syntax():
    """C1: 模板绑定(组校验/清单API/入口链接) + JS语法门禁"""
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "backups.html").read_text(encoding="utf-8")
    assert 'id="btnVerifyAll"' in html and "data-vfolder" in html
    assert "/admin/api/backups/manifest" in html and "jobpoll" in html
    assert "--bg2" not in html, "使用了不存在的主题变量(暗色模式会回退成亮色)"
    # D1-11 C4: 恢复弹窗与分块默认勾选
    assert 'id="modalMask"' in html and "data-rgroup" in html and "data-restore" in html
    assert "RESTORE_INFO" in html and "pre-restore" in html
    assert "基础+评分" in html                       # 组恢复默认勾基础块的判定串
    data_html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "bkBlockBase" in data_html and "bkBlockAcc" in data_html
    assert "blocks" in data_html                     # 立即备份带范围
    data_html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "backups_page" in data_html                 # 数据管理页入口
    if not shutil.which("node"):
        pytest.skip("node 不可用")
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m, "backups.html 缺 script 块"
    r = subprocess.run([shutil.which("node"), "-e", "new Function(process.argv[1])", m.group(1)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"JS 语法错误: {r.stderr[:500]}"
