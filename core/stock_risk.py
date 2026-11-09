# -*- coding: utf-8 -*-
"""避雷针A4: AI财报风险评级 — 混合模式(规则引擎打底 + AI定性补充)

N1(计划review): 纯AI评级会漂移 → 评级锚定规则分(A/B/C/D), AI提供风险点叙述;
N2: 风险点必须带指标名+数值依据, 指标名必须在输入表中存在(幻觉校验);
N3: 手动触发串行, 不进夜间链; 缓存按(ts_code,end_date,ann_date)失效
(更正版财报ann_date变化也触发重分析); 次新股不足季数降级。
免责: AI分析为参考信号, 非投资建议。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

FIN_PROMPT = """你是资深A股财务风控分析师。基于以下近{n_qua}季核心财务指标与已命中的硬规则, 输出风险分析。

股票: {name}({code})
指标表(报告期: {fields}):
{table}

已命中硬规则(规则引擎, 评级锚定):
{rules}

严格按JSON输出(不要其他文字):
{{"summary": "总体财务风险评估(60字内)", "points": [{{"title": "风险点(15字内)",
"evidence": "指标名=数值(必须直接引用上表数值)", "severity": "高|中|低"}}],
"advice": "关注建议(40字内)"}}

要求: points只列真实存在于指标表/规则中的问题(最多6条), 不得编造指标或数值;
若指标健康则points可以为空数组; evidence必须形如"扣非净利同比=-45.2%(连续2期)"。"""


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
        hits.append({"rule": f"扣非净利同比连续为负({n_yoy[-2]}%,{n_yoy[-1]}%)",
                     "level": "高", "score": -10})
    elif len(n_yoy) >= 2 and n_yoy[-1] is not None and n_yoy[-2] is not None \
            and n_yoy[-2] > 0 and n_yoy[-1] < 0:
        hits.append({"rule": f"扣非净利同比转负({n_yoy[-2]}%→{n_yoy[-1]}%)",
                     "level": "高", "score": -10})
    dta = _last("debt_to_assets")
    if dta is not None and dta > 70:
        hits.append({"rule": f"资产负债率{dta}%>70%", "level": "中", "score": -5})
    gpm = _seq("gpm")
    if len(gpm) >= 4 and all(v is not None for v in gpm[-4:]) \
            and gpm[-1] < gpm[-2] < gpm[-3] < gpm[-4]:
        hits.append({"rule": "毛利率连续3期下滑", "level": "中", "score": -5})
    o_yoy = _seq("or_yoy")
    if len(o_yoy) >= 2 and o_yoy[-1] is not None and o_yoy[-2] is not None \
            and o_yoy[-1] < 0 and o_yoy[-2] < 0:
        hits.append({"rule": f"营收同比连续为负({o_yoy[-2]}%,{o_yoy[-1]}%)",
                     "level": "中", "score": -5})
    gw = _last("goodwill_eq")
    if gw is not None and gw > 30:
        hits.append({"rule": f"商誉占净资产{gw}%>30% — 减值爆雷风险",
                     "level": "高", "score": -10})
    roe = _last("roe")
    if roe is not None and roe < 0:
        hits.append({"rule": f"ROE为负({roe}%)", "level": "高", "score": -10})
    score = sum(h["score"] for h in hits)
    grade = "A" if score == 0 else ("B" if score >= -5 else
                                    ("C" if score >= -15 else "D"))
    return {"score": score, "grade": grade, "hits": hits}


KNOWN_METRICS = {"dedt_yi": "扣非净利润(亿)", "netprofit_yoy": "扣非净利同比%",
                 "or_yoy": "营收同比%", "gpm": "毛利率%", "roe": "ROE%",
                 "debt_to_assets": "资产负债率%", "goodwill_eq": "商誉占净资产%"}


def _validate_ai_points(ai: dict, metrics: dict) -> dict:
    """N2幻觉校验: evidence中的指标引用必须存在于输入表(数值由prompt约束直引)"""
    rows = metrics["rows"]
    for p in (ai or {}).get("points", []):
        ev = str(p.get("evidence", ""))
        p["suspect"] = not any(k in ev for k in KNOWN_METRICS) and \
            not any(str(v) in ev for r in rows for v in r.values() if v is not None)
    return ai


def analyze_stock_finance(code: str, path=None) -> dict:
    """主流程: 数据→规则→(AI定性, 本地模型失败降级仅规则)→缓存。
    N3: 手动触发用; 缓存按(报告期,ann_date)失效。"""
    import fetch_stock as FS
    import ai_client
    from analyze_news import get_ai_config
    ts_code = FS._norm_code(code)
    m = finance_metrics(ts_code, path=path)
    if m["n"] < 2:
        return {"ok": False, "msg": f"财务数据不足({m['n']}季, 次新股降级不分析)"}
    con = _conn(path)
    last_ann = con.execute(
        "SELECT MAX(COALESCE(ann_date,'')) FROM stock_finance WHERE ts_code=?",
        (ts_code,)).fetchone()[0] or ""
    cached = con.execute(
        "SELECT end_date, ann_date, rule_grade, rule_score, rules_json, ai_json "
        "FROM stock_ai WHERE ts_code=?", (ts_code,)).fetchone()
    end_max = m["rows"][-1]["end_date"]
    if cached and cached[0] == end_max and cached[1] == last_ann:
        con.close()                                   # 缓存命中(同报告期同公告日)
        return {"ok": True, "cached": True, "code": ts_code, "name": m["name"],
                "grade": cached[2], "score": cached[3],
                "rules": json.loads(cached[4] or "[]"),
                "ai": json.loads(cached[5] or "null"),
                "end_date": end_max}
    con.close()
    rules = run_rules(m)
    ai = None
    cfg = get_ai_config()
    if cfg["enabled"] and cfg["model"]:
        fields = ["报告期", *KNOWN_METRICS.values()]
        table = "\n".join(
            "  " + " | ".join(str(v) for v in
                              [r["end_date"], r["dedt_yi"], r["netprofit_yoy"],
                               r["or_yoy"], r["gpm"], r["roe"],
                               r["debt_to_assets"], r["goodwill_eq"]])
            for r in m["rows"])
        rules_txt = "\n".join(f"  - [{h['level']}] {h['rule']}" for h in rules["hits"]) \
            or "  (无命中 — 指标健康)"
        try:
            resp = ai_client.chat(
                cfg["base_url"], cfg["model"],
                [{"role": "user", "content": FIN_PROMPT.format(
                    n_qua=m["n"], name=m["name"], code=ts_code,
                    fields=" | ".join(fields), table=table, rules=rules_txt)}],
                api_key=cfg["api_key"], temperature=0.1, max_tokens=2000, timeout=180)
            text = resp.strip()
            if "```" in text:
                for part in text.split("```"):
                    part = part.replace("json", "").strip()
                    if part.startswith("{"):
                        text = part
                        break
            ai = _validate_ai_points(json.loads(text), m)
        except Exception as e:                               # noqa: BLE001
            ai = {"summary": f"AI定性分析失败(仅规则引擎结果): {str(e)[:40]}",
                  "points": [], "advice": ""}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
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
         json.dumps(ai, ensure_ascii=False), cfg.get("model") or "-", now))
    con.commit()
    con.close()
    return {"ok": True, "cached": False, "code": ts_code, "name": m["name"],
            "grade": rules["grade"], "score": rules["score"],
            "rules": rules["hits"], "ai": ai, "end_date": end_max,
            "stale": bool(cached and (cached[0] != end_max or cached[1] != last_ann))}
