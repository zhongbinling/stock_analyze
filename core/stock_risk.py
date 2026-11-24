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


# 扣非净利同比两条规则的分级门槛(百分点)。
# D1-36: 原"连续为负/由增转负"一律按高危扣10分 — 茅台扣非净利同比-1.95%、-0.25%
# (持平级别)因此被判C级。改为按降幅分级, 规则名称与扣分制不变:
#   连续为负: 两期中任一降幅 ≥ _YOY_DROP_HIGH 判高, 否则判中(关注)。
#     取"任一期"而非两期平均: (-2%,-12%)正在恶化, 平均只有7%会被低估
#   由增转负: 回落幅度(上期-本期) ≥ _YOY_DROP_HIGH 判高, ≥ _YOY_DROP_MID 判中,
#     更小视为持平、不命中(原+0.3%→-0.2%也按高危)
# 生产库回测(自选池9只×历史各报告期共360个组合): 仅3个由C变B, 均为微降;
# 大降幅的高危判定全部不变
_YOY_DROP_HIGH = 10.0
_YOY_DROP_MID = 5.0


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
    if len(n_yoy) >= 2 and None not in (n_yoy[-1], n_yoy[-2]):   # 两期即可判, 不要求有第三期
        cur, prev = n_yoy[-1], n_yoy[-2]
        if cur < 0 and prev < 0:
            high = max(abs(cur), abs(prev)) >= _YOY_DROP_HIGH
            hits.append({"rule": f"扣非净利同比连续为负({prev:.2f}%,{cur:.2f}%)",
                         "level": "高" if high else "中", "score": -10 if high else -5})
        elif prev > 0 and cur < 0:
            drop = prev - cur
            if drop >= _YOY_DROP_MID:                 # 回落幅度太小 = 持平, 不命中
                high = drop >= _YOY_DROP_HIGH
                hits.append({"rule": f"扣非净利同比转负({prev:.2f}%→{cur:.2f}%)",
                             "level": "高" if high else "中", "score": -10 if high else -5})
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


_CLAUSE_SPLIT = re.compile(r"[；;，,。]")
# 依据里对指标的常见说法 → _metric_series 的键(金额与增速是两个序列: "营收"是金额,
# "营收同比"是增速; 被更长的说法包含的短说法自动作废)
_METRIC_ALIASES = (
    ("扣非净利同比", "净利同比"), ("净利同比", "净利同比"), ("净利增速", "净利同比"),
    ("净利增长", "净利同比"), ("利润增速", "净利同比"), ("利润同比", "净利同比"),
    ("盈利增速", "净利同比"),
    ("营收同比", "营收同比"), ("营收增速", "营收同比"), ("营收增长", "营收同比"),
    ("收入增速", "营收同比"), ("收入同比", "营收同比"),
    ("毛利率", "毛利率"), ("净利率", "净利率"), ("ROE", "ROE"),
    ("资产负债率", "资产负债率"), ("负债率", "资产负债率"),
    ("经营现金流", "经营现金流"), ("经营性现金流", "经营现金流"), ("OCF", "经营现金流"),
    ("商誉占净资产", "商誉占净资产"), ("商誉占比", "商誉占净资产"),
    ("营业收入", "营收"), ("营收", "营收"), ("净利润", "净利"), ("净利", "净利"),
    ("机构持仓", "机构持仓"), ("股东户数", "股东户数"), ("户数", "股东户数"),
    ("PE", "PE"), ("PB", "PB"), ("股息率", "股息率"),
)


def _near(x: float, xs: list) -> bool:
    """x(绝对值)是否等于序列中某个值的绝对值; 容差同_grounded(AI常四舍五入到两位)"""
    return any(v is not None and abs(x - abs(v)) <= max(0.001 * max(x, abs(v)), 0.0055)
               for v in xs or [])


def _misattributed(ev: str, series: dict) -> bool:
    """依据里是否有子句"说的是A指标, 数字却全是B指标的"。

    D1-36: 这些数值都能在prompt里找到(所以_grounded放行), 只是安错了指标 — 实测
    模型把财务表的"净利同比"一列当成"营收同比", 写出"茅台营收同比连续三期负增长"
    (其营收同比为正)。判定: 子句点名的指标没有一个数字对得上, 而同一子句里
    有≥2个数字对得上某个没点名的指标。只有1个数字的子句不判(推导值太常见)"""
    for clause in _CLAUSE_SPLIT.split(ev or ""):
        nums = _key_nums(clause)
        if len(nums) < 2:
            continue
        found = [a for a, _k in _METRIC_ALIASES if a in clause]
        named = {k for a, k in _METRIC_ALIASES
                 if a in found and not any(a != b and a in b for b in found)}
        if not named:
            continue
        if any(_near(abs(n), series[k]) for n in nums for k in named if series.get(k)):
            continue
        if any(k not in named and xs and sum(_near(abs(n), xs) for n in nums) >= 2
               for k, xs in series.items()):
            return True
    return False


