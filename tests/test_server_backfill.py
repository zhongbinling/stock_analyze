# -*- coding: utf-8 -*-
"""D1-2/D1-3 server 侧测试: task_backfill 落库 / API 校验与入队 / logs 反查"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RESULT_OK = {"dataset": "zt_ts", "label": "涨停结构·tushare回补(2020起)", "status": "ok",
             "date_from": "2026-09-24", "date_to": "2026-09-28", "rows_written": 9,
             "days": [{"date": "2026-09-24", "rows": 3}, {"date": "2026-09-28", "rows": 6}],
             "duration_s": 1.2, "error": None}


@pytest.fixture()
def app(server_app, monkeypatch):
    """server_app + 拦截 jobs._run(不出子进程, 可控结果行)"""
    state = {"cmds": [], "rc": 0, "result": RESULT_OK}
    from server import jobs

    def fake_run(cmd, append, cwd=None, env_extra=None):
        state["cmds"].append([str(c) for c in cmd])
        append("$ " + " ".join(state["cmds"][-1]))
        if state["result"] is not None:
            append("@@BACKFILL@@ " + json.dumps(state["result"], ensure_ascii=False))
        return state["rc"]

    monkeypatch.setattr(jobs, "_run", fake_run)
    return {**server_app, "state": state}


def _logs(app):
    from server.models import DataBackfillLog
    with app["app"].app_context():
        return DataBackfillLog.query.order_by(DataBackfillLog.id.desc()).all()


def test_task_backfill_success_records_log(app):
    from server import jobs
    fn = jobs.task_backfill("zt_ts", "20200102", "manual")
    lines = []
    with app["app"].app_context():
        rc = fn(lines.append)
        rows = _logs(app)
    assert rc == 0
    assert len(rows) == 1
    r = rows[0]
    assert r.dataset == "zt_ts" and r.status == "done" and r.rows_written == 9
    assert r.date_from == "2026-09-24" and r.date_to == "2026-09-28"
    assert json.loads(r.days_json) == RESULT_OK["days"]
    assert r.duration_s == 1.2 and r.error is None
    assert any("@@BACKFILL@@" in ln for ln in lines)
    assert "--backfill" in " ".join(app["state"]["cmds"][0])


def test_task_backfill_fail_without_result_line(app):
    app["state"]["result"] = None                 # 子进程没输出结果行
    app["state"]["rc"] = 0
    from server import jobs
    fn = jobs.task_backfill("breadth", "20180102", "manual")
    with app["app"].app_context():
        rc = fn(lambda s: None)
        rows = _logs(app)
    assert rc == 1
    assert rows[0].status == "fail"


def test_task_backfill_executor_fail_status(app):
    app["state"]["result"] = dict(RESULT_OK, status="fail", error="RuntimeError: x")
    from server import jobs
    fn = jobs.task_backfill("zt_est", "20120101", "schedule")
    with app["app"].app_context():
        rc = fn(lambda s: None)
        rows = _logs(app)
    assert rc == 1 and rows[0].status == "fail" and "RuntimeError" in rows[0].error
    assert rows[0].trigger == "schedule"


def test_api_backfill_run_validation(app):
    c = app["client"]
    r = c.post("/admin/api/backfill/run", json={"dataset": "hack", "start": "20200102"})
    assert r.status_code == 400 and "dataset" in r.get_json()["msg"]
    r = c.post("/admin/api/backfill/run", json={"dataset": "zt_ts", "start": "2020-1-2"})
    assert r.status_code == 400 and "YYYYMMDD" in r.get_json()["msg"]


def test_api_backfill_run_enqueues(app):
    r = app["client"].post("/admin/api/backfill/run",
                           json={"dataset": "calendar", "start": "20200101"})
    assert r.status_code == 200 and r.get_json()["ok"]
    from server import jobs
    assert jobs.queue_size() == 1
    item = jobs._q.get_nowait()
    assert item["kind"] == "backfill" and item["target"] == "calendar"


def test_api_backfill_run_empty_start_uses_dataset_default(app):
    """start 留空 → 数据集默认起始(zt_est=20120101), 避免统一默认漏掉目标区间"""
    app["client"].post("/admin/api/backfill/run", json={"dataset": "zt_est", "start": ""})
    from server import jobs
    item = jobs._q.get_nowait()
    starts = [c for c in app["state"]["cmds"][-1]] if app["state"]["cmds"] else []
    assert jobs.queue_size() == 0                     # 已取出
    with app["app"].app_context():
        from server.models import Job  # noqa: F401
    # 闭包内 start 已固化为默认值: 执行任务验证 CLI 参数
    lines = []
    with app["app"].app_context():
        rc = item["fn"](lines.append)
    assert rc == 0
    assert "--start" in app["state"]["cmds"][0]
    assert app["state"]["cmds"][0][app["state"]["cmds"][0].index("--start") + 1] == "20120101"


def test_api_backfill_logs_and_date_lookup(app):
    from server.models import DataBackfillLog
    with app["app"].app_context():
        for ds, days in (("zt_ts", [{"date": "2026-03-14", "rows": 3},
                                    {"date": "2026-03-17", "rows": 5}]),
                         ("breadth", [{"date": "2026-04-01", "rows": 1}])):
            app["app"].extensions["sqlalchemy"].session.add(DataBackfillLog(
                dataset=ds, label=ds, status="done", rows_written=sum(x["rows"] for x in days),
                date_from=days[0]["date"], date_to=days[-1]["date"],
                days_json=json.dumps(days), started_at=__import__("datetime").datetime.now()))
        app["app"].extensions["sqlalchemy"].session.commit()
    c = app["client"]
    j = c.get("/admin/api/backfill/logs").get_json()
    assert len(j["logs"]) == 2
    j = c.get("/admin/api/backfill/logs?dataset=zt_ts").get_json()
    assert len(j["logs"]) == 1 and j["logs"][0]["dataset"] == "zt_ts"
    j = c.get("/admin/api/backfill/logs?date=2026-03-14").get_json()      # 按(数据集×日期)反查
    assert len(j["logs"]) == 1 and j["logs"][0]["dataset"] == "zt_ts"
    assert j["logs"][0]["days"] == [{"date": "2026-03-14", "rows": 3},
                                    {"date": "2026-03-17", "rows": 5}]
    j = c.get("/admin/api/backfill/logs?date=2026-12-31").get_json()
    assert j["logs"] == []


def test_api_backfill_logs_compact_date_lookup(app):
    """calendar 数据集的 days 为紧凑 cal_date 格式 → ISO 反查也能命中"""
    from server.models import DataBackfillLog
    import datetime as _dt
    with app["app"].app_context():
        s = app["app"].extensions["sqlalchemy"].session
        s.add(DataBackfillLog(dataset="calendar", label="日历", status="done",
                              rows_written=3, date_from="20260301", date_to="20260331",
                              days_json=json.dumps([{"date": "20260315", "rows": 3}]),
                              started_at=_dt.datetime.now()))
        s.commit()
    j = app["client"].get("/admin/api/backfill/logs?date=2026-03-15").get_json()
    assert len(j["logs"]) == 1 and j["logs"][0]["dataset"] == "calendar"
