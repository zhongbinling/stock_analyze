# -*- coding: utf-8 -*-
"""管理端路由: 管理登录 / 工作台 / 账号管理 (≥5/≥9) + S2 数据管理与数据生成 (≥7) + S3 视图管理 (≥5)"""
import json
import re
from datetime import datetime

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template,
                   request, send_file, url_for)
from flask_login import current_user, login_user

from server import ROOT, db
from server.auth import audit, level_name, require_level
from server.forms import LoginForm, ResetPwForm, UserAddForm
from server.models import (AdminLog, BackupRun, DataBackfillLog, DataSource, Job,
                           User, ViewDef, ViewRun)

bp_admin = Blueprint("admin", __name__, url_prefix="/admin")


# ---------------------------------------------------------------- 管理登录

@bp_admin.route("/login", methods=["GET", "POST"])
def admin_login():
    if current_user.is_authenticated and current_user.level >= 5:
        return redirect(url_for("admin.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        from server.routes import _do_login
        user = _do_login(form, admin_mode=True)
        if user:
            return redirect(url_for("admin.dashboard"))
    return render_template("login.html", form=form, admin_mode=True)


# ---------------------------------------------------------------- 工作台

@bp_admin.get("/")
@require_level(5)
def dashboard():
    users_n = User.query.filter_by(deleted_at=None).count()
    logs = AdminLog.query.order_by(AdminLog.id.desc()).limit(5).all()
    return render_template("admin/dashboard.html", users_n=users_n, logs=logs)


# ---------------------------------------------------------------- 账号管理 (9)

def _get_user(uid) -> User:
    return User.query.filter_by(id=uid, deleted_at=None).first()


@bp_admin.route("/accounts", methods=["GET", "POST"])
@require_level(9)
def accounts():
    form = UserAddForm()
    if form.validate_on_submit():
        u = User(username=form.username.data.strip(),
                 nickname=form.nickname.data.strip() or form.username.data.strip(),
                 level=form.level.data)
        u.set_password(form.password.data)
        db.session.add(u)
        db.session.commit()
        audit("user_add", target=u.username, detail=level_name(u.level))
        flash(f"已添加账号 {u.username} ({level_name(u.level)})", "ok")
        return redirect(url_for("admin.accounts"))

    users = (User.query.filter_by(deleted_at=None)
             .order_by(User.id).all())
    reset_form = ResetPwForm(prefix="rst")
    logs = AdminLog.query.order_by(AdminLog.id.desc()).limit(20).all()
    return render_template("admin/accounts.html", form=form,
                           users=users, reset_form=reset_form, logs=logs,
                           level_name=level_name)


def _guard_self(uid) -> bool:
    if current_user.id == uid:
        flash("不能对当前登录账号执行该操作", "error")
        return False
    return True


@bp_admin.post("/accounts/<int:uid>/toggle")
@require_level(9)
def account_toggle(uid):
    u = _get_user(uid)
    if not u:
        flash("账号不存在", "error")
    elif _guard_self(uid):
        u.enabled = 0 if u.enabled else 1
        db.session.commit()
        audit("user_toggle", target=u.username, detail="启用" if u.enabled else "停用")
        flash(f"{u.username} 已{'启用' if u.enabled else '停用'}", "ok")
    return redirect(url_for("admin.accounts"))


@bp_admin.post("/accounts/resetpw")
@require_level(9)
def account_resetpw():
    """uid 由表单下拉选择提交(无JS依赖)"""
    uid = request.form.get("uid", type=int)
    u = _get_user(uid) if uid else None
    form = ResetPwForm(prefix="rst")
    if not u:
        flash("请选择要重置密码的账号", "error")
    elif form.validate_on_submit():
        u.set_password(form.new.data)
        u.enabled = 1                     # 重置密码同时解除停用, 便于解救误操作
        db.session.commit()
        audit("user_resetpw", target=u.username)
        flash(f"已重置 {u.username} 的密码", "ok")
    else:
        flash("新密码至少8位且两次一致", "error")
    return redirect(url_for("admin.accounts"))


@bp_admin.post("/accounts/<int:uid>/delete")
@require_level(9)
def account_delete(uid):
    u = _get_user(uid)
    if not u:
        flash("账号不存在", "error")
        return redirect(url_for("admin.accounts"))
    if not _guard_self(uid):
        return redirect(url_for("admin.accounts"))
    n_root = User.query.filter_by(deleted_at=None, enabled=1).filter(User.level >= 9).count()
    # 目标本身是"当前可用的9级"且仅剩这一个时才拦截; 已停用的9级可安全删除
    if u.enabled and u.level >= 9 and n_root <= 1:
        flash("系统至少需要保留一个可用的超级管理员", "error")
        return redirect(url_for("admin.accounts"))
    from datetime import timezone
    original = u.username
    u.deleted_at = datetime.now(timezone.utc)
    u.enabled = 0
    # 软删除后释放用户名(改写为带标记的唯一形式), 允许后续注册/新建同名账号
    u.username = f"{original}~del{u.id}"
    db.session.commit()
    audit("user_delete", target=original, detail=f"{level_name(u.level)}(原名)")
    flash(f"已删除账号 {original}(软删除, 保留审计)", "ok")
    return redirect(url_for("admin.accounts"))


# ================================================= S2: 数据管理 / 数据生成 (≥7)

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@bp_admin.get("/data")
@require_level(7)
def data_page():
    return render_template("admin/data.html")


@bp_admin.get("/generate")
@require_level(7)
def generate_page():
    from server.jobs import component_meta
    return render_template("admin/generate.html", comp=component_meta())


# ---------------- 数据集 / 任务 ----------------

@bp_admin.get("/api/datasets")
@require_level(7)
def api_datasets():
    from server import market
    return jsonify({"datasets": market.datasets(),
                    "scores": market.score_datasets()})   # D2-2 评分数据状态


@bp_admin.post("/api/update")
@require_level(7)
def api_update():
    from server import jobs
    ok, msg = jobs.enqueue("update", jobs.task_update, trigger="manual")
    audit("job_update_enqueue" if ok else "job_update_reject", detail=msg)
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 409)


@bp_admin.get("/api/jobpoll")
@require_level(7)
def api_jobpoll():
    from server import jobs
    cur = Job.query.filter_by(status="running").order_by(Job.id.desc()).first()
    recent = (Job.query.filter(Job.status.in_(("done", "fail")))
              .order_by(Job.id.desc()).limit(10).all())
    run = None
    if cur:
        run = {"id": cur.id, "kind": cur.kind, "target": cur.target or "",
               "trigger": cur.trigger, "at": cur.started_at.strftime("%m-%d %H:%M:%S") if cur.started_at else "",
               "log": (cur.log or "")[-4000:]}
    return jsonify({"running": run, "queued": jobs.queue_size(),
                    "scheduler": "运行中" if jobs.scheduler_running() else "未启用",
                    "recent": [{"id": j.id, "kind": j.kind, "target": j.target or "",
                                "status": j.status, "rc": j.rc, "duration": j.duration_s,
                                "at": j.started_at.strftime("%m-%d %H:%M") if j.started_at else ""}
                               for j in recent]})


# ---------------- 更新策略 / 数据源 ----------------

@bp_admin.get("/api/strategy")
@require_level(7)
def api_strategy_get():
    from server.jobs import all_settings
    return jsonify(all_settings())


@bp_admin.post("/api/strategy")
@require_level(7)
def api_strategy_set():
    from server import jobs
    d = request.get_json(force=True) or {}
    # D2-3: 部分更新 — 只校验/保存本次出现的键(兼容旧全量提交与新开关单独保存)
    keys = ("auto_update", "update_cron", "auto_generate", "generate_cron", "backfill_recalc")
    present = [k for k in keys if k in d]
    if not present:
        return jsonify({"ok": False, "msg": "无可保存的设置项"}), 400
    errs = []
    for k in present:
        v = str(d.get(k)).strip()
        if k in ("update_cron", "generate_cron") and not _HHMM_RE.match(v):
            errs.append(f"{k} 应为 HH:MM")
        if k in ("auto_update", "auto_generate", "backfill_recalc") and v not in ("0", "1"):
            errs.append(f"{k} 应为 0/1")
    if errs:
        return jsonify({"ok": False, "msg": "; ".join(errs)}), 400
    for k in present:
        jobs.set_setting(k, str(d.get(k)).strip())
    jobs.reschedule()
    audit("strategy_update", detail="; ".join(f"{k}={d.get(k)}" for k in present))
    return jsonify({"ok": True, "msg": "已保存并生效"})


@bp_admin.get("/api/job/<int:jid>")
@require_level(7)
def api_job_log(jid):
    """D2-1: 任务历史下钻 — jobpoll 仅当前任务, 此处取任意历史任务完整日志"""
    j = db.session.get(Job, jid)
    if not j:
        return jsonify({"ok": False, "msg": "任务不存在"}), 404
    return jsonify({"id": j.id, "kind": j.kind, "target": j.target or "",
                    "trigger": j.trigger, "status": j.status, "rc": j.rc,
                    "duration": j.duration_s,
                    "at": j.started_at.strftime("%Y-%m-%d %H:%M:%S") if j.started_at else "",
                    "log": j.log or ""})


# ---------------- B1: 数据备份 ----------------

_BACKUP_TYPES = ("manual", "daily", "weekly")


def _backup_mod():
    import sys
    core = ROOT / "core"
    if str(core) not in sys.path:
        sys.path.insert(0, str(core))
    import backup as bk
    return bk


@bp_admin.post("/api/backup/run")
@require_level(7)
def api_backup_run():
    from server import jobs
    d = request.get_json(force=True) or {}
    btype = str(d.get("type", "manual"))
    if btype not in _BACKUP_TYPES:
        return jsonify({"ok": False, "msg": f"type 应为 {'/'.join(_BACKUP_TYPES)}"}), 400
    ok, msg = jobs.enqueue("backup", jobs.task_backup(btype, "manual"),
                           target=btype, trigger="manual")
    audit("backup_run_enqueue" if ok else "backup_run_reject",
          target=btype, detail=msg)
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 409)


