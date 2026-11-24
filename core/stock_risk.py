# -*- coding: utf-8 -*-
"""避雷针A4+B2: 一键扫雷 — 混合模式(规则引擎锚定 + AI四维度定性)

N1(计划review): 纯AI评级会漂移 → 评级锚定规则分(A/B/C/D), AI提供风险点叙述;
N2: 风险点必须带数据依据; 依据中的数值必须能在喂给AI的prompt原文中找到
(幻觉校验, D1-35起按prompt核对);
N3: 手动触发(页面[一键扫雷]), 不进夜间链; 缓存按(ts_code,end_date,ann_date,
口径版本)失效(更正版财报ann_date变化、prompt组装方式变更都触发重新分析);
次新股不足季数降级。
免责: AI分析为参考信号, 非投资建议。
"""
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def _conn(path=None):
    import fetch_stock as FS
    return FS._conn(path)


def finance_metrics(code: str, n_qua: int = 8, path=None) -> dict:
    """近N季指标载荷: fina指标 + 商誉/净资产(自算)"""
    import fetch_stock as FS
    ts_code = FS._norm_code(code)
    con = _conn(path)
    fin = con.execute(
        "SELECT end_date, ann_date, profit_dedt, netprofit_yoy, or_yoy, "
        "grossprofit_margin, roe, debt_to_assets FROM stock_finance "
        "WHERE ts_code=? AND profit_dedt IS NOT NULL ORDER BY end_date DESC LIMIT ?",
        (ts_code, n_qua)).fetchall()
    bs = {r[0]: r for r in con.execute(
        "SELECT end_date, goodwill, total_assets, total_liab FROM stock_bs "
        "WHERE ts_code=?", (ts_code,))}
    name = con.execute("SELECT name FROM stock_pool WHERE ts_code=?",
                       (ts_code,)).fetchone()
    con.close()
    rows = []
    for end_date, ann, dedt, n_yoy, o_yoy, gpm, roe, dta in reversed(fin):
        b = bs.get(end_date)
        gw_ratio = None
        if b and b[1] and b[2] and b[3]:
            eq = b[2] - b[3]                       # 净资产=总资产-总负债
            gw_ratio = round(b[1] / eq * 100, 1) if eq > 0 else None
        rows.append({"end_date": end_date, "ann_date": ann,
                     "dedt_yi": round(dedt / 1e8, 2) if dedt is not None else None,
                     "netprofit_yoy": n_yoy, "or_yoy": o_yoy,
                     "gpm": gpm, "roe": roe, "debt_to_assets": dta,
                     "goodwill_eq": gw_ratio})
    return {"code": ts_code, "name": name[0] if name else ts_code,
            "n": len(rows), "rows": rows}


def run_rules(metrics: dict) -> dict:
    """规则引擎(可解释硬规则扣分 → 评级锚定): 每条命中-10(高危)-5(关注)"""
    rows = metrics["rows"]
    hits = []

    def _last(field):
        return rows[-1].get(field) if rows else None

    def _seq(field):
        return [r.get(field) for r in rows]

    dedt = _last("dedt_yi")
    if dedt is not None and dedt < 0:
        hits.append({"rule": "最新报告期扣非净利润为负", "level": "高", "score": -10})
    n_yoy = _seq("netprofit_yoy")
    if len(n_yoy) >= 3 and n_yoy[-1] is not None and n_yoy[-2] is not None \
            and n_yoy[-3] is not None and n_yoy[-1] < 0 and n_yoy[-2] < 0:
        hits.append({"rule": f"扣非净利同比连续为负({n_yoy[-2]:.2f}%,{n_yoy[-1]:.2f}%)",
                     "level": "高", "score": -10})
    elif len(n_yoy) >= 2 and n_yoy[-1] is not None and n_yoy[-2] is not None \
            and n_yoy[-2] > 0 and n_yoy[-1] < 0:
        hits.append({"rule": f"扣非净利同比转负({n_yoy[-2]:.2f}%→{n_yoy[-1]:.2f}%)",
                     "level": "高", "score": -10})
    dta = _last("debt_to_assets")
    if dta is not None and dta > 70:
        hits.append({"rule": f"资产负债率{dta:.1f}%>70%", "level": "中", "score": -5})
    gpm = _seq("gpm")
    if len(gpm) >= 4 and all(v is not None for v in gpm[-4:]) \
            and gpm[-1] < gpm[-2] < gpm[-3] < gpm[-4]:
        hits.append({"rule": "毛利率连续3期下滑", "level": "中", "score": -5})
    o_yoy = _seq("or_yoy")
    if len(o_yoy) >= 2 and o_yoy[-1] is not None and o_yoy[-2] is not None \
            and o_yoy[-1] < 0 and o_yoy[-2] < 0:
        hits.append({"rule": f"营收同比连续为负({o_yoy[-2]:.2f}%,{o_yoy[-1]:.2f}%)",
                     "level": "中", "score": -5})
    gw = _last("goodwill_eq")
    if gw is not None and gw > 30:
        hits.append({"rule": f"商誉占净资产{gw:.1f}%>30% — 减值爆雷风险",
                     "level": "高", "score": -10})
    roe = _last("roe")
    if roe is not None and roe < 0:
        hits.append({"rule": f"ROE为负({roe:.2f}%)", "level": "高", "score": -10})
    score = sum(h["score"] for h in hits)
    grade = "A" if score == 0 else ("B" if score >= -5 else
                                    ("C" if score >= -15 else "D"))
    return {"score": score, "grade": grade, "hits": hits}


