# -*- coding: utf-8 -*-
"""D1-33 数据管理review: 定时任务错过补跑 / ts-mood回看补采 / T+1源白名单 /
AI分析让道 / 修复链诊断措辞

取证(2026-11-24): data/service.log 92条 "was missed" — 系统休眠挂起进程,
16:35定时更新5周内错过10次且从不补跑; ts-mood步骤按墙上时钟"今天"查询,
16:35定时链恒查空(N次运行0次生效)。
"""
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

# 2026-11-20=周五, 11-23=周一, 11-24=周二, 11-27=周五, 11-28=周六


# ════════════════ ① 触发时刻推算 ════════════════

def test_trigger_time_helpers():
    from server import jobs as J
    daily = dict(hour=22, minute=35)
    wkday = dict(hour=16, minute=35, day_of_week="mon-fri")
    friday = dict(day_of_week="fri", hour=23, minute=45)
    tue = datetime(2026, 11, 24, 10, 0)
    assert J._last_trigger(daily, tue) == datetime(2026, 11, 23, 22, 35)
    assert J._last_trigger(wkday, tue) == datetime(2026, 11, 23, 16, 35)   # 周一
    assert J._next_trigger(wkday, tue) == datetime(2026, 11, 24, 16, 35)   # 当天
    assert J._last_trigger(friday, tue) == datetime(2026, 11, 20, 23, 45)
    assert J._next_trigger(friday, tue) == datetime(2026, 11, 27, 23, 45)
    sat = datetime(2026, 11, 28, 10, 0)                                   # 周六
    assert J._last_trigger(wkday, sat) == datetime(2026, 11, 27, 16, 35)
    assert J._next_trigger(wkday, sat) == datetime(2026, 11, 30, 16, 35)
    at = datetime(2026, 11, 24, 22, 35)                                   # 触发当刻=已到
    assert J._last_trigger(daily, at) == at
    assert J._next_trigger(daily, at) == datetime(2026, 11, 25, 22, 35)
    assert J._dow_set(None) == set(range(7))
    assert J._dow_set("mon-fri") == {0, 1, 2, 3, 4}
    assert J._dow_set("fri") == {4}
    assert J._dow_set("fri-sun") == {4, 5, 6}                             # 跨周环绕


# ════════════════ ② 错过补跑 ════════════════

class _Sched:
    """APScheduler替身: 只记录登记的任务"""
    def __init__(self):
        self.jobs = {}

    def get_job(self, jid):
        return self.jobs.get(jid)

    def remove_job(self, jid):
        self.jobs.pop(jid, None)

    def add_job(self, fn, trigger, id=None, **kw):
        self.jobs[id] = (fn, trigger)


@pytest.fixture()
def cron(server_app, monkeypatch):
    from server import jobs as J
    monkeypatch.setattr(J, "_scheduler", _Sched())
    J._caught_up.clear()                                # 进程内"已补跑"记忆
    J._queue_log.clear()
    while not J._q.empty():
        J._q.get_nowait()
    with server_app["app"].app_context():
        for k, v in (("auto_update", "1"), ("auto_generate", "1"), ("backup_enabled", "1")):
            J.set_setting(k, v)
    return {"J": J, "app": server_app["app"], "tmp": server_app["tmp"]}


def _sync(env, now):
    from server import jobs as J
    with J._sched_lock, env["app"].app_context():
        J._sync_cron_jobs(env["app"], now=now)


def _drain(env):
    """按入队顺序取出队列(保持登记簿, 以便检验去重)"""
    from server import jobs as J
    out = []
    while not J._q.empty():
        it = J._q.get_nowait()
        out.append((it["kind"], it["target"]))
    return out


def _add_job(env, kind, target, started, status="done", log=""):
    from server import db
    from server.models import Job
    with env["app"].app_context():
        db.session.add(Job(kind=kind, target=target, status=status,
                           started_at=started, log=log))
        db.session.commit()


ALL_IDS = {"daily_update", "stock_daily", "stock_weekly", "daily_generate",
           "briefing_evening", "briefing_morning", "daily_backup", "weekly_backup"}