@bp_admin.get("/api/backups")
@require_level(7)
def api_backups():
    runs = (BackupRun.query.order_by(BackupRun.id.desc()).limit(50).all())
    today = datetime.now().strftime("%Y-%m-%d")
    last_ok = {}
    for t in _BACKUP_TYPES:
        r = (BackupRun.query.filter_by(btype=t, status="done")
             .order_by(BackupRun.id.desc()).first())
        last_ok[t] = r.started_at.strftime("%m-%d %H:%M") if r and r.started_at else ""
    bk = _backup_mod()
    m = bk._load_manifest()
    disk_mb = sum(e.get("size", 0) for e in m["entries"]) / 1048576
    return jsonify({
        "runs": [{"id": r.id, "btype": r.btype, "trigger": r.trigger or "",
                  "status": r.status, "files_n": r.files_n, "total_mb": r.total_mb,
                  "duration": r.duration_s, "error": r.error or "",
                  "files": json.loads(r.files_json or "[]"),
                  "at": r.started_at.strftime("%m-%d %H:%M:%S") if r.started_at else ""}
                 for r in runs],
        "last_ok": last_ok,
        "today": today,
        "backed_today": any(r.btype in _BACKUP_TYPES and r.status == "done"
                            and r.started_at and r.started_at.strftime("%Y-%m-%d") == today
                            for r in runs),
        "disk_mb": round(disk_mb, 1), "files_n": len(m["entries"]),
    })


