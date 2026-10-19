# -*- coding: utf-8 -*-
"""任务系统: 单工作线程队列 + 子进程执行(core CLI) + APScheduler 定时 + 种子数据

任务类型:
  update    增量更新行情数据(core/fetch_daily.py + core/score.py)
  generate  按生成规则产出视图(core/viewer.py → views_store/)

设计要点:
  - 全局单工作线程串行执行, 内存队列上限20; 手动/定时统一入队
  - Job 行在开始执行时创建, 日志逐行落库(截断最近400行)供页面轮询
  - 调度与数据解耦: 更新(默认16:35)与生成(默认17:00)独立触发;
    定时生成前用"行情最新日期==今天"做守卫, 节假日/更新失败自动跳过;
    手动生成不受守卫限制(便于周末调参)
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

from server import db

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "core"
VIEWS_STORE = ROOT / "views_store"
QUEUE_CAP = 20

_q = queue.Queue()
_state_lock = threading.Lock()
_sched_lock = threading.Lock()
_worker_started = False
_scheduler = None
_comp_cache = None

DEFAULT_SETTINGS = {
    "auto_update": "1",
    "update_cron": "16:35",
    "auto_generate": "1",
    "generate_cron": "17:00",
    # B1 备份服务化: 类型滚动保留 + 调度策略(数据管理页可改, 热生效)
    "backup_enabled": "1",
    "backup_daily_cron": "18:35",       # 每日备份时刻(收盘任务链后)
    "backup_weekly_cron": "fri 18:45",  # 周备份: 周几 HH:MM
    "backup_daily_n": "14",
    "backup_weekly_n": "8",
    "backup_manual_n": "5",
    # D2-3: 补全后自动重算评分
    "backfill_recalc": "1",
}

SOURCES_SEED = [
    ("ths", "同花顺涨停复盘", "pool", "涨停/跌停/炸板/连板/接力率主源(约一年滚动史)"),
    ("eastmoney", "东方财富", "kline", "指数K线主源(push2his)"),
    ("tencent", "腾讯行情", "kline", "指数K线回退源(ifzq)"),
    ("legu", "乐咕乐股", "breadth", "当日涨跌家数(可选, 需akshare)"),
    ("tushare", "Tushare", "reserve", "预留: 更早涨停史/两融等, 需token"),
]


def _now():
    return datetime.now()          # 本地时间, 任务/生成记录便于直读


def utcnow_str():
    return _now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 设置读写

def get_setting(key, default=None):
    from server.models import AppSetting
    row = db.session.get(AppSetting, key)
    if row:
        return row.value
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(key, value):
    from server.models import AppSetting
    row = db.session.get(AppSetting, key)
    if row:
        row.value = str(value)
    else:
        db.session.add(AppSetting(skey=key, value=str(value)))
    db.session.commit()


def all_settings():
    return {k: get_setting(k) for k in DEFAULT_SETTINGS}


# ---------------------------------------------------------------- 种子数据

def _default_params() -> dict:
    with open(CORE / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return {"weights": cfg["weights"], "window": cfg.get("window", 250),
            "pool_min_periods": cfg.get("pool_min_periods", 60), "years": None}


def seed_defaults():
    from server.models import DataSource, ViewDef
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    if not (insp.has_table("data_sources") and insp.has_table("view_defs")):
        return                      # 迁移未执行(如 flask db migrate 阶段), 跳过
    if DataSource.query.count() == 0:
        for code, name, kind, note in SOURCES_SEED:
            db.session.add(DataSource(code=code, name=name, kind=kind, note=note))
        db.session.commit()
    if ViewDef.query.count() == 0:
        db.session.add(ViewDef(name="情绪视图·产线", env="prod",
                               params_json=json.dumps(_default_params(), ensure_ascii=False),
                               auto_publish=1))
        db.session.commit()


def component_meta():
    """[(key, 中文名, 默认权重)] 供生成页权重编辑器; 标签读 core/metrics(带缓存)"""
    global _comp_cache
    if _comp_cache:
        return _comp_cache
    labels = {}
    try:
        if str(CORE) not in sys.path:
            sys.path.insert(0, str(CORE))
        from metrics import LABELS as _L   # noqa: 首次会拉起pandas, 稍慢
        labels = dict(_L)
    except Exception:  # noqa: BLE001
        pass
    with open(CORE / "config.yaml", encoding="utf-8") as f:
        w = (yaml.safe_load(f) or {}).get("weights", {})
    _comp_cache = [(k, labels.get(k, k), float(v)) for k, v in w.items()]
    return _comp_cache


# ---------------------------------------------------------------- 工作线程与队列

def ensure_worker(app):
    global _worker_started
    with _state_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker_loop, args=(app,), daemon=True, name="sa-jobs").start()


def _worker_loop(app):
    while True:
        item = _q.get()
        with app.app_context():
            _execute(item)


def queue_size() -> int:
    return _q.qsize()


def enqueue(kind, fn, target="", trigger="manual"):
    """入队; 返回 (ok, msg)"""
    if _q.qsize() >= QUEUE_CAP:
        return False, "任务队列已满(20), 请稍后再试"
    _q.put({"kind": kind, "fn": fn, "target": target, "trigger": trigger})
    return True, "已加入队列"


def _execute(item):
    from server.models import Job
    job = Job(kind=item["kind"], target=item["target"], trigger=item["trigger"],
              status="running", started_at=_now())
    db.session.add(job)
    db.session.commit()
    lines = []

    def append(line):
        lines.append(str(line)[:2000])
        del lines[:-400]
        job.log = "\n".join(lines)
        db.session.commit()

    t0 = time.time()
    append(f"[{utcnow_str()}] 开始: {item['kind']} {item['target'] or ''}")
    rc = 1
    try:
        rc = int(item["fn"](append) or 0)
    except Exception as e:  # noqa: BLE001
        append(f"! 异常: {type(e).__name__}: {e}")
        rc = 1
    job.rc = rc
    job.status = "done" if rc == 0 else "fail"
    job.duration_s = round(time.time() - t0, 1)
    job.finished_at = _now()
    append(f"[{utcnow_str()}] 结束: {'成功' if rc == 0 else '失败'} ({job.duration_s}s)")
    db.session.commit()


# ---------------------------------------------------------------- 子进程执行

def _run(cmd, append, cwd=CORE, env_extra=None):
    append("$ " + " ".join(str(c) for c in cmd))
    env = None
    if env_extra:
        env = {**os.environ, **{k: str(v) for k, v in env_extra.items()}}
    p = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", env=env)
    for line in p.stdout:
        append(line.rstrip())
    rc = p.wait()
    if rc:
        append(f"! 退出码 {rc}")
    return rc


# ---------------------------------------------------------------- 任务定义

# ---------------------------------------------------------------- D2-1: 更新链分步执行

def _run_steps(append, steps):
    """分步执行: 每步分隔线+耗时+增量计数(pipeline_stats前后差); 失败即中止"""
    import server.market as market
    pre = market.pipeline_stats()
    n = len(steps)
    for i, (name, cmd, keys) in enumerate(steps, 1):
        append(f"━━━━ [{i}/{n}] {name} ({' '.join(cmd)}) ━━━━")
        t0 = time.time()
        rc = _run([sys.executable, "-u"] + cmd, append)
        if rc:
            append(f"━━━━ [{i}/{n}] {name} ✗ 失败, 中止后续步骤 ({time.time() - t0:.1f}s) ━━━━")
            return rc
        post = market.pipeline_stats()
        grown = [f"{k} +{post.get(k, {}).get('rows', 0) - pre.get(k, {}).get('rows', 0)}行"
                 f"(最新 {post.get(k, {}).get('last')})"
                 for k in keys
                 if post.get(k, {}).get("rows", 0) > pre.get(k, {}).get("rows", 0)]
        append(f"━━━━ [{i}/{n}] {name} ✓ ({time.time() - t0:.1f}s)"
               f"{'; ' + ', '.join(grown) if grown else ''} ━━━━")
        pre = post
    return 0


def task_update(append):
    """增量更新三步链(D2-1): 抓取 → 大盘评分 → 板块温度(含SW聚合)"""
    return _run_steps(append, [
        ("抓取", ["fetch_daily.py"], ("index", "mood", "sector_daily")),
        ("大盘评分", ["score.py"], ("sentiment",)),
        ("板块温度", ["sector.py", "calc"], ("bk", "sw1", "sw2")),
    ])


def task_recalc(append):
    """D2-3: 评分重算(不抓取): 补全后联动, 保证视图生成用到完整评分"""
    return _run_steps(append, [
        ("大盘评分", ["score.py"], ("sentiment",)),
        ("板块温度", ["sector.py", "calc"], ("bk", "sw1", "sw2")),
    ])


# ---------------------------------------------------------------- B1 备份任务

def backup_retention() -> dict:
    """类型滚动保留份数(设置热更新)"""
    return {"daily": int(get_setting("backup_daily_n", 14) or 14),
            "weekly": int(get_setting("backup_weekly_n", 8) or 8),
            "manual": int(get_setting("backup_manual_n", 5) or 5),
            "pre": 10}


def task_backup(btype: str, trigger: str = "manual"):
    """返回备份任务函数(闭包): in-process 调 core/backup.run_all + 落 BackupRun"""
    def fn(append):
        from server.models import BackupRun
        run = BackupRun(btype=btype, trigger=trigger, status="running", started_at=_now())
        db.session.add(run)
        db.session.commit()
        append(f"[{utcnow_str()}] 备份开始: {btype} ({trigger})")
        if str(CORE) not in sys.path:
            sys.path.insert(0, str(CORE))
        import backup as bk
        result = bk.run_all(btype, retention=backup_retention())
        for f in result["files"]:
            append(f"+ {f['file']} ({f['size'] / 1048576:.1f} MB)")
        if result["error"]:
            append(f"! {result['error']}")
        run.status = "done" if result["status"] == "ok" else "fail"
        run.files_n = result["files_n"]
        run.total_mb = result["total_mb"]
        run.duration_s = result["duration_s"]
        run.error = (result["error"] or "")[:255] or None
        run.files_json = json.dumps(result["files"], ensure_ascii=False)
        run.finished_at = _now()
        db.session.commit()
        append(f"[{utcnow_str()}] 备份结束: {result['status']} "
               f"({result['files_n']} 文件, {result['total_mb']} MB, {result['duration_s']}s)")
        return 0 if result["status"] == "ok" else 1
    return fn


def _slug(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name).strip("_") or "view"


def task_generate(def_id, scheduled=False):
    """返回生成任务函数(闭包)"""
    def fn(append):
        from server.models import ViewDef, ViewRun
        import server.market as market

        vd = db.session.get(ViewDef, def_id)
        if not vd or not vd.enabled:
            append("! 生成规则不存在或已停用")
            return 1
        if scheduled and not market.is_trading_day_today():
            append(f"[{utcnow_str()}] 今日非交易日或数据未更新, 跳过定时生成")
            return 0

        data_date = market.latest_index_date()
        if not data_date:
            append("! 行情库无数据, 请先执行数据更新")
            return 1

        params = json.loads(vd.params_json or "{}")
        run = ViewRun(def_id=vd.id, def_name=vd.name, env=vd.env,
                      vtype=vd.type or "sentiment", status="running", data_date=data_date)
        db.session.add(run)
        db.session.commit()
        t0 = time.time()
        out = (VIEWS_STORE / vd.env / (vd.type or "sentiment") /
               data_date.replace("-", "") /
               (time.strftime("%H%M%S") + "_" + _slug(vd.name) + ".html"))
        out.parent.mkdir(parents=True, exist_ok=True)
        override = {k: params[k] for k in ("weights", "window", "pool_min_periods") if k in params}
        tmp = ROOT / "data" / f"_gen_{run.id}.yaml"
        tmp.parent.mkdir(exist_ok=True)
        tmp.write_text(yaml.safe_dump(override, allow_unicode=True), encoding="utf-8")

        if vd.type == "sector_heat":
            cmd = [sys.executable, "-u", "sector_viewer.py", "--out", str(out)]
            if params.get("years"):
                cmd += ["--years", str(int(params["years"]))]
        else:
            cmd = [sys.executable, "-u", "viewer.py", "--out", str(out)]
            if params.get("years"):
                cmd += ["--years", str(int(params["years"]))]
        append(f"生成规则: {vd.name} [{vd.env}] 数据日期 {data_date}")
        rc = _run(cmd, append, env_extra={"SENTIMENT_OVERRIDE": tmp})
        try:
            tmp.unlink()
        except OSError:
            pass

        run.status = "done" if rc == 0 else "fail"
        run.generated_at = _now()
        run.duration_s = round(time.time() - t0, 1)
        run.file_path = str(out.relative_to(ROOT)).replace("\\", "/") if out.exists() else None
        run.size_kb = out.stat().st_size // 1024 if out.exists() else 0
        if rc == 0 and vd.env == "prod" and vd.auto_publish:
            run.published = 1
            run.published_at = _now()
            append("> 产线+自动发布: 已置为已发布(用户侧 S3 可见)")
        db.session.commit()
        return rc
    return fn


# ---------------------------------------------------------------- D1-2/D1-3: 数据补全

# 与 core/fetch_daily.py BACKFILLS 的 dataset 键一致(路由校验复用; 不import fetch_daily避免拉起pandas)
BACKFILL_DATASETS = {"zt_est": "涨停家数·估算回补(2012-2019)",
                     "zt_ts": "涨停结构·tushare回补(2020起)",
                     "breadth": "涨跌家数·tushare回补(2018起)",
                     "calendar": "交易日历·刷新",
                     "sector_history": "板块日线·历史回补(东财, 目标2012起)",
                     "sw_breadth": "板块涨跌家数·SW层级(2005起)",
                     "sw_pool": "涨停结构·SW层级(2005起, 含全市场段)",
                     "sw_fflow": "主力资金流·SW层级(2007起)"}
# 默认起始日(与 core BACKFILLS 第4元素保持同步): start 留空时使用, 避免统一默认
# 20200102 让 zt_est(2012-2019)/breadth(2018起) 静默漏掉目标区间
BACKFILL_DEFAULT_START = {"zt_est": "20120101", "zt_ts": "20200102",
                          "breadth": "20180102", "calendar": "20240101",
                          "sector_history": "20120101",
                          "sw_breadth": "20050101",
                          "sw_pool": "20050101",
                          "sw_fflow": "20070101"}


def task_backfill(dataset: str, start: str, trigger: str = "manual"):
    """返回补全任务函数(闭包): subprocess 跑统一CLI, 解析结果JSON → 落 DataBackfillLog"""
    def fn(append):
        from server.models import DataBackfillLog
        log = DataBackfillLog(dataset=dataset, label=BACKFILL_DATASETS.get(dataset, dataset),
                              status="running", trigger=trigger, started_at=_now())
        db.session.add(log)
        db.session.commit()
        append(f"[{utcnow_str()}] 补全开始: {dataset} 自 {start}")   # dataset代码留痕(target为中文)
        lines = []

        def tee(l):
            lines.append(l)
            append(l)

        rc = _run([sys.executable, "-u", "fetch_daily.py",
                   "--backfill", dataset, "--start", str(start)], tee)
        result = None
        for line in reversed(lines[-5:]):          # 末尾找结果协议行
            if line.startswith("@@BACKFILL@@ "):
                try:
                    result = json.loads(line[len("@@BACKFILL@@ "):])
                except json.JSONDecodeError:
                    pass
                break
        if rc == 0 and result is None:
            rc = 1
            append("! 未找到补全结果行(@@BACKFILL@@)")
        log.status = ("done" if rc == 0 and result and result.get("status") == "ok" else "fail")
        if result:
            log.date_from = result.get("date_from")
            log.date_to = result.get("date_to")
            log.rows_written = result.get("rows_written") or 0
            log.days_json = json.dumps(result.get("days") or [], ensure_ascii=False)
            log.duration_s = result.get("duration_s")
            log.error = (result.get("error") or "")[:255] or None
            append(f"[{utcnow_str()}] 补全结束: {log.status} "
                   f"(写入 {log.rows_written} 行 / {len(result.get('days') or [])} 天)")
        log.finished_at = _now()
        db.session.commit()
        # D2-3: 有实际写入且开关开 → 自动入队评分重算(独立job, 历史清晰)
        if (log.status == "done" and (log.rows_written or 0) > 0
                and get_setting("backfill_recalc", "1") == "1"):
            ok2, msg2 = enqueue("recalc", task_recalc, target="评分重算", trigger="schedule")
            append(f"> 基础数据已更新({log.rows_written}行), "
                   + (f"已自动入队评分重算" if ok2 else f"⚠ 评分重算入队失败: {msg2}"))
        return 0 if log.status == "done" else 1
    return fn

def _parse_hhmm(s, dh, dm):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s or ""))
    if not m:
        return dh, dm
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return dh, dm


# ---------------------------------------------------------------- 调度器

def scheduler_running() -> bool:
    return _scheduler is not None


def start_scheduler(app):
    global _scheduler
    if _scheduler is not None:
        return
    cfg = app.config.get("SERVER_CFG", {}).get("scheduler", {})
    if not cfg.get("enabled", False):
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler(timezone=cfg.get("timezone", "Asia/Shanghai"), daemon=True)
        sched.add_job(lambda: _safe_sync(app), "interval", seconds=300,
                      id="sync_crons", next_run_time=datetime.now())
        sched.start()
        _scheduler = sched
        _safe_sync(app)
        print(f"[scheduler] 已启动: 定时更新/生成(策略热更新每5分钟)")
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 启动失败(定时任务不可用, 手动不受影响): {e}")


def _safe_sync(app):
    try:
        with _sched_lock, app.app_context():
            _sync_cron_jobs(app)
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] 同步失败: {e}")


def _sync_cron_jobs(app):
    from apscheduler.triggers.cron import CronTrigger
    au = get_setting("auto_update") == "1"
    ag = get_setting("auto_generate") == "1"
    uh, um = _parse_hhmm(get_setting("update_cron"), 16, 35)
    gh, gm = _parse_hhmm(get_setting("generate_cron"), 17, 0)

    for jid in ("daily_update", "daily_generate", "daily_backup", "weekly_backup"):
        if _scheduler.get_job(jid):
            _scheduler.remove_job(jid)
    if au:
        _scheduler.add_job(lambda: enqueue("update", task_update, trigger="schedule"),
                           CronTrigger(hour=uh, minute=um, day_of_week="mon-fri"),
                           id="daily_update")
    if ag:
        _scheduler.add_job(_make_sched_generate(app),
                           CronTrigger(hour=gh, minute=gm, day_of_week="mon-fri"),
                           id="daily_generate")

    # B1: 日备/周备(策略热更新同机制)
    if get_setting("backup_enabled") == "1":
        bh, bm = _parse_hhmm(get_setting("backup_daily_cron"), 18, 35)
        _scheduler.add_job(lambda: enqueue("backup", task_backup("daily", "schedule"),
                                           target="daily", trigger="schedule"),
                           CronTrigger(hour=bh, minute=bm), id="daily_backup")
        dow, wh, wm = _parse_weekly(get_setting("backup_weekly_cron"))
        _scheduler.add_job(lambda: enqueue("backup", task_backup("weekly", "schedule"),
                                           target="weekly", trigger="schedule"),
                           CronTrigger(day_of_week=dow, hour=wh, minute=wm),
                           id="weekly_backup")


_WEEKDAYS = {"mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu", "fri": "fri",
             "sat": "sat", "sun": "sun", "周一": "mon", "周二": "tue", "周三": "wed",
             "周四": "thu", "周五": "fri", "周六": "sat", "周日": "sun"}


def _parse_weekly(s, dd="fri", dh=18, dm=45):
    """解析 "fri 18:45" / "周五 18:45" → (day_of_week, h, m); 非法回落默认"""
    m = re.match(r"^(\S+)\s+(\d{1,2}):(\d{2})$", str(s or "").strip())
    if m and m.group(1).lower() in _WEEKDAYS:
        h, mi = int(m.group(2)), int(m.group(3))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return _WEEKDAYS[m.group(1).lower()], h, mi
    return dd, dh, dm


def _make_sched_generate(app):
    def fn():
        from server.models import ViewDef
        with app.app_context():
            defs = ViewDef.query.filter_by(enabled=1).order_by(ViewDef.id).all()
            items = [(vd.id, vd.name) for vd in defs]
        for def_id, name in items:
            enqueue("generate", task_generate(def_id, scheduled=True),
                    target=name, trigger="schedule")
    return fn


def reschedule():
    """策略保存后立即生效"""
    if _scheduler is not None and _app_ref is not None:
        _safe_sync(_app_ref)


_app_ref = None


def ensure_started(app):
    """种子数据 + 中断任务标记 + 工作线程 + 调度器(一次)"""
    global _app_ref
    _app_ref = app
    with app.app_context():
        seed_defaults()
        _mark_interrupted()
    ensure_worker(app)
    start_scheduler(app)


def _mark_interrupted():
    """D1-4: 进程重启后, 上一进程遗留的 running 任务标记为失败(否则永久残留running)"""
    from server.models import BackupRun, DataBackfillLog, Job
    now = _now()
    for j in Job.query.filter_by(status="running").all():
        j.status = "fail"
        j.finished_at = now
        j.log = (j.log or "") + f"\n[{utcnow_str()}] ⚠ 服务重启, 任务中断"
    for r in DataBackfillLog.query.filter_by(status="running").all():
        r.status = "fail"
        r.error = "服务重启, 任务中断"
        r.finished_at = now
    for b in BackupRun.query.filter_by(status="running").all():
        b.status = "fail"
        b.error = "服务重启, 任务中断"
        b.finished_at = now
    db.session.commit()