KNOWN_METRICS = {"dedt_yi": "扣非净利润(亿)", "netprofit_yoy": "扣非净利同比%",
                 "or_yoy": "营收同比%", "gpm": "毛利率%", "roe": "ROE%",
                 "debt_to_assets": "资产负债率%", "goodwill_eq": "商誉占净资产%"}
# 幻觉校验用指标基名(中文去单位后缀 — AI依据写"扣非净利同比=-45.2%"应命中)
_KNOWN_BASE = [k.split("%")[0].split("(")[0]
               for k in list(KNOWN_METRICS) + list(KNOWN_METRICS.values())]


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _key_nums(text: str) -> list:
    """有判别力的数值: 带小数点的, 或≥4位且不是年份的整数。
    (年份、季数、小整数在依据里到处都是, 匹配上也不说明什么)"""
    out = []
    for tok in _NUM_RE.findall(text or ""):
        if "." in tok:
            out.append(float(tok))
        elif len(tok) >= 4 and not tok.startswith(("19", "20")):
            out.append(float(tok))
    return out


def _grounded(ev: str, prompt_nums: list) -> bool:
    """依据中的数值至少有一个能在prompt原文里找到。
    容差=相对0.1%或绝对±0.0055取大: AI常把数值四舍五入到两位小数(97.4974→97.50,
    -0.2465→-0.25); 相对容差不能放宽到0.5% — 小数值附近会放过编造(3.2 vs 3.21)"""
    return any(any(abs(e - p) <= max(0.001 * max(e, p), 0.0055) for p in prompt_nums)
               for e in _key_nums(ev))


def _validate_ai_points(ai: dict, metrics: dict, prompt: str = "") -> dict:
    """N2幻觉校验: 风险点的evidence须有据可依。

    传prompt时(扫雷主流程): AI拿到的全部数据都在prompt里, 依据中的关键数值
    必须能在prompt原文找到(找不到=凭空编造); 不含关键数值的定性依据退回
    指标名判断。D1-35: 原只认财务指标名/财务行数值 — 筹码/估值/事件三个
    维度的依据本就不含这些, 实测65条风险点里40条被标"依据存疑"(筹码/估值/
    事件几乎全中), 标记失去意义。
    不传prompt: 保持原口径(财务指标名或财务行的精确数值)。"""
    rows = metrics["rows"]
    pnums = _key_nums(prompt) if prompt else None
    for p in (ai or {}).get("points", []):
        ev = str(p.get("evidence", ""))
        has_term = any(k in ev for k in _KNOWN_BASE)
        if pnums is None:
            p["suspect"] = not has_term and \
                not any(str(v) in ev for r in rows for v in r.values() if v is not None)
        elif _key_nums(ev):
            p["suspect"] = not _grounded(ev, pnums)
        else:
            p["suspect"] = not has_term
    return ai