@bp_admin.post("/api/backup/settings")
@require_level(7)
def api_backup_settings():
    from server import jobs
    d = request.get_json(force=True) or {}
    errs = []
    if str(d.get("backup_enabled")) not in ("0", "1"):
        errs.append("backup_enabled 应为 0/1")
    dc = str(d.get("backup_daily_cron", "")).strip()
    if not re.match(r"^\d{1,2}:\d{2}$", dc) \
            or not (0 <= int(dc.split(":")[0]) <= 23 and 0 <= int(dc.split(":")[1]) <= 59):
        errs.append("日备时刻应为 HH:MM (00:00~23:59)")
    wk = str(d.get("backup_weekly_cron", "")).strip()
    wk_m = re.match(r"^(\S+)\s+(\d{1,2}):(\d{2})$", wk)
    if not wk_m or wk_m.group(1).lower() not in jobs._WEEKDAYS \
            or not (0 <= int(wk_m.group(2)) <= 23 and 0 <= int(wk_m.group(3)) <= 59):
        errs.append("周备应为 '周几 HH:MM' (如 fri 18:45 / 周五 18:45, 时刻00:00~23:59)")
    for k in ("backup_daily_n", "backup_weekly_n", "backup_manual_n"):
        v = str(d.get(k, "")).strip()
        if not (v.isdigit() and 1 <= int(v) <= 365):
            errs.append(f"{k} 应为 1~365")
    if errs:
        return jsonify({"ok": False, "msg": "; ".join(errs)}), 400
    for k in ("backup_enabled", "backup_daily_cron", "backup_weekly_cron",
              "backup_daily_n", "backup_weekly_n", "backup_manual_n"):
        jobs.set_setting(k, str(d.get(k)).strip())
    jobs.reschedule()
    audit("backup_settings", detail=f"日备{d.get('backup_daily_cron')} 周备{d.get('backup_weekly_cron')} "
                                    f"保留{d.get('backup_daily_n')}/{d.get('backup_weekly_n')}/{d.get('backup_manual_n')}")
    return jsonify({"ok": True, "msg": "已保存并生效"})