def test_catchup_after_downtime(cron, capsys):
    """停机错过周一的定时任务, 周二02:00启动: 全部重挂 + 未执行的补跑一次。
    - 当天稍后还会定时运行的(更新链/个股日频)不补: 盘中补跑会把当日盘中
      快照写成"已有日", 挡住盘后的正式抓取
    - 备份错过即补; 周频个股链下次在周五 → 补; 简报过期(>3h)不补"""
    J = cron["J"]
    capsys.readouterr()
    _sync(cron, datetime(2026, 11, 24, 2, 0))
    assert set(J._scheduler.jobs) == ALL_IDS
    assert J._q.qsize() == 3
    printed = [l for l in capsys.readouterr().out.splitlines() if "补跑一次" in l]
    # 打印补跑的: 周频个股链 / 视图生成(无视图定义, 实际0项入队) / 日备 / 周备
    assert len(printed) == 4 and all("被错过" in l for l in printed)
    _sync(cron, datetime(2026, 11, 24, 2, 5))            # 再同步: 已排队的不重复
    assert J._q.qsize() == 3
    # 且不再误报"补跑"(准点触发/已补跑但尚未起跑的任务在任务表里还没有记录)
    assert "补跑一次" not in capsys.readouterr().out
    assert _drain(cron) == [("stock_daily", "个股数据·日频+季频"),
                            ("backup", "daily"), ("backup", "weekly")]
    for e in J._queue_log:
        e["state"] = "done"
    J._queue_log.clear()


def test_catchup_skips_when_todays_run_is_ahead(cron):
    """周二10:00(工作日盘中前): 周一16:35更新链被错过, 但当天16:35即将运行
    且会覆盖全部近端数据 → 不补; 备份不受此限"""
    J = cron["J"]
    _sync(cron, datetime(2026, 11, 24, 10, 0))
    q = _drain(cron)
    J._queue_log.clear()
    assert ("update", "") not in q
    assert ("stock_daily", "个股数据·日频") not in q      # 当天19:30会跑
    assert ("backup", "daily") in q


def test_catchup_runs_on_weekend(cron):
    """周六10:00: 周五的更新链/备份被错过, 下次定时在周一 → 立即补"""
    J = cron["J"]
    _sync(cron, datetime(2026, 11, 28, 10, 0))
    q = _drain(cron)
    J._queue_log.clear()
    assert q == [("update", ""), ("stock_daily", "个股数据·日频+季频"),
                 ("generate", "数据简报"),                # 周六07:00晨报, 迟到3h
                 ("backup", "daily"), ("backup", "weekly")]


def test_catchup_uses_job_history(cron):
    """该次触发之后该类任务已跑过 → 不补"""
    J = cron["J"]
    now = datetime(2026, 11, 24, 17, 0)                   # 周二17:00, 当天16:35已过
    _add_job(cron, "repair", "近7天", datetime(2026, 11, 24, 16, 36))   # 手动修复也算
    _sync(cron, now)
    q = _drain(cron)
    J._queue_log.clear()
    assert ("update", "") not in q


def test_catchup_reruns_interrupted_not_failed(cron):
    """被服务重启打断的任务不算执行过 → 重新补跑; 真失败的不无限重试"""
    J = cron["J"]
    now = datetime(2026, 11, 24, 17, 0)
    _add_job(cron, "update", "", datetime(2026, 11, 24, 16, 36), "fail",
             "[..] 开始: update\n[..] ⚠ 服务重启, 任务中断")
    _add_job(cron, "backup", "daily", datetime(2026, 11, 24, 16, 36), "fail",
             "! PermissionError: [WinError 5]")
    _sync(cron, now)
    q = _drain(cron)
    J._queue_log.clear()
    assert ("update", "") in q                           # 中断 → 补
    assert ("backup", "daily") not in q                  # 真失败(已在11-23 22:35之后起跑) → 不补


def test_catchup_briefing_freshness_limit(cron):
    """晚报错过50分钟 → 补; 晨报迟到16小时 → 放弃; 补过/跑过不再补"""
    J = cron["J"]
    now = datetime(2026, 11, 23, 23, 30)                  # 周一23:30
    _sync(cron, now)
    assert _drain(cron).count(("generate", "数据简报")) == 1
    J._queue_log.clear()
    _add_job(cron, "generate", "数据简报", datetime(2026, 11, 23, 22, 41))
    _sync(cron, now)
    assert _drain(cron).count(("generate", "数据简报")) == 0
    J._queue_log.clear()