# 依据里的日期/年份(取整数时不计): 2026-11-18、2025年12月31日、2026Q3、2026H1、2025年报
_DATE_RE = re.compile(
    r"(?<![\d.])\d{4}[-./年]\d{1,2}(?:[-./月]\d{1,2}日?)?(?![\d.])"
    r"|(?<![\d.])\d{4}[QH][1-4]?(?![\d.])"
    r"|(?<![\d.])\d{4}(?![\d.])")


def _int_nums(text: str) -> list:
    """依据里写成整数的量("减持127万股""营收300亿"): 两位以上的整数, 日期与年份不计"""
    return [float(t) for t in _NUM_RE.findall(_DATE_RE.sub(" ", text or ""))
            if "." not in t and len(t) >= 2]


def _ints_grounded(ints: list, prompt_nums: list) -> bool:
    """每个整数都是prompt里某个数值的取整(±0.5, 三位数以上放宽到1%)"""
    return all(any(abs(k - p) <= max(0.5, 0.01 * k) for p in prompt_nums) for k in ints)


def _validate_ai_points(ai: dict, metrics: dict, prompt: str = "", series: dict = None) -> dict:
    """N2幻觉校验: 风险点的evidence须有据可依。

    传prompt时(扫雷主流程): AI拿到的全部数据都在prompt里, 依据中的关键数值
    必须能在prompt原文找到(找不到=凭空编造); 不含关键数值的定性依据退回
    指标名判断。D1-35: 原只认财务指标名/财务行数值 — 筹码/估值/事件三个
    维度的依据本就不含这些, 实测65条风险点里40条被标"依据存疑"(筹码/估值/
    事件几乎全中), 标记失去意义。
    D1-36: 写成整数的量("减持127万股", prompt里是"127.0万股")原被当成"定性依据",
    不含财务指标名就标存疑 — 与上面同类的误标; 现按取整后能否对上来判。
    再传series(各指标的取值, 见_metric_series)时加查"数值安错指标"(_misattributed)。
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
            p["suspect"] = not _grounded(ev, pnums) or \
                (bool(series) and _misattributed(ev, series))
        else:
            ints = _int_nums(ev)
            p["suspect"] = not (_ints_grounded(ints, pnums) if ints else has_term)
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

一、财务全景(近8季; 每行一个指标, 各列为报告期; 金额单位亿/比率单位%):
{fin_table}

二、筹码: 机构持仓合计{inst_ratio}%(近4期 {inst_trend}) · 质押率{pledge}% · 股东户数{holder_num}万(近4期 {hn_trend})
三、估值: PE_TTM {pe} · PB {pb} · 股息率{dv}% · 近一年股价{chg_1y}%
四、近一年风险事件(高/中风险, 最近{n_events}条; 括号内为明细 — 减持看占流通股本比例, 规模很小的不要当作大额):
{events}

已命中硬规则(规则引擎):
{rules}

严格按JSON输出(不要其他文字):
{{"summary": "整体风险判断(80字内)", "points": [{{"title": "风险点(15字内)", "evidence": "数据依据(写明是哪个指标哪个报告期, 直引上表数值)", "severity": "高|中|低", "dimension": "财务|筹码|估值|事件"}}], "advice": "操作建议(50字内)"}}

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


def _dash(v) -> str:
    """None→'--': 空值不进prompt(无商誉的公司商誉列原样写成"None", 让模型猜含义);
    也防"股价None%"这类字样"""
    return "--" if v is None else str(v)


def _fin_table(full: dict) -> list:
    """扫雷prompt的财务表: 每行一个指标, 各列为报告期(金额单位亿, 比率单位%)。

    D1-36: 原为每行一个报告期的10列表格 — 模型读趋势要竖着看一列, 实测看串列:
    把"净利同比"一列当成"营收同比", 写出"茅台营收同比连续三期负增长"(其营收
    同比为正)。营收同比(规则引擎在用)原表里没有, 一并补上"""
    P, C, B = full["profit"], full["cashflow"], full["balance"]
    ds = full["dates"]
    rows = [("报告期", ds), ("营收(亿)", P["revenue"]), ("营收同比(%)", P.get("or_yoy")),
            ("净利(亿)", P["n_income"]), ("净利同比(%)", P["net_yoy"]),
            ("毛利率(%)", P["gpm"]), ("净利率(%)", P["npm"]), ("ROE(%)", P["roe"]),
            ("资产负债率(%)", B["dta"]), ("经营现金流(亿)", C["ocf"]),
            ("商誉占净资产(%)", B["gw_eq"])]
    out = []
    for name, seq in rows:
        seq = seq or []
        cells = [_dash(seq[i] if i < len(seq) else None) for i in range(len(ds))]
        out.append("  " + " | ".join([name] + cells))
    return out


def _metric_series(full: dict, val: dict) -> dict:
    """依据核对用(_misattributed): 各指标喂给AI的取值序列, 键见_METRIC_ALIASES"""
    P, C, B, inst = full["profit"], full["cashflow"], full["balance"], full["inst"]
    li = len(val["dates"]) - 1 if val.get("dates") else -1
    last = lambda seq: seq[li] if 0 <= li < len(seq) else None   # noqa: E731
    return {"营收同比": P.get("or_yoy") or [], "净利同比": P["net_yoy"],
            "毛利率": P["gpm"], "净利率": P["npm"], "ROE": P["roe"],
            "资产负债率": B["dta"], "经营现金流": C["ocf"],
            "商誉占净资产": B["gw_eq"], "营收": P["revenue"], "净利": P["n_income"],
            "机构持仓": inst["ratio"],
            "股东户数": [v / 1e4 if v is not None else None for v in inst["holder_num"]],
            "PE": [last(val["pe"])], "PB": [last(val["pb"])], "股息率": [P.get("dv_ttm")]}


def _scan_ai(ts_code, m, inp, rules, cfg) -> dict:
    """B2: 全维度AI分析(SCAN_PROMPT固定输出)"""
    import ai_client
    full = inp["full"]
    P = full["profit"]
    lines = _fin_table(full)
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
    return _validate_ai_points(json.loads(text), m, prompt,
                               _metric_series(full, val))


# 扫雷结果口径版本: prompt/输入组装方式/规则分级变更时+1 → 旧缓存视为过期(需重新扫雷)。
# v2(D1-35): 筹码栏取最新一期+近4期走势、事件取最近12条、财务表限8季
# v3(D1-36): 扣非净利同比规则按降幅分级(规则级别进了prompt, 且缓存里存着评级与规则分);
#            财务表改为每行一个指标并补营收同比; 依据核对加查"数值安错指标"
SCAN_VERSION = 3


def _ai_cache_state(ai_json) -> str:
    """缓存里AI结果的状态:
    valid    — 当前口径的完整结果(或未启用AI的规则-only结果)
    degraded — AI没跑成的降级结果(模型不可用/返回无法解析): 规则评级有效, AI定性缺失
    stale    — 旧口径的完整结果, 需重新扫雷
    D1-36: 原只有"有效/无效"两态, 降级结果按无效处理 → 页面只读接口返回404, 刷新后
    显示"尚无扫雷结果", 把有效的规则评级也藏了起来(改动前刷新还能看到评级)"""
    try:
        ai = json.loads(ai_json or "null")
    except ValueError:
        return "stale"
    if not isinstance(ai, dict):
        return "valid"
    if ai.get("_v") == SCAN_VERSION:
        return "valid"
    if ai.get("_degraded") or (not ai.get("points")
                               and str(ai.get("summary") or "").startswith("AI定性分析失败")):
        return "degraded"                 # 后半句: 兼容未带标记的存量降级结果
    return "stale"


def scan_stock(code: str, path=None, force: bool = False,
               cached_only: bool = False) -> dict:
    """B2一键扫雷(合并AI分析): 数据→规则→全维度AI→报告入库可回看。
    force=True跳过缓存重扫; 财务不足返回incomplete让前端确认。
    cached_only=True: 只读缓存, 无当前口径的结果返回 {ok:False, nocache:True};
    AI没跑成的降级结果照常返回并带 degraded=True(规则评级有效) — 供页面加载时的
    GET接口使用(D1-35: 原GET在无缓存/缓存过期时会直接跑一遍AI扫雷, 仅仅打开一只
    没扫过的股票就静默占用本地模型1~3分钟)。"""
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
    same = bool(cached and not force and cached[0] == end_max and cached[1] == last_ann)
    state = _ai_cache_state(cached[5]) if same else "stale"
    # 只读接口(cached_only)连降级结果也返回: 规则评级有效, 只缺AI定性 — 由页面提示
    # 用户点[一键扫雷]重试。旧口径(stale)仍然不返回
    if same and (state == "valid" or (cached_only and state == "degraded")):
        ai_c = json.loads(cached[5] or "null")
        out = {"ok": True, "cached": True, "code": ts_code, "name": m["name"],
               "grade": cached[2], "score": cached[3],
               "rules": json.loads(cached[4] or "[]"),
               "ai": {k: v for k, v in ai_c.items() if k not in ("_v", "_degraded")}
               if isinstance(ai_c, dict) else ai_c,
               "end_date": end_max}
        if state == "degraded":
            out["degraded"] = True
            out["msg"] = "上次AI定性分析未成功(规则评级有效) — 点[一键扫雷]重试"
        return out
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
    # 标记: 跑完的结果带口径版本; AI失败的降级结果带_degraded(不算有效缓存, 下次扫雷
    # 重跑AI); AI未启用时为None(规则-only, 视为有效)
    degraded = False
    if ai_ok and isinstance(ai, dict):
        stored = {"_v": SCAN_VERSION, **ai}
    elif ai is None:
        stored = None
    else:
        stored, degraded = {"_degraded": True, **ai}, True
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
    out = {"ok": True, "cached": False, "code": ts_code, "name": m["name"],
           "grade": rules["grade"], "score": rules["score"],
           "rules": rules["hits"], "ai": ai, "end_date": end_max}
    if degraded:
        out["degraded"] = True
    return out


def analyze_stock_finance(code: str, path=None) -> dict:
    """兼容入口(A4): 转发scan_stock"""
    return scan_stock(code, path=path)
