# -*- coding: utf-8 -*-
"""管理端路由: 管理登录 / 工作台 / 账号管理 (≥5/≥9) + S2 数据管理与数据生成 (≥7) + S3 视图管理 (≥5)"""
import json
import re
import time
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
    from server import market
    # P4①: SCAN_DS服务端单点(替代前端BF_ROWS镜像, _REPAIR_DS唯一真源)
    return render_template("admin/data.html",
                          repair_ds_json=json.dumps(market._REPAIR_DS))


# ── D1-17 R3+R4: 实时行情页与API ──

@bp_admin.get("/realtime")
@require_level(7)
def realtime_page():
    return render_template("admin/realtime.html")


# ── 避雷针A1: 个股分析页与API ──

@bp_admin.get("/jobs")
@require_level(7)
def jobs_center_page():
    """任务运行中心: 运行中/排队/计划 三视图 + 历史"""
    return render_template("admin/jobs_center.html")


@bp_admin.get("/api/jobs/center")
@require_level(7)
def api_jobs_center():
    """三视图数据: running(Job表) + queued(登记簿) + scheduled(jobstore) + paused"""
    from server import jobs as J
    from server.models import Job
    running = []
    try:
        for j in Job.query.filter_by(status="running").all():
            started = j.started_at
            elapsed = None
            if started:
                elapsed = round((datetime.now() - started).total_seconds())
            running.append({"id": j.id, "kind": j.kind, "target": j.target,
                            "progress": j.progress, "started_at": str(started or ""),
                            "elapsed_s": elapsed,
                            "log": (j.log or "")[-4000:]})   # 实时日志流(尾部4k)
    except Exception:                                       # noqa: BLE001
        pass
    return jsonify({"running": running,
                    "queued": J.queue_snapshot(),
                    "scheduled": J.scheduled_jobs_view(),
                    "paused": J.is_paused()})


@bp_admin.get("/api/jobs")
@require_level(7)
def api_jobs_list():
    """历史任务列表(最近N条, 任务运行中心/历史区)"""
    from server.models import Job
    n = min(int(request.args.get("limit", 20) or 20), 100)
    jobs = Job.query.order_by(Job.id.desc()).limit(n).all()
    return jsonify({"jobs": [{"id": j.id, "kind": j.kind, "target": j.target,
                              "status": j.status, "progress": j.progress,
                              "duration_s": j.duration_s,
                              "started_at": str(j.started_at or "")} for j in jobs]})


@bp_admin.post("/api/jobs/pause")
@require_level(7)
def api_jobs_pause():
    """暂停/恢复队列(worker挂起取件, 排队不丢)"""
    from server import jobs as J
    d = request.get_json(force=True, silent=True) or {}
    on = J.set_paused(bool(d.get("paused", True)))
    return jsonify({"ok": True, "paused": on,
                    "msg": "队列已暂停(运行中任务不受影响, 新任务排队)"
                    if on else "队列已恢复"})


@bp_admin.get("/stock")
@require_level(7)
def stock_page():
    """避雷针: 个股K线+多均线+量额(A1); A2~A5逐步扩展"""
    import fetch_stock as FS
    return render_template("admin/stock.html", pool=FS.pool_list())


# ── D1-31: 历史回测(市场状态/行为指数/因子策略) ──

@bp_admin.get("/backtest")
@require_level(7)
def backtest_page():
    return render_template("admin/backtest.html")


@bp_admin.get("/api/backtest/factors")
@require_level(7)
def api_bt_factors():
    import behavior as BH
    return jsonify({"factors": [
        {"key": k, "name": n, "default_w": w, "desc": d}
        for k, n, w, d in BH.FACTOR_DEFS],
        "defaults": {"entry": BH.DEFAULT_ENTRY, "exit": BH.DEFAULT_EXIT}})


@bp_admin.post("/api/backtest/compute")
@require_level(7)
def api_bt_compute():
    """因子组合+状态概率+回测(只读计算~5s首算/0.1s缓存; 交互式同步返回)。
    review: 全不勾因子曾静默回退默认权重 — 用户以为在测自己的组合"""
    import behavior as BH
    d = request.get_json(force=True, silent=True) or {}
    weights = {k: v for k, v in (d.get("factors") or {}).items()
               if isinstance(v, (int, float)) and -100 <= v <= 100}
    if not weights:
        return jsonify({"ok": False, "msg": "请至少勾选一个因子"}), 400
    try:
        entry = max(-100, min(100, int(d.get("entry", BH.DEFAULT_ENTRY))))
        exit_ = max(-100, min(100, int(d.get("exit", BH.DEFAULT_EXIT))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "entry/exit应为整数"}), 400
    # D1-34: 页面加载/点[刷新数据]时带pull — 份额源D+1上午发布而库只在16:35
    # 更新链刷新, 回测页末端整天空柱; 落后则主动拉一次(服务端节流)
    nt_pull = BH.pull_latest() if d.get("pull") else None
    r = BH.payload(weights, entry, exit_)
    r["ok"] = True
    if nt_pull is not None:
        r["nt_pull"] = nt_pull
    return jsonify(r)


@bp_admin.get("/api/backtest/strategies")
@require_level(7)
def api_bt_strategies():
    from server.models import BehaviorStrategy
    rows = (BehaviorStrategy.query.order_by(BehaviorStrategy.id.desc()).all())
    return jsonify({"strategies": [
        {"id": s.id, "name": s.name, "config": _bt_cfg(s),
         "updated_at": s.updated_at.strftime("%Y-%m-%d %H:%M") if s.updated_at else ""}
        for s in rows]})


def _bt_cfg(s):
    import json as _j
    try:
        return _j.loads(s.config_json or "{}")
    except ValueError:
        return {}