def test_catchup_respects_gates(cron):
    """开关关闭的任务: 不登记也不补跑"""
    J = cron["J"]
    with cron["app"].app_context():
        J.set_setting("auto_update", "0")
        J.set_setting("backup_enabled", "0")
    _sync(cron, datetime(2026, 11, 28, 10, 0))
    ids = set(J._scheduler.jobs)
    q = _drain(cron)
    J._queue_log.clear()
    assert "daily_update" not in ids and "daily_backup" not in ids
    assert "briefing_morning" in ids                      # auto_generate仍开
    assert not any(k in ("update", "backup") for k, _t in q)


def test_scheduled_update_still_repair_chain():
    """回归: 定时更新任务仍为 task_repair(近1周)"""
    import inspect
    from server import jobs
    src = inspect.getsource(jobs._sync_cron_jobs)
    assert "task_repair(7" in src and "task_update, trigger" not in src


def test_repair_diagnosis_only_for_real_gaps(server_app, monkeypatch):
    """修复链诊断: 不再把"真缺口"说成"源端滞后", 也不对本没做过定向回补的
    缺口说"定向回补零写入—疑源端接口变化"(11-24 00:43 日志实测误诊)"""
    from server import jobs as J
    import server.market as M

    def scan(win):
        return {"trading_days": 5, "latest": "2026-11-23", "assets": [
            {"code": "etf_flow", "name": "宽基ETF份额", "db": "sentiment.db",
             "missing": ["2026-11-23"], "pending": [], "delay": ["2026-11-20"],
             "has_path": True},
            {"code": "sw_sic", "name": "SW等权指数", "db": "sentiment.db",
             "missing": ["2026-11-23"], "pending": [], "delay": [],
             "has_path": False}]}
    monkeypatch.setattr(M, "scan_gaps", scan)
    monkeypatch.setattr(J, "_run", lambda cmd, append, cwd=None, env_extra=None: 0)
    logs = []
    with server_app["app"].app_context():
        assert J.task_repair(7, "manual")(logs.append) == 0
    text = "\n".join(logs)
    assert "宽基ETF份额: 剩1日" in text and "点[补全]做定向回补" in text
    assert "SW等权指数: 剩1日" in text and "无定向通道" in text
    assert "定向回补零写入" not in text and "疑源端口径" not in text
    assert "源端滞后自愈" not in text                   # 真缺口不再被说成源端滞后


# ════════════════ ③ ts-mood 回看补采 ════════════════

def _ld_df():
    return pd.DataFrame({"limit": ["U", "U", "D", "Z"],
                         "industry": ["白酒", "白酒", "银行", "电子"],
                         "limit_times": [1, 2, None, None]})


@pytest.fixture()
def ts_env(tmp_path, monkeypatch):
    import fetch_daily as FD
    import sectors
    import tushare_api as T
    db = tmp_path / "s.db"
    con = sqlite3.connect(str(db))
    con.executescript("""
      CREATE TABLE index_daily(code TEXT, date TEXT);
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, dt_count INTEGER,
        zb_rate REAL, zb_count INTEGER, lb_max INTEGER, lb2_count INTEGER,
        jl_rate REAL, yzt_count INTEGER);
      CREATE TABLE sector_pool_stat(date TEXT, code TEXT, zt_count INTEGER, dt_count INTEGER,
        zb_count INTEGER, lb_max INTEGER, lb2_count INTEGER, PRIMARY KEY(date, code));
    """)
    con.executemany("INSERT INTO calendar VALUES(?,1)",
                    [(d,) for d in ("20261117", "20261118", "20261119",
                                    "20261120", "20261123", "20261124")])
    con.execute("INSERT INTO index_daily VALUES('1.000001', '2026-11-23')")
    con.execute("INSERT INTO mood_raw VALUES('2026-11-20', 28, 58, 20.0, NULL, 4, 4, 20.0, 23)")
    # 历史日的板块聚合已在; 仅最新交易日(11-23)缺 — 定时链从未写入的状态
    con.executemany("INSERT INTO sector_pool_stat(date,code,zt_count) VALUES(?,'BK1',3)",
                    [(f"2026-11-{d}",) for d in ("17", "18", "19", "20")])
    con.commit()
    con.close()
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(db)))
    monkeypatch.setattr(T, "has_token", lambda: True)
    monkeypatch.setattr(sectors, "_write_pool_agg",
                        lambda conn, d, ind: conn.execute(
                            "INSERT OR REPLACE INTO sector_pool_stat(date,code) "
                            "VALUES(?,'X')", (d,)))
    monkeypatch.setattr(sectors, "remap_pending", lambda conn: 0)
    return {"FD": FD, "T": T, "db": db}