def data_freshness(code: str, path=None) -> dict:
    """B2: 各数据集更新状态(供'数据更新'按钮展示; 不拉取只检查)
    季频表(财务/资产负债/利润/现金流/股东)看月份级新鲜, 日频看7日"""
    import fetch_stock as FS
    import datetime as dt
    ts_code = FS._norm_code(code)
    month_ago = (dt.date.today() - dt.timedelta(days=120)).strftime("%Y-%m")
    week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    con = _conn(path)

    def q(sql):
        return con.execute(sql, (ts_code,)).fetchone()

    qmap = {
        "日线": q("SELECT MAX(trade_date) FROM stock_daily WHERE ts_code=?"),
        "两融": q("SELECT MAX(trade_date) FROM stock_margin WHERE ts_code=?"),
        "估值": q("SELECT MAX(trade_date) FROM stock_val WHERE ts_code=?"),
        "财务": q("SELECT MAX(end_date) FROM stock_finance WHERE ts_code=?"),
        "资产负债": q("SELECT MAX(end_date) FROM stock_bs WHERE ts_code=?"),
        "利润": q("SELECT MAX(end_date) FROM stock_income WHERE ts_code=?"),
        "现金流": q("SELECT MAX(end_date) FROM stock_cashflow WHERE ts_code=?"),
        "股东": q("SELECT MAX(end_date) FROM stock_holders WHERE ts_code=?"),
        "解禁": q("SELECT MAX(float_date) FROM stock_float WHERE ts_code=?"),
        "事件": q("SELECT MAX(ev_date) FROM stock_events WHERE ts_code=?"),
    }
    con.close()
    quarterly = {"财务", "资产负债", "利润", "现金流", "股东", "解禁", "事件"}
    out = {}
    for name, row in qmap.items():
        last = row[0] if row and row[0] else None
        if name == "解禁":
            fresh = bool(last)              # 天然稀疏: 有数据即fresh(茅台2009后无解禁)
        elif name in quarterly:
            fresh = bool(last and last[:7] >= month_ago)
        else:
            fresh = bool(last and last >= week_ago)
        out[name] = {"last": last, "fresh": fresh}
    return out


def update_all(code: str, path=None) -> dict:
    """B2扫雷第一步: 更新全部数据集(返回逐项结果供状态展示)"""
    import fetch_stock as FS
    ts_code = FS._norm_code(code)
    res = {}
    r0 = FS.add_stock(ts_code, path)
    res["日线"] = 1 if r0.get("ok") else 0
    for name, fn in (("两融", "refresh_margin"), ("估值", "refresh_val"),
                     ("财务", "refresh_finance"), ("资产负债", "refresh_bs"),
                     ("利润", "refresh_income"), ("现金流", "refresh_cashflow"),
                     ("股东", "refresh_holders"), ("解禁", "refresh_float"),
                     ("事件", "refresh_events")):
        res[name] = max(0, getattr(FS, fn)(ts_code, path))
    return res


SCAN_PROMPT = """你是资深A股风控分析师, 对以下个股执行全面扫雷体检。综合财务、筹码、估值、事件四维度, 找出所有风险点。

股票: {name}({code}) 最新报告期: {last_period}

一、财务全景(近8季, 金额单位亿/比率单位%):
{fin_table}

二、筹码: 机构持仓合计{inst_ratio}%(近4期 {inst_trend}) · 质押率{pledge}% · 股东户数{holder_num}万(近4期 {hn_trend})
三、估值: PE_TTM {pe} · PB {pb} · 股息率{dv}% · 近一年股价{chg_1y}%
四、近一年风险事件(高/中风险, 最近{n_events}条; 括号内为明细 — 减持看占总股本比例, 规模很小的不要当作大额):
{events}

已命中硬规则(规则引擎):
{rules}

严格按JSON输出(不要其他文字):
{{"summary": "整体风险判断(80字内)", "points": [{{"title": "风险点(15字内)", "evidence": "数据依据(直引上表数值)", "severity": "高|中|低", "dimension": "财务|筹码|估值|事件"}}], "advice": "操作建议(50字内)"}}

最多8条风险点按severity降序; 健康维度可不出点; evidence必须直引上面给出的数据(不要引入其他来源的数字)。"""


def _build_scan_input(code: str, path=None) -> dict:
    """B2: 扫雷全维度输入(财务全景+估值+近一年事件+股价表现)"""
    import fetch_stock as FS
    import datetime as dt
    ts_code = FS._norm_code(code)
    full = FS.finance_full_payload(ts_code, path=path, n_qua=8)
    val = FS.val_payload(ts_code, path=path)
    evs = FS.events_payload(ts_code, path=path)["events"]
    year_ago = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    # 事件序列按日期升序 → 取"最后"12条=最近的。原取前12条(最早): 茅台近一年
    # 26条风险事件时prompt里只有头一年的旧事件, 最近半年的一条不进
    events = [e for e in evs if e["date"] >= year_ago and e["risk"] != "low"][-12:]
    k = FS.kline(ts_code, "qfq", path=path)
    chg_1y = None
    if k.get("ok"):
        for i, d in enumerate(k["dates"]):
            if d >= year_ago:
                chg_1y = round((k["closes"][-1] / k["closes"][i] - 1) * 100, 1)
                break
    return {"full": full, "val": val, "events": events, "chg_1y": chg_1y}