@bp_admin.post("/api/backup/verify")
@require_level(7)
def api_backup_verify():
    ok = _backup_mod().verify("--all")
    audit("backup_verify", detail="全部通过" if ok else "存在异常")
    return jsonify({"ok": ok, "msg": "全部备份校验通过" if ok else "存在异常备份, 详见服务日志/backup.log"})


@bp_admin.post("/api/backup/restore")
@require_level(9)
def api_backup_restore():
    from server import jobs
    d = request.get_json(force=True) or {}
    fname = str(d.get("file", "")).strip()
    if not fname or "/" in fname or "\\" in fname or ".." in fname:
        return jsonify({"ok": False, "msg": "文件名非法"}), 400
    bk = _backup_mod()
    m = bk._load_manifest()
    e = next((x for x in m["entries"] if x["file"] == fname), None)
    if not e:
        return jsonify({"ok": False, "msg": "备份不存在"}), 404
    if e["asset"] == "app":
        return jsonify({"ok": False, "msg": "app.db 为服务自身数据库, 请停服后用命令行恢复: "
                                            "cd core && python backup.py restore <文件> --yes"}), 409
    if jobs.queue_size() > 0 or Job.query.filter_by(status="running").count() > 0:
        return jsonify({"ok": False, "msg": "有任务运行/排队中, 为避免写冲突请稍后再试"}), 409
    ok = bk.restore(fname, yes=True)
    audit("backup_restore" if ok else "backup_restore_reject", target=fname)
    return jsonify({"ok": ok, "msg": "已恢复(恢复前已自动快照 pre-restore)" if ok
                                     else "恢复失败, 详见 backup.log"}), (200 if ok else 500)


# ---------------- D1-2/D1-3: 数据补全 ----------------

_START_RE = re.compile(r"^\d{8}$|^\d{4}-\d{2}-\d{2}$")


@bp_admin.post("/api/backfill/run")
@require_level(7)
def api_backfill_run():
    from server import jobs
    d = request.get_json(force=True) or {}
    dataset = str(d.get("dataset", "")).strip()
    start = str(d.get("start", "")).strip() or jobs.BACKFILL_DEFAULT_START.get(dataset, "")
    if dataset not in jobs.BACKFILL_DATASETS:
        return jsonify({"ok": False, "msg": f"dataset 应为 {'/'.join(jobs.BACKFILL_DATASETS)}"}), 400
    if not _START_RE.match(start):
        return jsonify({"ok": False, "msg": "start 应为 YYYYMMDD 或 YYYY-MM-DD"}), 400
    ok, msg = jobs.enqueue("backfill", jobs.task_backfill(dataset, start, "manual"),
                           target=jobs.BACKFILL_DATASETS[dataset], trigger="manual")
    audit("backfill_run_enqueue" if ok else "backfill_run_reject",
          target=dataset, detail=f"start={start}; {msg}")
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 409)