def _q(db, sql, *args):
    con = sqlite3.connect(str(db))
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


def test_ts_mood_uses_latest_trading_day_not_wall_clock(ts_env, monkeypatch):
    """午夜后运行(旧实现查墙上时钟"今天"=未开盘日, 恒查空): 回看补最新交易日;
    THS未提供该日 → 全量写tushare口径 + 板块聚合"""
    FD, T = ts_env["FD"], ts_env["T"]
    calls = []
    monkeypatch.setattr(T, "limit_list_d",
                        lambda d: calls.append(d) or (_ld_df() if d == "20261123" else pd.DataFrame()))
    FD.fetch_tushare_mood_today()
    assert calls == ["20261123"]                    # 不是"今天"
    assert _q(ts_env["db"], "SELECT zt_count,dt_count,zb_count,zb_rate,lb_max,lb2_count,"
               "jl_rate,yzt_count FROM mood_raw WHERE date='2026-11-23'") == [
        (2, 1, 1, 33.3, 2, 1, 3.6, 28)]             # jl=1板+/昨涨停28, yzt=28
    assert ("2026-11-23", "X") in _q(ts_env["db"], "SELECT date, code FROM sector_pool_stat")


def test_ts_mood_keeps_ths_columns(ts_env, monkeypatch):
    """THS已提供涨停结构的日子只补tushare专有列(炸板家数) — 与 backfill_tushare
    "不覆盖已有THS段"一致; 旧实现仅当链路恰在晚间运行才覆盖(近16日仅3天),
    同一序列的处理方式随运行时刻而变且历史分被回改"""
    FD, T = ts_env["FD"], ts_env["T"]
    con = sqlite3.connect(str(ts_env["db"]))
    con.execute("INSERT INTO mood_raw VALUES('2026-11-23', 145, 3, 13.7, NULL, 9, 23, 82.0, 28)")
    con.commit()
    con.close()
    monkeypatch.setattr(T, "limit_list_d", lambda d: _ld_df())
    FD.fetch_tushare_mood_today()
    assert _q(ts_env["db"], "SELECT zt_count,dt_count,zb_rate,zb_count,lb_max,lb2_count,"
               "jl_rate,yzt_count FROM mood_raw WHERE date='2026-11-23'") == [
        (145, 3, 13.7, 1, 9, 23, 82.0, 28)]         # 仅zb_count新增
    assert ("2026-11-23",) in _q(ts_env["db"], "SELECT date FROM sector_pool_stat")


def test_ts_mood_skips_unpublished_and_stops(ts_env, monkeypatch):
    """最新日名单未出数 → 跳过继续补更早缺口; 连续2天空结果停(不空耗配额)"""
    FD, T = ts_env["FD"], ts_env["T"]
    calls = []
    con = sqlite3.connect(str(ts_env["db"]))
    con.execute("DELETE FROM sector_pool_stat WHERE date<'2026-11-23'")   # 历史日也缺
    con.commit()
    con.close()
    monkeypatch.setattr(T, "limit_list_d",
                        lambda d: calls.append(d) or (_ld_df() if d in ("20261119", "20261120")
                                                      else pd.DataFrame()))
    FD.fetch_tushare_mood_today()
    assert calls == ["20261123", "20261120", "20261119", "20261118", "20261117"]
    assert _q(ts_env["db"], "SELECT DISTINCT date FROM sector_pool_stat ORDER BY 1") == [
        ("2026-11-19",), ("2026-11-20",)]
    # 全部未出数: 连续2空即停
    calls.clear()
    con = sqlite3.connect(str(ts_env["db"]))
    con.execute("DELETE FROM sector_pool_stat")
    con.commit()
    con.close()
    monkeypatch.setattr(T, "limit_list_d",
                        lambda d: calls.append(d) or pd.DataFrame())
    FD.fetch_tushare_mood_today()
    assert calls == ["20261123", "20261120"]