def _tail(vals, n: int = 4, fmt=str) -> str:
    """序列末n项(旧→新)箭头连接; 空序列'--'"""
    xs = [fmt(v) for v in (vals or [])[-n:] if v is not None]
    return "→".join(xs) if xs else "--"


def _scan_ai(ts_code, m, inp, rules, cfg) -> dict:
    """B2: 全维度AI分析(SCAN_PROMPT固定输出)"""
    import ai_client
    full = inp["full"]
    P = full["profit"]
    # None→'--': 空值不进prompt(无商誉的公司商誉列原样写成"None", 让模型猜含义);
    # 也防"股价None%"这类字样
    _dash = lambda v: "--" if v is None else str(v)   # noqa: E731
    lines = ["  报告期|营收|净利|净利同比|毛利率|净利率|ROE|负债率|OCF|商誉占净资产"]
    for i, d in enumerate(full["dates"]):
        ocf = full["cashflow"]["ocf"][i] if i < len(full["cashflow"]["ocf"]) else None
        gw = full["balance"]["gw_eq"][i] if i < len(full["balance"]["gw_eq"]) else None
        dta = full["balance"]["dta"][i] if i < len(full["balance"]["dta"]) else None
        cells = [P["revenue"][i], P["n_income"][i], P["net_yoy"][i], P["gpm"][i],
                 P["npm"][i], P["roe"][i], dta, ocf, gw]
        lines.append("  " + " | ".join([d] + [_dash(v) for v in cells]))
    # D1-35: 筹码取**最新一期**(序列旧→新)。原取[0]=最早一期: 茅台实测把
    # 2003-12-31的"机构4.66%/户数3.2万"当现值喂给AI(实为5.76%/27.8万), AI据此
    # 写出"机构覆盖度不足、筹码分散"; 9只池内股票的筹码维度分析全部基于上市初期数据
    inst = full["inst"]
    val = inp["val"]
    li = len(val["dates"]) - 1 if val.get("dates") else -1
    rules_txt = "\n".join(f"  - [{h['level']}] {h['rule']}" for h in rules["hits"]) \
        or "  (无命中)"
    # 事件行带上明细(减持占总股本比例/预告变动幅度/复牌日) — 只有标题时模型把
    # "减持732万股"一律当大额, 看不到它只占总股本0.036%
    def _ev_line(e):
        d = str(e.get("detail") or "").strip()
        if not d or d.startswith("解禁股东:"):          # 解禁的占比已在标题里
            return f"  - [{e['risk']}]{e['date']} {e['title']}"
        return f"  - [{e['risk']}]{e['date']} {e['title']}({d})"
    events_txt = "\n".join(_ev_line(e) for e in inp["events"]) \
        or "  (近一年无高风险事件)"
    hn = inst["holder_num"][-1] if inst["holder_num"] else None
    prompt = SCAN_PROMPT.format(
        name=m["name"], code=ts_code,
        last_period=full["dates"][-1] if full["dates"] else "-",
        fin_table="\n".join(lines),
        inst_ratio=_dash(inst["ratio"][-1] if inst["ratio"] else None),
        inst_trend=_tail(inst["ratio"], fmt=lambda v: f"{v:.2f}"),
        pledge=_dash(full.get("pledge_ratio")),
        holder_num=_dash(round(hn / 1e4, 1) if hn else None),
        hn_trend=_tail(inst["holder_num"], fmt=lambda v: f"{v / 1e4:.1f}"),
        pe=_dash(val["pe"][li] if li >= 0 and val["pe"][li] else None),
        pb=_dash(val["pb"][li] if li >= 0 and val["pb"][li] else None),
        dv=_dash(P.get("dv_ttm")), chg_1y=_dash(inp["chg_1y"]),
        n_events=len(inp["events"]),
        events=events_txt, rules=rules_txt)
    resp = ai_client.chat(
        cfg["base_url"], cfg["model"],
        [{"role": "user", "content": prompt}],
        # qwen3 thinking模式: reasoning先耗max_tokens(实测扫雷prompt下
        # reasoning 2658字耗尽2000 → content空) → 6000留足content额度
        api_key=cfg["api_key"], temperature=0.1, max_tokens=6000, timeout=240)
    text = resp.strip()
    if "```" in text:
        for part in text.split("```"):
            part = part.replace("json", "").strip()
            if part.startswith("{"):
                text = part
                break
    else:
        # 模型偶尔裸输出markdown前缀(**思考**: ...)+```json — 容错截取首个{
        i = text.find("{")
        if i > 0:
            text = text[i:]
    return _validate_ai_points(json.loads(text), m, prompt)