@bp_admin.get("/api/backfill/logs")
@require_level(7)
def api_backfill_logs():
    dataset = request.args.get("dataset", "").strip()
    date = request.args.get("date", "").strip()
    date_alt = date.replace("-", "") if date else ""      # 兼容紧凑格式(calendar 的 cal_date)
    q = DataBackfillLog.query
    if dataset:
        q = q.filter_by(dataset=dataset)
    rows = q.order_by(DataBackfillLog.id.desc()).limit(50).all()
    out = []
    for r in rows:
        days = json.loads(r.days_json or "[]")
        if date and not any(x.get("date") in (date, date_alt) for x in days):
            continue                      # 按(数据集×日期)反查: 只留写过该日的记录
        out.append({"id": r.id, "dataset": r.dataset, "label": r.label or "",
                    "date_from": r.date_from, "date_to": r.date_to,
                    "rows_written": r.rows_written, "status": r.status,
                    "trigger": r.trigger or "", "duration": r.duration_s,
                    "error": r.error or "", "days": days,
                    "at": r.started_at.strftime("%m-%d %H:%M:%S") if r.started_at else ""})
    # D1-4: 当前运行中的补全进度(解析回补循环既有的 [n/N] 日志输出)
    running = None
    cur = Job.query.filter_by(kind="backfill", status="running").order_by(Job.id.desc()).first()
    if cur and cur.log:
        m = re.findall(r"\[(\d+)/(\d+)\]", cur.log)
        if m:
            n, tot = int(m[-1][0]), int(m[-1][1])
            running = {"target": cur.target or "", "n": n, "total": tot,
                       "pct": round(n / tot * 100) if tot else 0}
    return jsonify({"logs": out, "running": running})


@bp_admin.get("/api/sources")
@require_level(7)
def api_sources():
    rows = DataSource.query.order_by(DataSource.id).all()
    return jsonify({"sources": [{"code": s.code, "name": s.name, "kind": s.kind,
                                 "enabled": bool(s.enabled), "has_key": bool(s.api_key),
                                 "note": s.note or ""} for s in rows]})


@bp_admin.post("/api/sources")
@require_level(7)
def api_sources_save():
    d = request.get_json(force=True) or {}
    ts_key_changed = False
    for code, v in (d or {}).items():
        ds = DataSource.query.filter_by(code=code).first()
        if not ds or not isinstance(v, dict):
            continue
        ds.enabled = 1 if v.get("enabled") else 0
        if str(v.get("api_key") or "").strip():
            key = str(v["api_key"]).strip()       # 空值不覆盖已存key
            ds.api_key = key
            if code == "tushare":
                # 同步写 core 侧 token 文件(采集进程读取), core/data/ 不入git
                p = ROOT / "core" / "data" / "tushare_token.txt"
                p.parent.mkdir(exist_ok=True)
                p.write_text(key, encoding="utf-8")
                ts_key_changed = True
    db.session.commit()
    audit("sources_update", detail=",".join(d.keys()))
    return jsonify({"ok": True, "msg": "数据源已保存"
                    + ("，tushare token 已同步" if ts_key_changed else "")})


# ---------------- 生成规则 CRUD / 执行 ----------------

def _def_row(vd: ViewDef) -> dict:
    p = json.loads(vd.params_json or "{}")
    w = p.get("weights") or {}
    top = sorted(w.items(), key=lambda kv: -kv[1])[:3]
    return {"id": vd.id, "name": vd.name, "env": vd.env, "type": vd.type or "sentiment",
            "auto_publish": bool(vd.auto_publish), "enabled": bool(vd.enabled),
            "params": {"weights": w, "window": p.get("window", 250),
                       "pool_min_periods": p.get("pool_min_periods", 60),
                       "years": p.get("years")},
            "wsum": 1.0 if (vd.type == "sector_heat" and not w) else round(sum(w.values()), 3),
            "wtop": "板块温度权重→config" if (vd.type == "sector_heat") else
                    " ".join(f"{k}:{v:.0%}" for k, v in top),
            "updated": vd.updated_at.strftime("%m-%d %H:%M") if vd.updated_at else ""}


def _validate_params(d: dict, comp_keys: set):
    weights = d.get("weights") or {}
    clean = {}
    for k, v in weights.items():
        if k not in comp_keys:
            return None, f"未知分量 {k}"
        try:
            wv = float(v)
        except (TypeError, ValueError):
            return None, f"权重 {k} 非数值"
        if not (0 <= wv <= 1):
            return None, f"权重 {k} 应在 0~1"
        clean[k] = round(wv, 4)
    try:
        window = int(d.get("window") or 250)
        pmp = int(d.get("pool_min_periods") or 60)
    except (TypeError, ValueError):
        return None, "窗口参数需为整数"
    if not (20 <= window <= 750):
        return None, "窗口N应在 20~750"
    if not (10 <= pmp <= window):
        return None, "池分量起步窗口应在 10~窗口N"
    years = d.get("years")
    if years not in (None, "", 0):
        try:
            years = int(years)
        except (TypeError, ValueError):
            return None, "年限需为整数"
        if not (1 <= years <= 30):
            return None, "年限应在 1~30"
    else:
        years = None
    return {"weights": clean, "window": window, "pool_min_periods": pmp,
            "years": years}, None