def test_ts_mood_noop_when_pool_complete(ts_env, monkeypatch):
    """板块聚合已齐 → 不调用接口"""
    FD, T = ts_env["FD"], ts_env["T"]
    con = sqlite3.connect(str(ts_env["db"]))
    con.execute("INSERT INTO sector_pool_stat(date,code) VALUES('2026-11-23','BK1')")  # 历史日夹具已有
    con.commit()
    con.close()
    calls = []
    monkeypatch.setattr(T, "limit_list_d", lambda d: calls.append(d))
    FD.fetch_tushare_mood_today()
    assert calls == []


# ════════════════ ④ T+1 源体检白名单 ════════════════

def test_t1_sources_classified_as_delay(tmp_path, monkeypatch):
    """etf_flow/sector_pool 末端1个交易日的缺失=源端延迟(非真缺口);
    更早的缺日仍是真缺口"""
    from server import market as M
    p = tmp_path / "s.db"
    con = sqlite3.connect(str(p))
    con.executescript("""
      CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER);
      CREATE TABLE etf_share(code TEXT, trade_date TEXT, fd_share REAL);
      CREATE TABLE sector_pool_stat(date TEXT, code TEXT, zt_count INTEGER);
    """)
    con.executemany("INSERT INTO etf_share VALUES('510050.SH', ?, 1)",
                    [("20261120",), ("20261118",)])
    con.executemany("INSERT INTO sector_pool_stat VALUES(?, 'BK1', 3)",
                    [("2026-11-18",), ("2026-11-19",), ("2026-11-20",)])
    con.commit()
    con.close()
    core = tmp_path / "c"
    core.mkdir()
    shutil.copy(CORE / "config.yaml", core / "config.yaml")
    monkeypatch.setattr(M, "SENT_DB", p)
    monkeypatch.setattr(M, "_cfg_cache", None)
    monkeypatch.setattr(M, "CORE", core)                 # 不挂真实raw_daily库
    days = ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24"]
    monkeypatch.setattr(M, "_trading_days_since", lambda w: list(days))
    monkeypatch.setattr(M, "_today", lambda: "2026-11-24")
    by = {a["code"]: a for a in M.scan_gaps(7)["assets"]}
    assert by["etf_flow"]["delay"] == ["2026-11-23"]
    assert by["etf_flow"]["missing"] == ["2026-11-19"]
    assert by["etf_flow"]["pending"] == ["2026-11-24"]
    assert by["sector_pool"]["delay"] == ["2026-11-23"]
    assert by["sector_pool"]["missing"] == []


# ════════════════ ⑤ AI分析让道 ════════════════

def test_analyze_all_yields_to_waiting_tasks(server_app, monkeypatch):
    """队列里有等待任务 → 每批后让道: 剩余额度排到队尾(等等待任务先执行);
    队列空时续片跑完。实测: 1000条连续占串行队列5.3小时, 07:00晨报排到11:10"""
    import analyze_news as AN
    from server import jobs as J
    J._queue_log.clear()
    while not J._q.empty():
        J._q.get_nowait()
    calls = []

    def fake_batch(n):
        calls.append(n)
        return {"analyzed": n, "pending": 100}
    monkeypatch.setattr(AN, "analyze_batch", fake_batch)
    monkeypatch.setattr(AN, "analyze_cloud_batch", lambda n: {"analyzed": 0, "pending": 0})
    with server_app["app"].app_context():
        J.set_setting("ai_local_enabled", "1")
    assert server_app["client"].post("/admin/api/ai/analyze-all",
                                     json={"limit": 50}).status_code == 200
    item = J._q.get_nowait()
    J._q.put({"kind": "backup", "fn": lambda a: 0, "target": "daily",
              "trigger": "schedule"})                 # 模拟排队中的备份
    logs = []
    assert item["fn"](logs.append) == 0
    assert calls == [20]                               # 第一批后让道
    assert any("让道" in l for l in logs)
    assert J._q.qsize() == 2 and J._q.queue[0]["kind"] == "backup"   # 等待任务在前
    J._q.get_nowait()
    cont = J._q.get_nowait()
    assert cont["kind"] == "analyze_all" and "AI分析" in cont["target"]
    logs2 = []
    assert cont["fn"](logs2.append) == 0               # 续片: 队列已空 → 跑完
    assert calls == [20, 20, 10]
    assert any("达本次上限50条" in l for l in logs2)
    assert any("分析结束: 本次本地50" in l for l in logs2)
    J._queue_log.clear()


