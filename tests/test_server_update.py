# -*- coding: utf-8 -*-
"""D2-1/D2-3 测试: 更新链分步结构 / task_recalc / 补全联动入队三态 / strategy 部分更新 / job 日志下钻"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

STATS_EMPTY = {"index": {"rows": 0, "last": None}, "mood": {"rows": 0, "last": None},
               "sentiment": {"rows": 0, "last": None}, "sector_daily": {"rows": 0, "last": None},
               "bk": {"rows": 0, "last": None}, "sw1": {"rows": 0, "last": None},
               "sw2": {"rows": 0, "last": None}}


@pytest.fixture()
def app(server_app, monkeypatch):
    """server_app + 拦截 _run 与 pipeline_stats(可控脚本序列与计数变化)"""
    from server import jobs, market
    state = {"cmds": [], "rcs": [], "stats_seq": [dict(STATS_EMPTY)]}

    def fake_run(cmd, append, cwd=None, env_extra=None):
        state["cmds"].append([str(c) for c in cmd])
        append(f"$ {' '.join(state['cmds'][-1])}")
        return state["rcs"].pop(0) if state["rcs"] else 0

    def fake_stats():
        if len(state["stats_seq"]) > 1:            # 按调用次序推进计数快照
            return dict(state["stats_seq"].pop(0))
        return dict(state["stats_seq"][0])

    monkeypatch.setattr(jobs, "_run", fake_run)
    monkeypatch.setattr(market, "pipeline_stats", fake_stats)
    return {**server_app, "state": state, "jobs": jobs, "market": market}


def _grow(base, **kw):
    d = {k: dict(v) for k, v in base.items()}
    for k, n in kw.items():
        d[k] = {"rows": n, "last": "2026-10-16"}
    return d


def test_task_update_three_steps_in_order(app):
    lines = []
    rc = app["jobs"].task_update(lines.append)
    assert rc == 0
    scripts = [" ".join(c[2:]) for c in app["state"]["cmds"]]
    assert scripts == ["fetch_daily.py", "score.py", "sector.py calc"]
    joined = "\n".join(lines)
    assert "[1/3] 抓取" in joined and "[2/3] 大盘评分" in joined and "[3/3] 板块温度" in joined
    assert "━━━━" in joined


def test_task_update_reports_row_growth(app):
    # 三步各推进一步计数: stats_seq 依次为 初始→抓取后→评分后→温度后
    s0 = dict(STATS_EMPTY)
    s1 = _grow(s0, index=100, mood=50, sector_daily=900)
    s2 = _grow(s1, sentiment=10)
    s3 = _grow(s2, bk=90, sw1=30, sw2=60)
    app["state"]["stats_seq"][:] = [s0, s1, s2, s3]
    lines = []
    app["jobs"].task_update(lines.append)
    joined = "\n".join(lines)
    assert "sentiment +10行" in joined
    assert "bk +90行" in joined and "sw1 +30行" in joined


def test_task_update_aborts_on_step_failure(app):
    app["state"]["rcs"] = [1]                       # 第一步失败
    lines = []
    rc = app["jobs"].task_update(lines.append)
    assert rc == 1
    assert len(app["state"]["cmds"]) == 1           # 后续步骤未执行
    assert "中止后续步骤" in "\n".join(lines)


def test_task_recalc_has_no_fetch(app):
    lines = []
    rc = app["jobs"].task_recalc(lines.append)
    assert rc == 0
    scripts = [" ".join(c[2:]) for c in app["state"]["cmds"]]
    assert scripts == ["score.py", "sector.py calc"]


def _run_backfill_task(app, rows_written):
    app["state"]["cmds"].clear()
    result = {"dataset": "zt_ts", "status": "ok", "date_from": "2026-09-24",
              "date_to": "2026-09-28", "rows_written": rows_written,
              "days": [{"date": "2026-09-24", "rows": rows_written}],
              "duration_s": 1.0, "error": None}
    orig_run = app["jobs"]._run

    def fake_run(cmd, append, cwd=None, env_extra=None):
        append("@@BACKFILL@@ " + json.dumps(result, ensure_ascii=False))
        return 0
    from server import jobs
    orig_run = jobs._run
    jobs._run = fake_run
    try:
        fn = jobs.task_backfill("zt_ts", "20200102", "manual")
        with app["app"].app_context():
            rc = fn(lambda s: None)
    finally:
        jobs._run = orig_run
    return rc


def test_backfill_auto_recalc_enqueued(app):
    with app["app"].app_context():
        app["jobs"].set_setting("backfill_recalc", "1")
    rc = _run_backfill_task(app, rows_written=9)
    assert rc == 0
    assert app["jobs"].queue_size() == 1            # 自动入队 recalc
    item = app["jobs"]._q.get_nowait()
    assert item["kind"] == "recalc" and item["trigger"] == "schedule"


def test_backfill_no_recalc_when_disabled(app):
    with app["app"].app_context():
        app["jobs"].set_setting("backfill_recalc", "0")
    _run_backfill_task(app, rows_written=9)
    assert app["jobs"].queue_size() == 0


def test_backfill_no_recalc_when_nothing_written(app):
    with app["app"].app_context():
        app["jobs"].set_setting("backfill_recalc", "1")
    _run_backfill_task(app, rows_written=0)         # 幂等空跑
    assert app["jobs"].queue_size() == 0


def test_backfill_recalc_enqueue_failure_message(app):
    """队列满 → 入队失败, 日志明示'入队失败'而非自相矛盾的'已入队'"""
    with app["app"].app_context():
        app["jobs"].set_setting("backfill_recalc", "1")
    while app["jobs"].enqueue("noop", lambda a: 0)[0]:   # 填满队列(上限20); 注意enqueue返回元组恒真
        pass
    collected = []
    from server import jobs
    result = {"dataset": "zt_ts", "status": "ok", "date_from": "2026-09-24",
              "date_to": "2026-09-28", "rows_written": 9,
              "days": [{"date": "2026-09-24", "rows": 9}], "duration_s": 1.0, "error": None}
    orig = jobs._run

    def fake(cmd, append, cwd=None, env_extra=None):
        append("@@BACKFILL@@ " + json.dumps(result, ensure_ascii=False))
        return 0
    jobs._run = fake
    try:
        with app["app"].app_context():
            jobs.task_backfill("zt_ts", "20200102", "manual")(collected.append)
    finally:
        jobs._run = orig
    joined = "\n".join(collected)
    assert "入队失败" in joined and "已自动入队" not in joined
    while not jobs._q.empty():                        # 清理, 不污染后续
        jobs._q.get_nowait()


def test_strategy_partial_update(app):
    c = app["client"]
    r = c.post("/admin/api/strategy", json={"backfill_recalc": "0"})
    assert r.status_code == 200
    with app["app"].app_context():
        assert app["jobs"].get_setting("backfill_recalc") == "0"
        assert app["jobs"].get_setting("auto_update") == "1"   # 未提交的键不动
    r = c.post("/admin/api/strategy", json={"backfill_recalc": "x"})
    assert r.status_code == 400
    r = c.post("/admin/api/strategy", json={})
    assert r.status_code == 400 and "无可保存" in r.get_json()["msg"]


def test_job_log_drilldown_api(app):
    from server.models import Job
    import datetime as _dt
    with app["app"].app_context():
        s = app["app"].extensions["sqlalchemy"].session
        j = Job(kind="update", target="", trigger="manual", status="done", rc=0,
                log="line1\nline2", started_at=_dt.datetime.now())
        s.add(j)
        s.commit()
        jid = j.id
    r = app["client"].get(f"/admin/api/job/{jid}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["log"] == "line1\nline2" and body["kind"] == "update"
    assert app["client"].get("/admin/api/job/99999").status_code == 404