@bp_admin.get("/api/defs")
@require_level(7)
def api_defs():
    from server.jobs import component_meta
    defs = ViewDef.query.order_by(ViewDef.id).all()
    return jsonify({"defs": [_def_row(vd) for vd in defs],
                    "components": [{"key": k, "label": lb, "default": dv}
                                   for k, lb, dv in component_meta()]})


@bp_admin.post("/api/defs")
@require_level(7)
def api_def_create():
    d = request.get_json(force=True) or {}
    from server.jobs import component_meta
    name = str(d.get("name", "")).strip()
    env = d.get("env")
    vtype = d.get("type", "sentiment")
    if not (2 <= len(name) <= 32):
        return jsonify({"ok": False, "msg": "名称需2~32字"}), 400
    if env not in ("test", "prod"):
        return jsonify({"ok": False, "msg": "环境需为 test/prod"}), 400
    if vtype not in ("sentiment", "sector_heat"):
        return jsonify({"ok": False, "msg": "类型需为 sentiment/sector_heat"}), 400
    if ViewDef.query.filter_by(name=name).first():
        return jsonify({"ok": False, "msg": f"规则名 {name} 已存在, 请换名"}), 400
    params, err = _validate_params(d, {c[0] for c in component_meta()})
    if err:
        return jsonify({"ok": False, "msg": err}), 400
    vd = ViewDef(name=name, env=env, type=vtype,
                 params_json=json.dumps(params, ensure_ascii=False),
                 auto_publish=1 if d.get("auto_publish") else 0)
    db.session.add(vd)
    db.session.commit()
    audit("def_add", target=name, detail=f"env={env}")
    return jsonify({"ok": True, "id": vd.id, "msg": f"已创建规则 {name}"})


@bp_admin.post("/api/defs/<int:vid>")
@require_level(7)
def api_def_update(vid):
    vd = db.session.get(ViewDef, vid)
    if not vd:
        return jsonify({"ok": False, "msg": "规则不存在"}), 404
    d = request.get_json(force=True) or {}
    from server.jobs import component_meta
    if "name" in d:
        name = str(d.get("name", "")).strip()
        if not (2 <= len(name) <= 32):
            return jsonify({"ok": False, "msg": "名称需2~32字"}), 400
        dup = ViewDef.query.filter_by(name=name).first()
        if dup and dup.id != vd.id:
            return jsonify({"ok": False, "msg": f"规则名 {name} 已存在, 请换名"}), 400
        vd.name = name
    if "env" in d:
        if d["env"] not in ("test", "prod"):
            return jsonify({"ok": False, "msg": "环境需为 test/prod"}), 400
        vd.env = d["env"]
    if "weights" in d or "window" in d or "pool_min_periods" in d or "years" in d:
        merged = json.loads(vd.params_json or "{}")
        for k in ("weights", "window", "pool_min_periods", "years"):
            if k in d:
                merged[k] = d[k]
        params, err = _validate_params(merged, {c[0] for c in component_meta()})
        if err:
            return jsonify({"ok": False, "msg": err}), 400
        vd.params_json = json.dumps(params, ensure_ascii=False)
    if "auto_publish" in d:
        vd.auto_publish = 1 if d.get("auto_publish") else 0
    if "enabled" in d:
        vd.enabled = 1 if d.get("enabled") else 0
    db.session.commit()
    audit("def_update", target=vd.name)
    return jsonify({"ok": True, "msg": "已保存"})


@bp_admin.post("/api/defs/<int:vid>/toggle")
@require_level(7)
def api_def_toggle(vid):
    vd = db.session.get(ViewDef, vid)
    if not vd:
        return jsonify({"ok": False, "msg": "规则不存在"}), 404
    vd.enabled = 0 if vd.enabled else 1
    db.session.commit()
    audit("def_toggle", target=vd.name, detail="启用" if vd.enabled else "停用")
    return jsonify({"ok": True, "enabled": bool(vd.enabled)})


