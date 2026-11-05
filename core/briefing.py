# -*- coding: utf-8 -*-
"""D1-29: 数据简报生成器 — 七模块只读组装 → 移动端友好单页HTML

用法: python briefing.py [--out path.html]
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SENT_DB = BASE / "data" / "sentiment.db"


def _esc(s) -> str:
    """HTML转义(事件标题/AI概要来自DB, 静态页也应转义)"""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _conn():
    return sqlite3.connect(f"file:{SENT_DB.as_posix()}?mode=ro", uri=True)


def generate_html(out=None) -> Path:
    from server import macro as MC
    t0 = time.time()
    today = time.strftime("%Y-%m-%d")
    con = _conn()
    # 1. 情绪温度
    mood = con.execute("SELECT date,score,stage FROM sentiment ORDER BY date DESC LIMIT 2").fetchall()
    mood_prev = mood[1] if len(mood) > 1 else None
    # 2. 板块冷热前5 + 5日趋势(简报增强: Δ5升降温箭头)
    sdates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM sector_scores ORDER BY date DESC LIMIT 6")]
    latest_d = sdates[0] if sdates else None
    d5 = sdates[5] if len(sdates) >= 6 else None     # 5个交易日前
    prev_scores = {c: s for c, s in con.execute(
        "SELECT code, score FROM sector_scores WHERE date=?", (d5,))} if d5 else {}
    hot_raw = con.execute("SELECT code, score FROM sector_scores WHERE date=? ORDER BY score DESC LIMIT 5", (latest_d,)).fetchall() if latest_d else []
    cold_raw = con.execute("SELECT code, score FROM sector_scores WHERE date=? ORDER BY score ASC LIMIT 5", (latest_d,)).fetchall() if latest_d else []
    # D1-30r: BK/SW3代码→中文名(简报可读性; 映射移至hot/cold构建处带代码)
    name_map = {r[0]: r[1] for r in con.execute("SELECT code, name FROM sectors")}
    # 3. 估值水位
    val = con.execute("SELECT date,alla_pe,alla_pb,pe300 FROM mkt_val_daily ORDER BY date DESC LIMIT 1").fetchone()
    # 4. 两融
    mg = con.execute("SELECT date,rzrqye FROM margin_daily ORDER BY date DESC LIMIT 6").fetchall()
    # 5. ETF近5日 + 环比前5日(简报增强)
    etf = con.execute("SELECT date,SUM(amount)/1e5 FROM etf_daily GROUP BY date ORDER BY date DESC LIMIT 10").fetchall()
    con.close()
    # 6. 巴菲特
    bf = MC.buffett()["latest"]
    # 7. 新闻事件
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now()
    h24 = (now - _td(hours=24)).strftime("%Y-%m-%d")
    d7 = (now - _td(days=7)).strftime("%Y-%m-%d")
    ev24 = [e for e in MC.list_events()["events"] if e["date"] >= h24 and e["level"] >= 2]
    ev7 = [e for e in MC.list_events()["events"] if h24 > e["date"] >= d7 and e["level"] >= 2]

    def _fmt_pct(v): return f"{v*100:.0f}%" if v is not None else "--"  # viewer小数→%
    def _pct_bar(v, label=""):
        """D1-29r: 分位进度条(≥80%红/≤20%蓝/中间灰, 情绪图环境面板同配色)"""
        if v is None:
            return f'<span style="color:#999">{label}--</span>'
        pct = round(v * 100)
        color = "#e34448" if pct >= 80 else "#2f5cd6" if pct <= 20 else "#898781"
        bar = (f'<div style="display:inline-block;width:60px;height:8px;'
               f'background:#e8e8e8;border-radius:4px;vertical-align:middle">'
               f'<div style="width:{max(pct,2)}%;height:100%;background:{color};'
               f'border-radius:4px"></div></div>')
        return f'{label}{bar} <b style="color:{color}">{pct}%</b>'

    def _lv_badge(lv): return f'<span style="background:{"#d03b3b" if lv>=5 else "#e67e22" if lv==4 else "#eda100" if lv==3 else "#8e44ad"};color:#fff;padding:1px 5px;border-radius:3px;font-size:11px">L{lv}</span>'

    rows_score = ""
    if mood:
        d, s, st = mood[0]
        delta = f"{s - mood_prev[1]:+.1f}" if mood_prev and mood_prev[1] is not None else "--"
        rows_score = f"<tr><td>情绪分</td><td><b style=\"color:{'#e34448' if s >= 0 else '#2f5cd6'}\">{s:+.1f}</b> [{st}] (环比 {delta})</td></tr>"

    rows_val = ""
    if val:
        # 分位从viewer payload读取(表里只有原始值)
        try:
            import score as _S
            import viewer as _V
            cfg = _S.load_cfg()
            vd = _V.build_payload(cfg)
            mval = vd.get("mval", {})
            i = len(vd["dates"]) - 1
            pe_pct = mval.get("alla_pe_pct", [None] * (i + 1))[i]
            pe_pct5 = mval.get("alla_pe_pct5", [None] * (i + 1))[i]
            pb_pct = mval.get("alla_pb_pct", [None] * (i + 1))[i]
            pb_pct5 = mval.get("alla_pb_pct5", [None] * (i + 1))[i]
            r300_pct = mval.get("pe300_pct", [None] * (i + 1))[i]
        except Exception:
            pe_pct = pb_pct = pe_pct5 = pb_pct5 = r300_pct = None
        # 简报增强: PE/PB分行各带全史+5年分位条(PB原取而未渲染)
        rows_val = (f"<tr><td>全A PE</td><td>{val[1]:.1f}</td>"
                    f"<td>{_pct_bar(pe_pct, '全史')} {_pct_bar(pe_pct5, '5年')}</td></tr>"
                    f"<tr><td>全A PB</td><td>{val[2]:.2f}</td>"
                    f"<td>{_pct_bar(pb_pct, '全史')} {_pct_bar(pb_pct5, '5年')}</td></tr>"
                    f"<tr><td>沪深300 PE</td><td>{val[3]:.1f}</td>"
                    f"<td>{_pct_bar(r300_pct, '全史')}</td></tr>")

    rows_bf = ""
    if bf:
        bf_all = bf['pct_all'] / 100 if bf['pct_all'] else None
        bf_5 = bf.get('pct_5y') / 100 if bf.get('pct_5y') else None
        rows_bf = (f"<tr><td>巴菲特指数</td><td><b>{bf['ratio']}%</b></td>"
                   f"<td>{_pct_bar(bf_all, '全史')} {_pct_bar(bf_5, '5年')}</td></tr>")

    mg_html = ""
    if mg:
        latest = mg[0]
        prev5 = mg[-1] if len(mg) >= 6 else mg[-1] if len(mg) > 1 else None
        chg = f"{(latest[1] - prev5[1]) / prev5[1] * 100:+.1f}%" if prev5 and prev5[1] else "--"
        mg_html = f"<tr><td>两融余额</td><td><b>{latest[1] / 1e12:.2f}</b>万亿</td><td>5日变化 {chg}</td></tr>"

    etf_html = ""
    if etf:
        etf_r = list(reversed(etf))                    # 时间升序
        etf_html = "<tr><td>宽基ETF总量</td><td>" + " → ".join(
            f"<b>{v:.0f}</b>" for _, v in etf_r[-5:]) + " 亿</td></tr>"
        if len(etf_r) >= 10:                           # 简报增强: 近5日均值环比前5日
            cur5 = sum(v for _, v in etf_r[-5:]) / 5
            prev5 = sum(v for _, v in etf_r[-10:-5]) / 5
            if prev5:
                chg = (cur5 - prev5) / prev5 * 100
                c = "#e34448" if chg >= 0 else "#2f5cd6"
                etf_html += (f"<tr><td class='hint'>近5日均值</td><td><b>{cur5:.0f}</b> 亿"
                             f" (环比前5日 <b style='color:{c}'>{chg:+.1f}%</b>)</td></tr>")

    def ev_rows(evs):
        if not evs:
            return '<tr><td colspan="3" style="color:#999">无</td></tr>'
        # 简报增强: L2+事件附AI概要(灰字小号, 自包含不点开也有判断)
        return "".join(
            f'<tr><td>{e["date"][5:]}</td><td>{_lv_badge(e["level"])}</td>'
            f'<td>{_esc(e["title"][:40])}'
            + (f'<br><small style="color:#898781">{_esc(e.get("ai_impact","")[:30])}</small>'
               if e.get("ai_impact") else '')
            + '</td></tr>'
            for e in sorted(evs, key=lambda x: -x["level"])[:15])

    def _trend(code, score):
        """简报增强: 5日分差箭头(升=红↑热度升, 降=蓝↓); prev_scores按代码键控"""
        p = prev_scores.get(code)
        if p is None or score is None:
            return ""
        d = score - p
        if abs(d) < 0.05:
            return ' <small style="color:#898781">→持平</small>'
        return (f' <small style="color:{"#e34448" if d > 0 else "#2f5cd6"}">'
                f'{"↑" if d > 0 else "↓"}{abs(d):.1f}</small>')

    # (名称, 分, 代码) — 显示用名称, 趋势按代码查5日前
    hot = [(name_map.get(c, c), s2, c) for c, s2 in hot_raw]
    cold = [(name_map.get(c, c), s2, c) for c, s2 in cold_raw]
    hot_html = "".join(f'<tr><td>{nm}{_trend(code, s)}</td><td style="color:#e34448">{s:+.1f}</td></tr>' for nm, s, code in hot)
    cold_html = "".join(f'<tr><td>{nm}{_trend(code, s)}</td><td style="color:#2f5cd6">{s:+.1f}</td></tr>' for nm, s, code in cold)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>数据简报 · {today}</title>
<style>
body{{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;background:#f5f5f5;color:#222}}
.card{{background:#fff;margin:10px;padding:14px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
h2{{font-size:14px;color:#666;margin:0 0 10px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td{{padding:4px 6px;border-bottom:1px solid #eee}}
small{{color:#999}}
@media print {{ body{{background:#fff}} .card{{box-shadow:none;margin:0;padding:6px}} .noprint{{display:none}} }}
</style></head><body>
<div class="card"><h2>📊 数据简报 · {today} {time.strftime('%H:%M')}
<button class="noprint" onclick="window.print()" style="float:right;font-size:12px;
padding:2px 10px;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer">🖨 导出PDF/打印</button></h2>
<table>{rows_score}{rows_val}{rows_bf}{mg_html}{etf_html}</table></div>
<div class="card"><h2>🔥 板块冷热 TOP5 (最新日)</h2>
<table style="width:48%;display:inline-table"><tr><th>最热</th><th>分</th></tr>{hot_html}</table>
<table style="width:48%;display:inline-table;margin-left:2%"><tr><th>最冷</th><th>分</th></tr>{cold_html}</table></div>
<div class="card"><h2>📰 近24小时事件 (L2+)</h2><table>{ev_rows(ev24)}</table>
<h2 style="margin-top:12px">📰 24小时~7天事件</h2><table>{ev_rows(ev7)}</table>
<p><small>数据来源: sentiment.db / macro.db · 自动生成于 {time.strftime('%Y-%m-%d %H:%M:%S')} ({time.time()-t0:.1f}s)</small></p></div>
</body></html>"""
    out = Path(out) if out else BASE / "out" / "briefing.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[briefing] {out} ({out.stat().st_size / 1024:.1f} KB)")
    return out


def main():
    ap = argparse.ArgumentParser(description="数据简报生成(D1-29)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    generate_html(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
