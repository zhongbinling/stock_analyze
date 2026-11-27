# -*- coding: utf-8 -*-
"""测试隔离: 生成类任务(视图 / 简报)的输出目录 views_store 在测试里指向临时目录。

此前 server_app 夹具只隔离了 app.db, 而 tests/test_fix_dispatch_dedup.py 这类用例会通过真实的服务代码
跑生成任务, 占位视图文件按 server.jobs.VIEWS_STORE 落进生产的 views_store/ —
每跑一次全套留下 3 个(11-02 起累计 40 多个)。"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def test_views_store_points_outside_the_repo(tmp_path):
    from server import jobs
    p = Path(jobs.VIEWS_STORE).resolve()
    assert p.is_relative_to(tmp_path.resolve()) and p.name == "views_store"
    assert Path(jobs.ROOT).resolve() == tmp_path.resolve()             # file_path = 输出相对 jobs.ROOT
    assert not p.is_relative_to(REPO) and Path(jobs.CORE).resolve() == REPO / "core"   # 代码目录不变


def test_generate_writes_under_the_isolated_store(server_app, monkeypatch):
    from server import jobs
    from server.models import ViewDef
    cmds = []

    def fake_run(cmd, append, cwd=None, env_extra=None):
        cmds.append([str(c) for c in cmd])
        out = Path(cmd[cmd.index("--out") + 1])                      # 任务打算写到哪儿, 就在哪儿放个占位文件
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("<html>v</html>", encoding="utf-8")
        return 0
    monkeypatch.setattr(jobs, "_run", fake_run)
    with server_app["app"].app_context():
        db = server_app["app"].extensions["sqlalchemy"]
        vd = ViewDef(name="隔离检查", type="sentiment", env="prod", enabled=1, auto_publish=1)
        db.session.add(vd)
        db.session.commit()
        assert jobs.task_generate(vd.id)(lambda s: None) == 0
    out = Path(cmds[0][cmds[0].index("--out") + 1])
    assert out.is_file() and out.is_relative_to(Path(jobs.VIEWS_STORE).resolve())
    assert not out.is_relative_to(REPO / "views_store")