@bp_admin.post("/api/defs/<int:vid>/delete")
@require_level(7)
def api_def_delete(vid):
    vd = db.session.get(ViewDef, vid)
    if not vd:
        return jsonify({"ok": False, "msg": "规则不存在"}), 404
    name = vd.name
    db.session.delete(vd)
    db.session.commit()
    audit("def_delete", target=name)
    return jsonify({"ok": True, "msg": f"已删除规则 {name}(生成历史保留)"})


@bp_admin.post("/api/defs/<int:vid>/run")
@require_level(7)
def api_def_run(vid):
    from server import jobs
    vd = db.session.get(ViewDef, vid)
    if not vd:
        return jsonify({"ok": False, "msg": "规则不存在"}), 404
    ok, msg = jobs.enqueue("generate", jobs.task_generate(vd.id), target=vd.name)
    audit("def_run" if ok else "def_run_reject", target=vd.name, detail=msg)
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 409)


# ---------------- 生成历史 / 文件预览 ----------------

@bp_admin.get("/api/runs")
@require_level(7)
def api_runs():
    q = ViewRun.query.order_by(ViewRun.id.desc()).limit(30)
    return jsonify({"runs": [{"id": r.id, "def_id": r.def_id, "def_name": r.def_name or "--",
                              "status": r.status, "data_date": r.data_date,
                              "size_kb": r.size_kb, "duration": r.duration_s,
                              "published": bool(r.published),
                              "at": r.generated_at.strftime("%m-%d %H:%M") if r.generated_at else "",
                              "has_file": bool(r.file_path),
                              "file_url": url_for("admin.run_file", rid=r.id) if r.file_path else ""}
                             for r in q]})


@bp_admin.get("/runs/<int:rid>/file")
@require_level(5)
def run_file(rid):
    """视图文件预览: 仅允许 views_store 内文件(路径穿越防护)"""
    run = db.session.get(ViewRun, rid)
    p = _safe_run_file(run)
    if not p:
        abort(404)
    return send_file(p, mimetype="text/html")


# ================================================= S3: 视图管理 (≥5) + 用户行情页 (≥1)

def _safe_run_file(run):
    """返回视图文件绝对路径; 不在 views_store 内或不存在返回 None"""
    if not run or not run.file_path:
        return None
    p = (ROOT / run.file_path).resolve()
    store = (ROOT / "views_store").resolve()
    if store not in p.parents or not p.is_file():
        return None
    return p


@bp_admin.get("/views")
@require_level(5)
def views_page():
    return render_template("admin/views.html")


@bp_admin.get("/api/views")
@require_level(5)
def api_views():
    """生成记录列表: 筛选 环境/发布状态/数据日期/规则名"""
    q = ViewRun.query
    env = request.args.get("env", "")
    if env in ("test", "prod"):
        q = q.filter_by(env=env)
    status = request.args.get("status", "")
    if status == "published":
        q = q.filter_by(published=1)
    elif status == "unpublished":
        q = q.filter_by(published=0)
    d1, d2 = request.args.get("from", "").strip(), request.args.get("to", "").strip()
    if d1:
        q = q.filter(ViewRun.data_date >= d1)
    if d2:
        q = q.filter(ViewRun.data_date <= d2)
    kw = request.args.get("q", "").strip()
    if kw:
        q = q.filter(ViewRun.def_name.like(f"%{kw}%"))
    runs = q.order_by(ViewRun.id.desc()).limit(100).all()

    defs = {d.id: d for d in ViewDef.query.all()}
    rows = []
    for r in runs:
        d = defs.get(r.def_id)
        rows.append({"id": r.id, "def_id": r.def_id, "def_name": r.def_name or "--",
                     "env": r.env or "test", "vtype": r.vtype or "sentiment",
                     "status": r.status, "data_date": r.data_date or "--",
                     "size_kb": r.size_kb or 0, "duration": r.duration_s,
                     "published": bool(r.published),
                     "auto": bool(r.published and d and d.auto_publish and r.env == "prod"),
                     "publishable": bool(r.env == "prod" and r.status == "done" and r.file_path),
                     "at": r.generated_at.strftime("%m-%d %H:%M") if r.generated_at else "",
                     "pub_at": r.published_at.strftime("%m-%d %H:%M") if r.published_at else "",
                     "preview_url": url_for("admin.run_file", rid=r.id) if r.file_path else ""})
    def_cards = [{"id": d.id, "name": d.name, "env": d.env,
                  "auto_publish": bool(d.auto_publish), "enabled": bool(d.enabled)}
                 for d in ViewDef.query.filter_by(env="prod").order_by(ViewDef.id).all()]
    return jsonify({"runs": rows, "prod_defs": def_cards})