def test_enqueue_force_bypasses_dedup():
    """_force仅供让道续片: 同键排队中也能再入队"""
    from server import jobs as J
    J._queue_log.clear()
    while not J._q.empty():
        J._q.get_nowait()
    assert J.enqueue("analyze_all", lambda a: 0, target="t")[0]
    assert not J.enqueue("analyze_all", lambda a: 0, target="t")[0]      # 去重
    assert J.enqueue("analyze_all", lambda a: 0, target="t", _force=True)[0]
    assert J._q.qsize() == 2
    while not J._q.empty():
        J._q.get_nowait()
    J._queue_log.clear()


# ════════════════ review(1913742)复现用例 ════════════════

def test_catchup_update_never_intraday(cron, monkeypatch):
    """review: 交易日盘中不补更新链。原规则只在"当天稍后还会定时运行"时跳过
    — 把定时时刻改到已过去的更早时间(或非默认时刻)就会盘中补跑; 盘中跑链
    THS池返回的是盘中快照, 写入后当日即成"已有日", 盘后不再重抓。"""
    import server.market as M
    J = cron["J"]
    with cron["app"].app_context():
        J.set_setting("update_cron", "09:00")            # 当天09:00已过, 不再定时运行
    db = cron["tmp"] / "cal.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER)")
    con.execute("INSERT INTO calendar VALUES('20261124', 1)")        # 周二开市
    con.commit()
    con.close()
    monkeypatch.setattr(M, "_conn", lambda: sqlite3.connect(str(db)))
    _sync(cron, datetime(2026, 11, 24, 10, 0))           # 盘中
    q = _drain(cron)
    J._queue_log.clear()
    assert ("update", "") not in q
    _sync(cron, datetime(2026, 11, 24, 15, 6))           # 收盘后下一次同步即补
    assert ("update", "") in _drain(cron)
    J._queue_log.clear()


def test_catchup_update_allowed_on_closed_weekday(cron, monkeypatch):
    """日历标注休市的工作日盘中不属"交易时段" → 照常补"""
    import server.market as M
    J = cron["J"]
    with cron["app"].app_context():
        J.set_setting("update_cron", "09:00")
    db = cron["tmp"] / "cal.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER)")
    con.execute("INSERT INTO calendar VALUES('20261124', 0)")        # 节假日休市
    con.commit()
    con.close()
    monkeypatch.setattr(M, "_conn", lambda: sqlite3.connect(str(db)))
    _sync(cron, datetime(2026, 11, 24, 10, 0))
    assert ("update", "") in _drain(cron)
    J._queue_log.clear()


def test_catchup_check_error_keeps_other_jobs_registered(cron, monkeypatch):
    """review: 补跑判定某一项出错, 不能让排在后面的任务留在"已移除未重挂"
    状态(原实现逐项"移除→重挂→判定", 判定抛错即中断)。登记与补跑分两段。"""
    J = cron["J"]
    real = J._handled_since

    def flaky(kinds, since, *a, **kw):
        if "stock_daily" in kinds:
            raise RuntimeError("database is locked")
        return real(kinds, since, *a, **kw)
    monkeypatch.setattr(J, "_handled_since", flaky)
    _sync(cron, datetime(2026, 11, 28, 10, 0))
    assert set(J._scheduler.jobs) == ALL_IDS            # 全部照常重挂
    q = _drain(cron)
    J._queue_log.clear()
    assert ("update", "") in q and ("backup", "daily") in q   # 其余补跑不受影响