@bp_admin.post("/api/backtest/strategies")
@require_level(7)
def api_bt_strategy_save():
    """保存/更新策略(同名覆盖): {name, factors, entry, exit}"""
    import json as _j
    from server.models import BehaviorStrategy
    from server import db as sdb
    d = request.get_json(force=True, silent=True) or {}
    name = str(d.get("name", "")).strip()
    if not name or len(name) > 32:
        return jsonify({"ok": False, "msg": "名称需1~32字"}), 400
    weights = {k: v for k, v in (d.get("factors") or {}).items()
               if isinstance(v, (int, float))}
    if not weights:
        return jsonify({"ok": False, "msg": "策略需至少一个因子"}), 400
    try:                        # review: 阈值类型校验(曾可存任意串, 加载即坏)
        entry = int(d.get("entry"))
        exit_ = int(d.get("exit"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "entry/exit应为整数"}), 400
    cfg = {"factors": weights, "entry": entry, "exit": exit_}
    s = BehaviorStrategy.query.filter_by(name=name).first()
    from server.jobs import _now
    if s:
        s.config_json = _j.dumps(cfg, ensure_ascii=False)
        s.updated_at = _now()
        msg = f"策略[{name}]已更新"
    else:
        s = BehaviorStrategy(name=name, config_json=_j.dumps(cfg, ensure_ascii=False),
                             created_at=_now(), updated_at=_now())
        sdb.session.add(s)
        msg = f"策略[{name}]已保存"
    sdb.session.commit()
    audit("bt_strategy_save", target=name)
    return jsonify({"ok": True, "msg": msg, "id": s.id})


@bp_admin.post("/api/backtest/strategies/<int:sid>/delete")
@require_level(7)
def api_bt_strategy_delete(sid):
    from server.models import BehaviorStrategy
    from server import db as sdb
    s = sdb.session.get(BehaviorStrategy, sid)
    if not s:
        return jsonify({"ok": False, "msg": "策略不存在"}), 404
    sdb.session.delete(s)
    sdb.session.commit()
    audit("bt_strategy_delete", target=s.name)
    return jsonify({"ok": True, "msg": f"策略[{s.name}]已删除"})


@bp_admin.get("/api/stock/search")
@require_level(7)
def api_stock_search():
    """代码/名称搜索(基础信息缓存)"""
    import fetch_stock as FS
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"items": []})
    return jsonify({"items": FS.search(q)})


@bp_admin.get("/api/stock/kline")
@require_level(7)
def api_stock_kline():
    """K线载荷(前/后/不复权); 不在池自动入池(自选池语义: 查过即落地)"""
    import fetch_stock as FS
    code = request.args.get("code", "").strip()
    adj = request.args.get("adj", "qfq")
    if adj not in ("qfq", "hfq", "none"):
        adj = "qfq"
    r = FS.kline(code, adj)
    if not r.get("ok"):                       # 不在池 → 自动入池再取
        r2 = FS.add_stock(code)
        if r2.get("ok"):
            r = FS.kline(code, adj)
        else:
            return jsonify({"msg": r2.get("msg", r.get("msg"))}), 404
    return jsonify(r)


@bp_admin.get("/api/stock/pool")
@require_level(7)
def api_stock_pool():
    import fetch_stock as FS
    return jsonify({"pool": FS.pool_list()})


@bp_admin.get("/api/stock/stats_pool")
@require_level(7)
def api_stock_stats_pool():
    """A5数据管理卡: 池内每股各数据集健康度"""
    import fetch_stock as FS
    return jsonify(FS.stats_pool())


@bp_admin.post("/api/stock/full_refresh")
@require_level(7)
def api_stock_full_refresh():
    """A5: 单只全量重拉(异步入队, 全史所有数据集)"""
    from server import jobs as J
    import fetch_stock as FS
    code = (request.get_json(force=True, silent=True) or {}).get("code", "")
    if not FS._norm_code(code):
        return jsonify({"msg": "缺少 code"}), 400
    ok, msg = J.enqueue("stock_full", J.task_stock_full(FS._norm_code(code)),
                        target=f"个股全量重拉({FS._norm_code(code)})", trigger="manual")
    return jsonify({"ok": ok, "msg": msg + " — 已入队, 进度见任务中心"})


@bp_admin.post("/api/stock/nightly")
@require_level(7)
def api_stock_nightly():
    """A5: 手动触发夜间链(日频增量, 异步入队)"""
    from server import jobs as J
    ok, msg = J.enqueue("stock_daily", J.task_stock_daily(False),
                        target="个股数据·日频", trigger="manual")
    return jsonify({"ok": ok, "msg": msg + " — 已入队, 进度见任务中心"})


@bp_admin.post("/api/stock/daily")
@require_level(7)
def api_stock_daily_refresh():
    """池内日增(回看7日幂等) — 同步秒级(分钟档接口无配额风险, 实测45连发OK)"""
    import fetch_stock as FS
    n = FS.refresh_daily()
    return jsonify({"ok": n >= 0, "rows": n, "msg": f"日增 {n} 行"})


# ── 避雷针A2: 财务/股东/两融/估值(首访自动拉取, 自选池语义) ──

_B1_AUTOFETCH_GAP = 6 * 3600          # B1四表自动补拉的负缓存(秒)
_b1_autofetch = {}                   # 次新股/尚未出报表的股票, 不至于每开一次页面重拉一轮全史


def _b1_need_autofetch(ts_code: str, p: dict) -> bool:
    """B1财务全景是否需要自动补拉: 四表全无数据, 或利润表/现金流量表缺失
    (营收/净利/经营现金流图为空)。D1-35: 原只在全无数据时触发 — 而周频夜间链
    和[全量重拉]当时都不含这两张表, 没点过[数据更新]的股票永远不会自动补上。"""
    if p.get("dates"):
        import fetch_stock as FS
        con = FS._conn()
        try:
            inc, cf = con.execute(
                "SELECT (SELECT COUNT(*) FROM stock_income WHERE ts_code=?),"
                " (SELECT COUNT(*) FROM stock_cashflow WHERE ts_code=?)",
                (ts_code, ts_code)).fetchone()
        finally:
            con.close()
        if inc and cf:
            return False
    last = _b1_autofetch.get(ts_code)
    if last and time.time() - last < _B1_AUTOFETCH_GAP:
        return False
    _b1_autofetch[ts_code] = time.time()
    return True