@bp_admin.post("/api/views/<int:rid>/publish")
@require_level(5)
def api_view_publish(rid):
    run = db.session.get(ViewRun, rid)
    if not run:
        return jsonify({"ok": False, "msg": "记录不存在"}), 404
    if run.env != "prod":
        return jsonify({"ok": False, "msg": "仅产线(prod)环境视图可发布"}), 400
    if run.status != "done" or not run.file_path or not _safe_run_file(run):
        return jsonify({"ok": False, "msg": "该记录无有效产物文件"}), 400
    run.published = 1
    run.published_at = datetime.now()
    db.session.commit()
    audit("view_publish", target=f"run#{rid}", detail=f"{run.def_name} 数据日期{run.data_date}")
    return jsonify({"ok": True, "msg": "已发布"})


@bp_admin.post("/api/views/<int:rid>/unpublish")
@require_level(5)
def api_view_unpublish(rid):
    run = db.session.get(ViewRun, rid)
    if not run:
        return jsonify({"ok": False, "msg": "记录不存在"}), 404
    run.published = 0
    db.session.commit()
    audit("view_unpublish", target=f"run#{rid}", detail=run.def_name or "")
    return jsonify({"ok": True, "msg": "已下线"})


@bp_admin.post("/api/views/<int:rid>/delete")
@require_level(5)
def api_view_delete(rid):
    run = db.session.get(ViewRun, rid)
    if not run:
        return jsonify({"ok": False, "msg": "记录不存在"}), 404
    p = _safe_run_file(run)
    if p:
        try:
            p.unlink()
        except OSError as e:
            return jsonify({"ok": False, "msg": f"文件删除失败: {e}"}), 500
    name, was_pub = run.def_name, bool(run.published)
    db.session.delete(run)
    db.session.commit()
    audit("view_delete", target=f"run#{rid}", detail=f"{name}{'(曾已发布)' if was_pub else ''}")
    return jsonify({"ok": True, "msg": "已删除记录与文件"})


@bp_admin.post("/api/defs/<int:vid>/autopublish")
@require_level(5)
def api_def_autopublish(vid):
    """产线规则自动发布开关(运营≥5专用, 与数据生成页≥7分权)"""
    vd = db.session.get(ViewDef, vid)
    if not vd:
        return jsonify({"ok": False, "msg": "规则不存在"}), 404
    if vd.env != "prod":
        return jsonify({"ok": False, "msg": "仅产线规则有自动发布"}), 400
    vd.auto_publish = 0 if vd.auto_publish else 1
    db.session.commit()
    audit("def_autopublish", target=vd.name, detail="开启" if vd.auto_publish else "关闭")
    return jsonify({"ok": True, "auto_publish": bool(vd.auto_publish)})


# ---------------- S8: 概念板块开关 / 板块导出 ----------------

@bp_admin.post("/api/sectors/concept-toggle")
@require_level(7)
def api_concept_toggle():
    """概念板块批量启停(开启后次日采集扩展至概念, 温度排行自动包含)"""
    from server.models import DataSource  # noqa: F401 (确认表存在)
    from server import ROOT as _R
    import sqlite3 as _sq
    db_path = _R / "core" / "data" / "sentiment.db"
    con = _sq.connect(str(db_path))
    cur = con.execute("SELECT enabled FROM sectors WHERE kind='concept' LIMIT 1")
    row = cur.fetchone()
    new_val = 0 if (row and row[0]) else 1
    n = con.execute("UPDATE sectors SET enabled=? WHERE kind='concept'", (new_val,)).rowcount
    con.commit()
    con.close()
    audit("concept_toggle", detail=f"概念板块{'启用' if new_val else '停用'}({n}个)")
    return jsonify({"ok": True, "enabled": bool(new_val), "count": n,
                    "msg": f"概念板块已{'启用' if new_val else '停用'}({n}个), "
                           f"次日采集与温度计算将{'包含' if new_val else '不包含'}概念板块"})