def test_ts_mood_skips_today_during_trading(ts_env, monkeypatch):
    """review: 交易时段内不写"今天" — tushare盘中返回的是未定型名单, 写入后
    当日即有板块聚合, 之后不再重拉(旧版同样会写入, 但晚间运行会覆盖修正;
    改为不覆盖THS列后就再也没有修正机会)。"""
    FD, T = ts_env["FD"], ts_env["T"]
    monkeypatch.setattr(FD, "_today_iso", lambda: "2026-11-23")
    monkeypatch.setattr(FD, "_now_hm", lambda: "10:30")
    monkeypatch.setattr(T, "limit_list_d", lambda d: _ld_df())
    FD.fetch_tushare_mood_today()
    assert ("2026-11-23", "X") not in _q(ts_env["db"], "SELECT date, code FROM sector_pool_stat")
    assert _q(ts_env["db"], "SELECT zb_count FROM mood_raw WHERE date='2026-11-23'") == []
    monkeypatch.setattr(FD, "_now_hm", lambda: "21:30")         # 盘后名单定型
    FD.fetch_tushare_mood_today()
    assert ("2026-11-23", "X") in _q(ts_env["db"], "SELECT date, code FROM sector_pool_stat")


def test_fetch_pools_skips_today_intraday(tmp_path, monkeypatch):
    """同源隐患: THS池步骤对已有日跳过(get_mood_dates按zt_count非空判定) —
    盘中跑链写入的是盘中快照, 盘后定时链不再重抓, 当日涨停结构被锁死在盘中值。
    交易时段内不抓"今天", 留待盘后。"""
    import fetch_daily as FD
    import em_api
    db = tmp_path / "s.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE mood_raw(date TEXT PRIMARY KEY, zt_count INTEGER, dt_count INTEGER,"
                " zb_rate REAL, lb_max INTEGER, jl_rate REAL, lb2_count INTEGER, yzt_count INTEGER)")
    con.commit()
    con.close()
    calls = []

    def fake_ths(d):
        calls.append(d)
        return {"zt_count": 12, "dt_count": 2, "zb_rate": 30.0, "lb_max": 3,
                "jl_rate": 20.0, "lb2_count": 4, "yzt_count": 10}
    monkeypatch.setattr(em_api, "ths_pool_stats", fake_ths)
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(db)))
    monkeypatch.setattr(FD, "_today_iso", lambda: "2026-11-24")
    cfg = {"fetch": {"sleep": 0}}
    monkeypatch.setattr(FD, "_now_hm", lambda: "14:20")         # 盘中
    FD.fetch_pools(cfg, ["2026-11-23", "2026-11-24"])
    assert calls == ["20261123"]                                # 当日不抓
    monkeypatch.setattr(FD, "_now_hm", lambda: "16:35")         # 盘后
    FD.fetch_pools(cfg, ["2026-11-23", "2026-11-24"])
    assert "20261124" in calls


def test_analyze_all_continuation_queue_full(server_app, monkeypatch):
    """review: 让道续排遇队列满 — 续片入队失败不能静默丢掉剩余额度,
    要如实写进日志并给出收尾行"""
    import analyze_news as AN
    from server import jobs as J
    J._queue_log.clear()
    while not J._q.empty():
        J._q.get_nowait()
    monkeypatch.setattr(AN, "analyze_batch", lambda n: {"analyzed": n, "pending": 100})
    monkeypatch.setattr(AN, "analyze_cloud_batch", lambda n: {"analyzed": 0, "pending": 0})
    with server_app["app"].app_context():
        J.set_setting("ai_local_enabled", "1")
    assert server_app["client"].post("/admin/api/ai/analyze-all",
                                     json={"limit": 60}).status_code == 200
    item = J._q.get_nowait()
    for i in range(J.QUEUE_CAP):                               # 队列被占满
        J._q.put({"kind": "noop", "fn": lambda a: 0, "target": f"x{i}", "trigger": "manual"})
    logs = []
    assert item["fn"](logs.append) == 0
    text = "\n".join(logs)
    assert "续排失败" in text and "剩余" in text      # 不是无声终止
    assert "分析结束: 本次本地20" in text
    J._queue_log.clear()