def _stock_module(code, mod):
    """A2/A3模块统一入口: 空数据先refresh再出载荷"""
    import fetch_stock as FS
    ts_code = FS._norm_code(code)
    refresher = {"holders": FS.refresh_holders, "margin": FS.refresh_margin,
                 "val": FS.refresh_val, "finance": FS.refresh_finance,
                 "float": FS.refresh_float, "events": FS.refresh_events}.get(mod)
    payload = {"holders": FS.holders_payload, "margin": FS.margin_payload,
               "profit": FS.profit_payload, "val": FS.val_payload,
               "float": FS.float_payload, "events": FS.events_payload,
               "risk": FS.risk_payload, "full": FS.finance_full_payload}[mod]
    p = payload(ts_code)
    if mod == "profit" and not p.get("dates"):           # profit依赖finance
        FS.refresh_finance(ts_code)
        p = payload(ts_code)
    elif mod in ("holders", "margin", "val", "float", "events") and (
            not p.get("data") and not p.get("dates") and not p.get("n")
            and not p.get("events")):
        refresher(ts_code)
        p = payload(ts_code)
    elif mod == "full" and _b1_need_autofetch(ts_code, p):   # B1全景依赖四表
        for fn in (FS.refresh_income, FS.refresh_cashflow,
                   FS.refresh_finance, FS.refresh_bs):
            try:
                fn(ts_code)
            except Exception as e:  # noqa: BLE001
                print(f"[stock] B1自动补拉失败 {fn.__name__}: {str(e)[:60]}")
        p = payload(ts_code)
    elif mod == "risk" and not p.get("items") and not p.get("checked"):
        FS.refresh_float(ts_code)                        # risk依赖float+events
        FS.refresh_events(ts_code)
        p = payload(ts_code)
    return p


@bp_admin.get("/api/stock/<mod>")
@require_level(7)
def api_stock_module(mod):
    """A2/A3/B1模块载荷: holders|margin|profit|val|float|events|risk|full"""
    if mod not in ("holders", "margin", "profit", "val", "float", "events",
                   "risk", "full"):
        return jsonify({"msg": "mod 非法"}), 400
    return jsonify(_stock_module(request.args.get("code", ""), mod))


@bp_admin.get("/api/stock/finance_ai")
@require_level(7)
def api_stock_finance_ai():
    """A4/B2: 读扫雷结果缓存(无有效缓存返回nocache, 财务不足返回incomplete)。
    只读: D1-35前这里调的是不带cached_only的scan_stock — 页面每次加载一只
    没扫过(或财务数据已更新)的股票, 这个GET就会直接跑一遍AI扫雷: 本地模型
    被静默占用1~3分钟, 且与用户随后点的[一键扫雷]并发成两遍。"""
    import stock_risk as SR
    r = SR.scan_stock(request.args.get("code", ""), cached_only=True)
    if not r.get("ok") and not r.get("cached"):
        return jsonify(r), 404
    return jsonify(r)


@bp_admin.get("/api/stock/freshness")
@require_level(7)
def api_stock_freshness():
    """B2: 各数据集更新状态(供数据更新按钮边上的状态显示)"""
    import stock_risk as SR
    return jsonify({"ok": True, "datasets": SR.data_freshness(request.args.get("code", ""))})


@bp_admin.post("/api/stock/update_all")
@require_level(7)
def api_stock_update_all():
    """B2: 更新全部数据集(扫雷第一步, 同步~10-20秒/股)"""
    import stock_risk as SR
    code = (request.get_json(force=True, silent=True) or {}).get("code", "")
    res = SR.update_all(code)
    return jsonify({"ok": True, "results": res,
                    "datasets": SR.data_freshness(code)})


@bp_admin.post("/api/stock/scan")
@require_level(7)
def api_stock_scan():
    """B2一键扫雷: 全维度AI体检, 报告入库(下次打开可回看)。
    force跳过缓存; 数据不完整仍可继续(仅规则降级)。"""
    import stock_risk as SR
    d = request.get_json(force=True, silent=True) or {}
    r = SR.scan_stock(d.get("code", ""), force=bool(d.get("force")))
    if not r.get("ok") and not r.get("cached"):
        return jsonify(r), 404
    return jsonify(r)


@bp_admin.get("/api/rt/latest")
@require_level(7)
def api_rt_latest():
    from server import rt
    return jsonify(rt.latest())