# 扫雷结果口径版本: prompt/输入组装方式变更时+1 → 旧缓存视为过期(需重新扫雷)。
# v2(D1-35): 筹码栏取最新一期+近4期走势、事件取最近12条、财务表限8季
SCAN_VERSION = 2


def _ai_cache_valid(ai_json) -> bool:
    """缓存里的AI结果是否为当前口径: 非字典(无AI/规则-only)视为有效;
    字典须带当前版本标记(AI失败降级的summary字典无标记 → 下次自动重试)"""
    try:
        ai = json.loads(ai_json or "null")
    except ValueError:
        return False
    return not isinstance(ai, dict) or ai.get("_v") == SCAN_VERSION


def scan_stock(code: str, path=None, force: bool = False,
               cached_only: bool = False) -> dict:
    """B2一键扫雷(合并AI分析): 数据→规则→全维度AI→报告入库可回看。
    force=True跳过缓存重扫; 财务不足返回incomplete让前端确认。
    cached_only=True: 只读缓存, 无有效缓存返回 {ok:False, nocache:True}
    — 供页面加载时的GET接口使用(D1-35: 原GET在无缓存/缓存过期时会直接
    跑一遍AI扫雷, 仅仅打开一只没扫过的股票就静默占用本地模型1~3分钟)。"""
    import fetch_stock as FS
    from analyze_news import get_ai_config
    ts_code = FS._norm_code(code)
    m = finance_metrics(ts_code, path=path)
    if m["n"] < 2:
        return {"ok": False, "incomplete": True,
                "msg": f"财务数据仅{m['n']}季不足 — 建议先[数据更新]再扫雷"}
    con = _conn(path)
    last_ann = con.execute(
        "SELECT MAX(COALESCE(ann_date,'')) FROM stock_finance WHERE ts_code=?",
        (ts_code,)).fetchone()[0] or ""
    cached = con.execute(
        "SELECT end_date, ann_date, rule_grade, rule_score, rules_json, ai_json "
        "FROM stock_ai WHERE ts_code=?", (ts_code,)).fetchone()
    end_max = m["rows"][-1]["end_date"]
    con.close()
    if (cached and not force and cached[0] == end_max and cached[1] == last_ann
            and _ai_cache_valid(cached[5])):
        ai_c = json.loads(cached[5] or "null")
        return {"ok": True, "cached": True, "code": ts_code, "name": m["name"],
                "grade": cached[2], "score": cached[3],
                "rules": json.loads(cached[4] or "[]"),
                "ai": {k: v for k, v in ai_c.items() if k != "_v"}
                if isinstance(ai_c, dict) else ai_c,
                "end_date": end_max}
    if cached_only:
        return {"ok": False, "nocache": True, "code": ts_code,
                "msg": "尚无当前口径的扫雷结果 — 点[一键扫雷]生成"}
    inp = _build_scan_input(ts_code, path)
    rules = run_rules(m)
    ai, ai_ok = None, False
    cfg = get_ai_config()
    if cfg["enabled"] and cfg["model"]:
        try:
            ai = _scan_ai(ts_code, m, inp, rules, cfg)
            ai_ok = True
        except Exception as e:                               # noqa: BLE001
            ai = {"summary": f"AI定性分析失败(仅规则引擎结果): {str(e)[:40]}",
                  "points": [], "advice": ""}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    # 只有AI真正跑完的结果才打版本标记; 失败降级的字典不打 → 不算有效缓存
    stored = ({"_v": SCAN_VERSION, **ai} if ai_ok and isinstance(ai, dict) else ai)
    con = _conn(path)
    con.execute(
        "INSERT INTO stock_ai(ts_code,end_date,ann_date,rule_grade,rule_score,"
        "rules_json,ai_json,model,analyzed_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ts_code) DO UPDATE SET end_date=excluded.end_date,"
        "ann_date=excluded.ann_date,rule_grade=excluded.rule_grade,"
        "rule_score=excluded.rule_score,rules_json=excluded.rules_json,"
        "ai_json=excluded.ai_json,model=excluded.model,"
        "analyzed_at=excluded.analyzed_at",
        (ts_code, end_max, last_ann, rules["grade"], rules["score"],
         json.dumps(rules["hits"], ensure_ascii=False),
         json.dumps(stored, ensure_ascii=False), cfg.get("model") or "-", now))
    con.commit()
    con.close()
    return {"ok": True, "cached": False, "code": ts_code, "name": m["name"],
            "grade": rules["grade"], "score": rules["score"],
            "rules": rules["hits"], "ai": ai, "end_date": end_max}


def analyze_stock_finance(code: str, path=None) -> dict:
    """兼容入口(A4): 转发scan_stock"""
    return scan_stock(code, path=path)
