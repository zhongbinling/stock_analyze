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

def task_update(append):
    """增量更新: 抓取(指数+涨停池+板块) → 重算评分(大盘+板块温度)"""
    rc = _run([sys.executable, "-u", "fetch_daily.py"], append)
    if rc:
        return rc
    rc = _run([sys.executable, "-u", "score.py"], append)
    if rc:
        return rc
    return _run([sys.executable, "-u", "sector.py", "calc"], append)


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


# ---------------------------------------------------------------- 调度器

def _parse_hhmm(s, dh, dm):
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s or ""))
    if not m:
        return dh, dm
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return dh, dm


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

    for jid in ("daily_update", "daily_generate"):
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
    """种子数据 + 工作线程 + 调度器(一次)"""
    global _app_ref
    _app_ref = app
    with app.app_context():
        seed_defaults()
    ensure_worker(app)
    start_scheduler(app)