@bp_admin.get("/api/rt/day")
@require_level(7)
def api_rt_day():
    from server import rt
    d = request.args.get("date", "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return jsonify({"msg": "date 应为 YYYY-MM-DD"}), 400
    payload = rt.day(d)
    if payload is None:
        return jsonify({"msg": f"{d} 无存档(当日未采集或非交易日)"}), 404
    return jsonify(payload)


@bp_admin.get("/api/rt/days")
@require_level(7)
def api_rt_days():
    from server import rt
    return jsonify({"days": rt.days(), "recent": rt.recent_days(5)})


@bp_admin.get("/api/rt/stats")
@require_level(7)
def api_rt_stats():
    """D1-22 R6c: 实时库状态(数据管理卡)"""
    from server import rt
    return jsonify(rt.stats())


@bp_admin.post("/api/rt/backfill")
@require_level(7)
def api_rt_backfill():
    """D1-31: 立即补采入任务队列(曾同步"秒级" — 半日档重建改造后
    rt_series逐分钟情绪回算~10s+阻塞请求, 且任务中心不可见, 用户
    无法确认是否运行; 入队后与更新链串行不并发写库, 双击由去重拦)"""
    from server import jobs as J
    from server import rt

    def fn(append):
        r = rt.backfill_now()
        append({"ok": "已补采/重建当日", "have": "已有健康数据(无需补采)",
                "idle": "当前无可补采目标(盘中由实时采集负责)"}.get(r, f"结果: {r}"))
        return 0
    ok, msg = J.enqueue("rt_backfill", fn, target="实时行情·立即补采",
                        trigger="manual")
    if not ok:
        return jsonify({"ok": False, "msg": msg}), 409
    return jsonify({"ok": True, "msg": "已入队 — 进度与结果见任务中心"})


@bp_admin.get("/api/rt/hist_gap")
@require_level(7)
def api_rt_hist_gap():
    """历史管理: 双源近5日覆盖vs库存 → 缺失日+半日档"""
    from server import rt
    return jsonify(rt.hist_gap_map())


@bp_admin.post("/api/rt/backfill_hist")
@require_level(7)
def api_rt_backfill_hist():
    """历史管理: 一键回补缺失日+修复半日档(分钟重建+情绪, 异步入队)"""
    from server import jobs as J
    from server import rt

    def fn(append):
        r = rt.backfill_hist()
        append(f"历史回补: 补{len(r['filled'])}日 {r['filled']}, "
               f"修{len(r.get('repaired', []))}日 {r.get('repaired', [])}, "
               f"跳过{r.get('skipped', 0)}")
        return 0
    ok, msg = J.enqueue("rt_hist", fn, target="实时历史回补", trigger="manual")
    return jsonify({"ok": ok, "msg": msg + " — 已入队, 进度见任务中心"})


@bp_admin.post("/api/rt/settings")
@require_level(7)
def api_rt_settings():
    from server import jobs, rt
    d = request.get_json(force=True, silent=True) or {}
    if "enabled" not in d:
        return jsonify({"msg": "缺少 enabled"}), 400
    jobs.set_setting("rt_enabled", "1" if d["enabled"] else "0")
    return jsonify({"ok": True,
                    "msg": f"实时采集已{'开' if d['enabled'] else '关'}(交易时段生效)"})


# ── D1-18 M1/M2: 宏观舆情事件库页与API ──

@bp_admin.get("/api/ai/config")
@require_level(7)
def api_ai_config():
    """D1-28 N2: AI配置读取"""
    from server import jobs as J
    return jsonify({
        "local": {"enabled": J.get_setting("ai_local_enabled", "0") == "1",
                  "base_url": J.get_setting("ai_local_base_url", "http://localhost:11434/v1"),
                  "model": J.get_setting("ai_local_model", ""),
                  "has_key": bool(J.get_setting("ai_local_api_key", ""))},
        "cloud": {"enabled": J.get_setting("ai_cloud_enabled", "0") == "1",
                  "base_url": J.get_setting("ai_cloud_base_url", ""),
                  "model": J.get_setting("ai_cloud_model", ""),
                  "has_key": bool(J.get_setting("ai_cloud_api_key", ""))}})


@bp_admin.post("/api/ai/config")
@require_level(7)
def api_ai_config_save():
    """D1-28 N2: AI配置保存(仅传非空字段)"""
    from server import jobs as J
    d = request.get_json(force=True, silent=True) or {}
    for prefix in ("local", "cloud"):
        cfg = d.get(prefix) or {}
        for k in ("enabled", "base_url", "model"):
            if k in cfg:
                v = cfg[k]
                J.set_setting(f"ai_{prefix}_{k}",
                              ("1" if v else "0") if k == "enabled" else str(v))
        if "api_key" in cfg and cfg["api_key"]:
            J.set_setting(f"ai_{prefix}_api_key", str(cfg["api_key"]))
    return jsonify({"ok": True, "msg": "AI配置已保存"})


@bp_admin.get("/api/ai/progress")
@require_level(7)
def api_ai_progress():
    """D1-28 N2: 分析进度"""
    from server import macro
    import sys as _s
    _s.path.insert(0, str(macro.CORE))
    import analyze_news as AN
    return jsonify(AN.progress())


@bp_admin.post("/api/ai/analyze")
@require_level(7)
def api_ai_analyze():
    """D1-28 N2: 手动触发分析批次(异步入队)"""
    from server import jobs as J
    d = request.get_json(force=True, silent=True) or {}
    batch = min(int(d.get("batch", 10)), 50)
    ok, msg = J.enqueue("analyze", J.task_analyze_news(batch),
                        target=f"AI分析×{batch}", trigger="manual")
    return jsonify({"ok": ok, "msg": msg + " — 已入队, 进度见任务中心"})


@bp_admin.get("/api/ai/summary")
@require_level(7)
def api_ai_summary():
    """D1-28 N4: AI分析总览 — 进度+等级计数(AI/手工分列)"""
    from server import macro as _m
    import sys as _s
    _s.path.insert(0, str(_m.CORE))
    import analyze_news as AN
    prog = AN.progress()
    prog["levels"] = AN.level_counts()
    return jsonify(prog)


@bp_admin.post("/api/ai/analyze-all")
@require_level(7)
def api_ai_analyze_all():
    """D1-28 N4 + 批量控制: 一键分析(最新优先, 本次≤limit条, 上限1000;
    本地→云端L3+, 异步入队)。历史积压再多也不会饿死新新闻(pub_time DESC)。"""
    from server import jobs as J
    if J.get_setting("ai_local_enabled", "0") != "1":
        return jsonify({"msg": "本地AI未启用, 请先配置"}), 400
    d = request.get_json(force=True, silent=True) or {}
    try:
        limit = max(1, min(int(d.get("limit", 1000)), 1000))
    except (TypeError, ValueError):
        limit = 1000

    state = {"local": 0}                # 跨片累计(让道续排后仍保持"本次上限"口径)
    target = f"AI分析(≤{limit}条)"

    def run_slice(append, budget):
        """D1-33: 分片执行, 队列有等待任务时让道。实测单条本地分析约19s,
        1000条连续占住串行队列5.3小时(11-18: 07:00晨报排到11:10才生成)。
        每批(≤20条, 约6分钟)后检查: 队列里有别的任务在等 → 把剩余额度
        重新排到队尾, 让数据更新/生成/备份先走。"""
        import analyze_news as AN
        done = 0
        while done < budget:
            r = AN.analyze_batch(min(20, budget - done))
            n = r.get("analyzed", 0)
            done += n
            state["local"] += n
            append(f"  本地: +{n} (累计{state['local']}, 待{r.get('pending', 0)})")
            if n == 0:                        # 无待分析/已暂停/连续失败
                break
            rest = budget - done
            waiting = J.queue_size()
            if rest > 0 and waiting > 0:
                ok, why = J.enqueue("analyze_all", lambda a, _rest=rest: run_slice(a, _rest),
                                    target=target, trigger="manual", _force=True)
                if ok:
                    append(f"  ⇢ 让道: 队列有 {waiting} 项等待, 剩余≤{rest}条"
                           f"已排到队尾(先执行等待中的任务)")
                    return 0
                # 续排失败(队列满): 不能静默丢掉剩余额度 — 写明后正常收尾
                append(f"  ✗ 续排失败({why}): 剩余≤{rest}条未分析, 请稍后重新发起")
                break
        if state["local"] >= limit:
            append(f"  ⚠ 达本次上限{limit}条, 剩余待下轮再点")
        r2 = AN.analyze_cloud_batch(20)
        total_cloud = r2.get("analyzed", 0)
        append(f"  云端批次: +{total_cloud}")
        append(f"分析结束: 本次本地{state['local']} 云端{total_cloud}")
        return 0

    def fn(append):
        append(f"分析开始(最新优先, 本次≤{limit}条; 本地→云端)")
        return run_slice(append, limit)
    ok, msg = J.enqueue("analyze_all", fn, target=target, trigger="manual")
    return jsonify({"ok": ok, "msg": msg + f" — 已入队(最新优先≤{limit}条)"})


@bp_admin.post("/api/ai/analyze-cloud")
@require_level(7)
def api_ai_analyze_cloud():
    """D1-28 N3: 手动触发云端二次分析(L3+, 异步入队)"""
    from server import jobs as J
    if J.get_setting("ai_cloud_enabled", "0") != "1":
        return jsonify({"msg": "云端AI未启用, 请先配置并开启"}), 400
    d = request.get_json(force=True, silent=True) or {}
    batch = min(int(d.get("batch", 5)), 20)
    ok, msg = J.enqueue("analyze_cloud", J.task_analyze_cloud(batch),
                        target=f"云端AI×{batch}", trigger="manual")
    return jsonify({"ok": ok, "msg": msg + " — 已入队(仅L3+控成本)"})


@bp_admin.post("/api/briefing/run")
@require_level(7)
def api_briefing_run():
    """D1-29: 手动触发数据简报(异步入队)"""
    from server import jobs
    ok, msg = jobs.enqueue("generate", jobs.task_briefing("manual"),
                           target="数据简报", trigger="manual")
    return jsonify({"ok": ok, "msg": msg + " — 简报生成已入队, 进度见任务中心"})


@bp_admin.get("/macro")
@require_level(7)
def macro_page():
    from server import macro
    # M1r: 等级名单点维护 — 前端标签经 tojson 注入, 不在JS里二次硬编码
    return render_template("admin/macro.html", levels=macro.LEVELS,
                           lv_json=json.dumps(
                               {str(lv): name for lv, (name, _d) in macro.LEVELS.items()},
                               ensure_ascii=False))


@bp_admin.get("/api/macro/events")
@require_level(7)
def api_macro_events():
    """D1-30a: ≥N等级/分页/类型下拉数据"""
    from server import macro
    r = macro.list_events(
        level=request.args.get("level", "").strip() or None,
        level_min=request.args.get("level_min", "").strip() or None,
        d_from=request.args.get("from", "").strip() or None,
        d_to=request.args.get("to", "").strip() or None,
        type_=request.args.get("type", "").strip() or None,
        q=request.args.get("q", "").strip() or None,
        limit=request.args.get("limit", "30"),
        page=request.args.get("page", "1"))
    # 类型下拉(DISTINCT)
    con = macro._conn()
    try:
        types = [r2[0] for r2 in con.execute(
            "SELECT DISTINCT type FROM macro_events WHERE type IS NOT NULL AND type != '' ORDER BY 1")]
    except Exception:
        types = []
    finally:
        con.close()
    r["types"] = types
    r["stats"] = macro.stats()
    return jsonify(r)


@bp_admin.get("/api/macro/news/running")
@require_level(7)
def api_macro_news_running():
    """D1-30b: 新闻抓取/AI分析运行态"""
    from server.models import Job
    running = {}
    for j in Job.query.filter_by(status="running").all():
        running.setdefault(j.kind, []).append(j.target or "")
    return jsonify({"running": running})


@bp_admin.post("/api/ai/pause")
@require_level(7)
def api_ai_pause():
    """D1-30c: 停止分析(置ai_pause标记, 分析循环检查后break)"""
    from server import jobs as J
    d = request.get_json(force=True, silent=True) or {}
    pause = bool(d.get("pause", True))
    J.set_setting("ai_pause", "1" if pause else "0")
    return jsonify({"ok": True,
                    "msg": "已暂停 — 当前批次完成后停止" if pause else "已恢复"})


@bp_admin.post("/api/macro/events")
@require_level(7)
def api_macro_add():
    from server import macro
    d = request.get_json(force=True, silent=True) or {}
    try:
        eid = macro.add_event(d)
    except ValueError as e:
        return jsonify({"msg": str(e)}), 400
    return jsonify({"ok": True, "id": eid, "msg": "已新增"})


@bp_admin.delete("/api/macro/events/<int:eid>")
@require_level(7)
def api_macro_del(eid):
    from server import macro
    if not macro.delete_event(eid):
        return jsonify({"msg": f"id {eid} 不存在"}), 404
    return jsonify({"ok": True, "msg": "已删除"})


@bp_admin.put("/api/macro/events/<int:eid>")
@require_level(7)
def api_macro_update(eid):
    """宏观打磨: 事件编辑(level/type/date/content/source, title只读保AI关联)"""
    from server import macro
    d = request.get_json(force=True, silent=True) or {}
    try:
        if not macro.update_event(eid, d):
            return jsonify({"msg": f"id {eid} 不存在"}), 404
    except ValueError as e:
        return jsonify({"msg": str(e)}), 400
    return jsonify({"ok": True, "msg": "已保存"})


@bp_admin.post("/api/macro/events/bulk-delete")
@require_level(7)
def api_macro_bulk_del():
    """宏观打磨: 按等级批量清理(≤level_max, 可选日期范围; L5禁批量)"""
    from server import macro
    d = request.get_json(force=True, silent=True) or {}
    try:
        n = macro.delete_events(d.get("level_max"), d.get("from") or None,
                                d.get("to") or None)
    except ValueError as e:
        return jsonify({"msg": str(e)}), 400
    return jsonify({"ok": True, "deleted": n, "msg": f"已清理 {n} 条"})


@bp_admin.get("/api/macro/events/export")
@require_level(7)
def api_macro_export():
    """宏观打磨: 检索结果导出CSV(同列表筛选条件; BOM头兼容Excel中文)"""
    import csv
    import io
    from flask import Response
    from server import macro
    r = macro.list_events(
        level=request.args.get("level", "").strip() or None,
        level_min=request.args.get("level_min", "").strip() or None,
        d_from=request.args.get("from", "").strip() or None,
        d_to=request.args.get("to", "").strip() or None,
        type_=request.args.get("type", "").strip() or None,
        q=request.args.get("q", "").strip() or None,
        limit="50000", page="1")
    si = io.StringIO()
    si.write("﻿")                     # Excel中文兼容(BOM头)

    def _csv_safe(v):                     # 公式注入防护: =+-@开头补'前缀
        v = str(v or "")
        return "'" + v if v[:1] in ("=", "+", "-", "@") else v

    w = csv.writer(si)
    w.writerow(["日期", "等级", "类型", "标题", "内容", "来源",
                "AI等级", "AI概要", "AI方向", "AI详析", "更新时间"])
    for e in r["events"]:
        w.writerow([_csv_safe(e["date"]), f"L{e['level']}", _csv_safe(e.get("type")),
                    _csv_safe(e["title"]), _csv_safe(e.get("content")),
                    _csv_safe(e.get("source")),
                    f"L{e['ai_level']}" if e.get("ai_level") else "",
                    _csv_safe(e.get("ai_impact")), _csv_safe(e.get("ai_direction")),
                    _csv_safe(e.get("ai_reasoning")), _csv_safe(e.get("updated_at"))])
    fname = f"macro_events_{datetime.now():%Y%m%d_%H%M}.csv"
    return Response(si.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@bp_admin.get("/api/macro/buffett")
@require_level(7)
def api_macro_buffett():
    """D1-18 M4: 巴菲特指标(总市值/GDP)序列+最新分位"""
    from server import macro
    return jsonify(macro.buffett())


@bp_admin.get("/api/macro/float")
@require_level(7)
def api_macro_float():
    """D1-20 F2: 月度解禁(历史前收口径+当月两段+未来预判)"""
    from server import macro
    hist = request.args.get("hist", "36")
    future = request.args.get("future", "3")
    if not (hist.isdigit() and future.isdigit()):
        return jsonify({"msg": "hist/future 应为数字"}), 400
    return jsonify(macro.float_overview(int(hist), int(future)))


@bp_admin.get("/api/macro/news/stats")
@require_level(7)
def api_macro_news_stats():
    """D1-28 N1: 新闻抓取同步区间图(每源覆盖/起止/行数)"""
    import fetch_news as FN
    import sqlite3 as sq
    from server import macro
    p = macro.CORE / "data" / "macro.db"
    if not p.exists():
        return jsonify({"sources": {}, "total": 0})
    con = sq.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        total = con.execute("SELECT COUNT(*) FROM news_raw").fetchone()[0]
    except sq.Error:
        return jsonify({"sources": {}, "total": 0})
    finally:
        con.close()
    return jsonify({"sources": FN.sync_map(), "total": total})


@bp_admin.post("/api/macro/news/fetch")
@require_level(7)
def api_macro_news_fetch():
    """D1-28 N1: 抓取控制 — mode=backfill(全史回补)/incremental(近3日+em)。
    D1-31: incremental亦入队 — 曾同步'秒级', 实际含tushare限频重试+三路
    快讯+渐进回补10日, 阻塞请求分钟级且与更新链并发写macro.db"""
    from server import jobs
    d = request.get_json(force=True, silent=True) or {}
    mode = d.get("mode", "incremental")
    if mode not in ("backfill", "incremental"):
        return jsonify({"msg": "mode 应为 backfill|incremental"}), 400
    if mode == "backfill":
        start = str(d.get("start") or "20180601")
        ok, msg = jobs.enqueue("backfill", jobs.task_backfill("news_fetch", start),
                               target=f"宏观新闻回补(自{start})", trigger="manual")
        return jsonify({"ok": ok, "msg": msg + " — 全史回补已入队(2018海量日较慢), 进度见任务中心"})
    def fn(append):
        import fetch_news as FN
        FN.refresh()            # 近3日major_news(含content) + em/新浪/同花顺快讯 + 渐进回补10日
        append("增量拉取完成: 近3日major_news + 三路快讯 + 渐进回补10日")
        return 0
    ok, msg = jobs.enqueue("news_inc", fn, target="宏观新闻·增量", trigger="manual")
    return jsonify({"ok": bool(ok), "msg": msg + (" — 进度与结果见任务中心" if ok else "")}), \
        (200 if ok else 409)


@bp_admin.post("/api/macro/import")
@require_level(7)
def api_macro_import():
    """D1-18 补充需求③: 外部工具/第三方数据批量导入通道"""
    from server import macro
    d = request.get_json(force=True, silent=True) or {}
    events = d.get("events")
    if events is None:
        return jsonify({"msg": "缺少 events 数组"}), 400
    r = macro.import_events(events)
    return jsonify({**r, "ok": not r["errors"] or r["inserted"] > 0,
                    "msg": f"新增 {r['inserted']} · 跳过重复 {r['skipped']}"
                           + (f" · 错误 {len(r['errors'])} 条" if r["errors"] else "")})


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
               "log": (cur.log or "")[-4000:],
               "progress": cur.progress}          # D1-10 U2: 0~100
    # D1-10 U2b: 每资产最新日期 vs 今日(数据集"已更新至X"徽标, 轻查询)
    fresh = []
    try:
        from server import market
        con = market._conn()
        if con is not None:
            today = datetime.now().strftime("%Y-%m-%d")
            for code, _n, table, where, *_r in (market._ASSET_DATE_TABLES):
                try:
                    cond = f" WHERE {where}" if where else ""
                    last = con.execute(
                        f"SELECT MAX(date) FROM {table}{cond}").fetchone()[0]
                    fresh.append({"code": code, "last": last,
                                  "ok": last == today})
                except Exception:  # noqa: BLE001
                    pass
            con.close()
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"running": run, "queued": jobs.queue_size(),
                    "scheduler": "运行中" if jobs.scheduler_running() else "未启用",
                    "fresh": fresh,
                    "recent": [{"id": j.id, "kind": j.kind, "target": j.target or "",
                                "status": j.status, "rc": j.rc, "duration": j.duration_s,
                                "progress": j.progress,
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
    d = request.get_json(force=True, silent=True) or {}
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

# D1-11 C1: 数据块口径(展示层) 与 类型徽章
BACKUP_BLOCKS = {"sentiment": "基础+评分", "raw_daily": "基础",
                 "realtime": "实时行情",        # D1-17 R3: 归基础块
                 "macro": "宏观事件",           # D1-18 M1: 归基础块
                 "app": "账号", "secret": "配置", "token": "配置",
                 "cfg_core": "配置", "cfg_server": "配置"}
TYPE_LABELS = {"daily": "日备", "weekly": "周备", "manual": "手动",
               "pre-calc": "计算前", "pre-backfill": "回填前",
               "pre-restore": "恢复前", "pre": "保护"}
# D1-11 C3: 备份范围块白名单(API只收块名, 不收任意资产名)
BLOCK_ASSETS = {"base": ("sentiment", "raw_daily", "realtime", "macro"),
                "account": ("app", "secret", "token", "cfg_core", "cfg_server")}


def _fresh_label(e: dict) -> str:
    """备份内数据新鲜度(冗长stats → 一行可读)"""
    s = e.get("stats") or {}
    if e.get("asset") == "sentiment":
        d = s.get("sector_scores") or s.get("sentiment")
        return f"评分 {d}" if d else "-"
    if e.get("asset") == "app":
        return f"{s.get('users', '?')} 用户" if s.get("users") is not None else "-"
    return "-"


@bp_admin.get("/backups")
@require_level(7)
def backups_page():
    """D1-11 C1: 备份中心 — manifest为真源的备份清单/校验状态/操作记录"""
    return render_template("admin/backups.html")


@bp_admin.get("/api/backups/manifest")
@require_level(7)
def api_backups_manifest():
    """D1-11 C1: manifest按运行(zip/目录)分组的备份清单 + 汇总"""
    bk = _backup_mod()
    m = bk._load_manifest()
    runs = {}
    for e in m["entries"]:
        folder = e["file"].split("/")[0] if "/" in e["file"] else "(旧平铺)"
        v = e.get("verify") or {}
        member = {
            "file": e["file"], "asset": e.get("asset", "?"),
            "block": BACKUP_BLOCKS.get(e.get("asset", ""), "-"),
            "size_mb": round(e.get("size", 0) / 1048576, 1),
            "fresh": _fresh_label(e), "kind": e.get("kind", "file"),
            "exists": bk._entry_exists(e["file"]),
            "verify": {"ok": v.get("ok"), "at": v.get("at", ""),
                       "issues": v.get("issues", [])} if v else None,
        }
        if folder not in runs:
            runs[folder] = {"folder": folder, "type": e.get("type", ""),
                            "type_label": TYPE_LABELS.get(e.get("type", ""), e.get("type", "")),
                            "time": e.get("time", ""), "members": []}
        runs[folder]["members"].append(member)
    run_list = []
    for r in runs.values():
        mem = r.pop("members")
        r.update({
            "files_n": len(mem), "total_mb": round(sum(x["size_mb"] for x in mem), 1),
            "fresh": next((x["fresh"] for x in mem if x["fresh"] != "-"), "-"),
            "n_ok": sum(1 for x in mem if x["verify"] and x["verify"]["ok"]),
            "n_bad": sum(1 for x in mem if x["verify"] and not x["verify"]["ok"]
                         or not x["exists"]),
            "members": mem,
        })
        run_list.append(r)
    run_list.sort(key=lambda r: r["time"], reverse=True)
    n_all = sum(r["files_n"] for r in run_list)
    return jsonify({
        "runs": run_list,
        "summary": {"files_n": n_all,
                    "disk_mb": round(sum(e.get("size", 0) for e in m["entries"]) / 1048576, 1),
                    "runs_n": len(run_list),
                    "by_type": {TYPE_LABELS.get(t, t): sum(1 for e in m["entries"]
                                                           if e.get("type") == t)
                                for t in {e.get("type", "") for e in m["entries"]}},
                    "n_bad": sum(r["n_bad"] for r in run_list)},
    })


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
    d = request.get_json(force=True, silent=True) or {}
    btype = str(d.get("type", "manual"))
    if btype not in _BACKUP_TYPES:
        return jsonify({"ok": False, "msg": f"type 应为 {'/'.join(_BACKUP_TYPES)}"}), 400
    # D1-11 C3: blocks 范围白名单(base=基础+评分 / account=账号配置), 缺省=全部;
    # 与本环境实际资产表求交(测试/精简环境资产集可不同)
    blocks = d.get("blocks") or []
    if not isinstance(blocks, list) or any(b not in BLOCK_ASSETS for b in blocks):
        return jsonify({"ok": False,
                        "msg": "blocks 应为 ['base','account'] 的子集"}), 400
    bk = _backup_mod()
    assets = (sorted({a for b in blocks for a in BLOCK_ASSETS[b] if a in bk.ASSETS})
              if blocks else None)
    if blocks and not assets:
        return jsonify({"ok": False, "msg": "所选范围在本环境无可用资产"}), 400
    ok, msg = jobs.enqueue("backup", jobs.task_backup(btype, "manual", assets=assets),
                           target=btype + (f"·{'+'.join(blocks)}" if blocks else ""),
                           trigger="manual")
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
    d = request.get_json(force=True, silent=True) or {}
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
    """D1-11 C2: 校验入队(异步job) — 全量校验需数分钟, 同步等待必超时;
    结果由 verify 逐条写回 manifest, 页面轮询任务结束后刷新"""
    from server import jobs
    d = request.get_json(force=True, silent=True) or {}   # 空 body 视为全部(页面按钮不带body)
    target = str(d.get("file", "")).strip() or "--all"
    bk = _backup_mod()
    if target != "--all":
        if not any(e["file"] == target or e["file"].startswith(target + "/")
                   for e in bk._load_manifest()["entries"]):
            return jsonify({"ok": False, "msg": "备份不存在"}), 404
    ok, msg = jobs.enqueue("verify", jobs.task_verify(target),
                           target="全部备份" if target == "--all" else target,
                           trigger="manual")
    audit("backup_verify_enqueue" if ok else "backup_verify_reject", target=target)
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 409)


@bp_admin.post("/api/backup/restore")
@require_level(9)
def api_backup_restore():
    from server import jobs
    d = request.get_json(force=True, silent=True) or {}
    fname = str(d.get("file", "")).strip()
    # D1-10 B1: 合法名 = 平铺文件名 或 {dir}/{file}(zip/子目录布局, 仅一层)
    import re as _re
    if (not fname or "\\" in fname or ".." in fname or fname.startswith("/")
            or not _re.match(r"^[\w.\-]+(?:/[\w.\-]+)?$", fname)):
        return jsonify({"ok": False, "msg": "文件名非法"}), 400
    bk = _backup_mod()
    m = bk._load_manifest()
    e = next((x for x in m["entries"] if x["file"] == fname), None)
    if not e:
        return jsonify({"ok": False, "msg": "备份不存在"}), 404
    if e["asset"] == "app":
        return jsonify({"ok": False,
                        "msg": f"app.db 为服务自身数据库(账号/设置), 在线恢复已拒绝。"
                               f"请停服后在仓库根目录执行: "
                               f"cd core && python backup.py restore \"{fname}\" --yes"}), 409
    # D1-31: 入队执行 — 曾同步阻塞请求2~5分钟(raw_daily 2.5GB校验+快照+拷贝),
    # 且忙时直接拒; 入队后与更新链串行(排队等待而非拒绝), 重复点击由去重拦
    def fn(append):
        ok = _backup_mod().restore(fname, yes=True)   # 内含校验+pre-restore快照
        append("恢复完成(恢复前已自动快照 pre-restore)" if ok
               else "恢复失败, 详见 backups/backup.log")
        return 0 if ok else 1
    ok, msg = jobs.enqueue("restore", fn, target=f"恢复·{e['asset']}",
                           trigger="manual")
    audit("backup_restore_enqueue" if ok else "backup_restore_reject", target=fname)
    return jsonify({"ok": bool(ok), "msg": msg + (" — 已入队(校验+快照+恢复), 进度见任务中心" if ok else "")}), \
        (200 if ok else 409)


# ---------------- D1-10 U1: 缺口扫描/修复 ----------------

@bp_admin.post("/api/scan")
@require_level(7)
def api_scan():
    from server import market
    d = request.get_json(force=True, silent=True) or {}
    win = int(d.get("window", 7))
    if win not in (7, 30, 90, 365):
        return jsonify({"ok": False, "msg": "window 应为 7/30/90/365"}), 400
    r = market.scan_gaps(win)
    r["ok"] = True
    return jsonify(r)


@bp_admin.post("/api/repair")
@require_level(7)
def api_repair():
    from server import jobs
    d = request.get_json(force=True, silent=True) or {}
    win = int(d.get("window", 7))
    if win not in (7, 30, 90, 365):
        return jsonify({"ok": False, "msg": "window 应为 7/30/90/365"}), 400
    ok, msg = jobs.enqueue("repair", jobs.task_repair(win, "manual"),
                           target=f"近{win}天", trigger="manual")
    audit("gap_repair_enqueue" if ok else "gap_repair_reject",
          target=f"{win}d", detail=msg)
    return jsonify({"ok": bool(ok), "msg": msg}), (200 if ok else 409)


# ---------------- D1-2/D1-3: 数据补全 ----------------

_START_RE = re.compile(r"^\d{8}$|^\d{4}-\d{2}-\d{2}$")


@bp_admin.post("/api/backfill/run")
@require_level(7)
def api_backfill_run():
    from server import jobs
    d = request.get_json(force=True, silent=True) or {}
    dataset = str(d.get("dataset", "")).strip()
    start = str(d.get("start", "")).strip() or jobs.BACKFILL_DEFAULT_START.get(dataset, "")
    if dataset not in jobs.BACKFILL_DATASETS:
        return jsonify({"ok": False, "msg": f"dataset 应为 {'/'.join(jobs.BACKFILL_DATASETS)}"}), 400
    if not _START_RE.match(start):
        return jsonify({"ok": False, "msg": "start 应为 YYYYMMDD 或 YYYY-MM-DD"}), 400
    norecalc = bool(d.get("norecalc"))       # D1-13r: 批处理补全跳过逐项重算(收尾链统一算)
    ok, msg = jobs.enqueue("backfill", jobs.task_backfill(dataset, start, "manual",
                                                          norecalc=norecalc),
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
                    "scanned": r.scanned_n,               # D1-19 P4: 扫描日数
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
    d = request.get_json(force=True, silent=True) or {}
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
    d = request.get_json(force=True, silent=True) or {}
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
    d = request.get_json(force=True, silent=True) or {}
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
