# -*- coding: utf-8 -*-
"""板块温度视图生成器 (S7): 自包含 HTML (内嵌 ECharts + 全量数据)

用法:
  python sector_viewer.py [--out path] [--years 2] [--topN 30]

视图四区: 大盘对照条 / 日期滑块+温度排行 / 板块曲线 / 分量明细
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "sentiment.db"
ECHARTS = BASE / "vendor" / "echarts.min.js"

C = {"surface": "#fcfcfb", "page": "#f9f9f7", "ink": "#0b0b0b", "ink2": "#52514e",
     "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
     "blue": "#2a78d6", "red": "#e34948", "aqua": "#1baf7a"}

# 分量中文名(与 core/config.yaml sector_weights 对应)
CLABELS = {"rsi": "RSI", "chg": "当日涨幅", "gap": "跳空缺口", "kr": "K线实体",
           "vol_dir": "量能×方向", "vr": "量比趋势", "bias20": "20日乖离",
           "bias60": "60日乖离", "pos": "价格位置", "amp": "振幅×方向",
           "zt": "板块涨停", "zt_share": "涨停占比", "ud_ratio": "涨跌家数比",
           "fflow": "主力净流入"}
CORDER = ["rsi", "chg", "gap", "kr", "vol_dir", "vr", "bias20", "bias60",
          "pos", "amp", "zt", "zt_share", "ud_ratio", "fflow"]


def build_payload(years=2):
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    # 分数矩阵: 近 years 年
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM sector_scores ORDER BY date DESC LIMIT ?", ((years + 1) * 250,))]
    dates.reverse()
    dset = set(dates)
    ph = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT date,code,score,rank_pct FROM sector_scores WHERE date IN ({ph})", dates).fetchall()
    codes = sorted({r[1] for r in rows})
    names = dict(conn.execute("SELECT code,name FROM sectors").fetchall())

    idx_d = {d: i for i, d in enumerate(dates)}
    idx_c = {c: i for i, c in enumerate(codes)}
    mat = [[None] * len(codes) for _ in range(len(dates))]
    for d, c, s, _rp in rows:
        mat[idx_d[d]][idx_c[c]] = round(s)

    # 大盘情绪对照
    mkt = conn.execute(
        "SELECT date,score FROM sentiment ORDER BY date DESC LIMIT ?",
        ((years + 1) * 250,)).fetchall()
    mkt.reverse()

    # 最近5日全板块分量明细
    recent5 = dates[-5:]
    comp = {}
    for d in recent5:
        for c, j in conn.execute(
                "SELECT code,components FROM sector_scores WHERE date=? AND components IS NOT NULL", (d,)):
            comp.setdefault(c, {})[d] = json.loads(j)

    conn.close()
    return {"dates": dates, "codes": codes, "names": [names.get(c, c) for c in codes],
            "mat": mat,
            "mkt": [[d, round(s, 1) if s is not None else None] for d, s in mkt],
            "comp": comp, "clabels": CLABELS, "corder": CORDER}


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>板块温度 · stock_analyze</title>
<style>
:root[data-theme="light"]{--s:#fcfcfb;--p:#f9f9f7;--i:#0b0b0b;--i2:#52514e;--m:#898781;
--g:#e1e0d9;--a:#c3c2b7;--bl:#2a78d6;--rd:#e34948;--aq:#1baf7a}
:root[data-theme="dark"]{--s:#1a1a19;--p:#0d0d0d;--i:#f0efec;--i2:#c3c2b7;--m:#898781;
--g:#2c2c2a;--a:#383835;--bl:#3987e5;--rd:#e66767;--aq:#199e70}
*{box-sizing:border-box}
body{margin:0;background:var(--p);color:var(--i);
font:13px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.hdr{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;
padding:10px 18px;background:var(--s);border-bottom:1px solid var(--g)}
.hdr h1{font-size:15px;margin:0}
.hdr .sub{color:var(--i2);font-size:12px}
.mktbar{display:flex;align-items:center;gap:12px}
.mktbar .sc{font-size:26px;font-weight:700}
#rank{width:46%;height:640px;display:inline-block;vertical-align:top;background:var(--s);
border:1px solid var(--g);border-radius:10px;margin:10px 0 0 12px}
#curve{width:53%;height:640px;display:inline-block;vertical-align:top;background:var(--s);
border:1px solid var(--g);border-radius:10px;margin:10px 0 0 8px}
#slider{margin:6px 12px;background:var(--s);border:1px solid var(--g);border-radius:10px;padding:4px 10px}
#side{position:fixed;right:12px;top:120px;width:270px;max-height:70vh;overflow-y:auto;
background:var(--s);border:1px solid var(--g);border-radius:10px;padding:12px 14px;display:none;z-index:10}
#side h3{margin:0 0 4px;font-size:13px}
#side .date{color:var(--m);font-size:11px;margin-bottom:8px}
table{border-collapse:collapse;width:100%}
th,td{padding:2px 3px;border-bottom:1px solid var(--g);font-size:11px;white-space:nowrap}
th{color:var(--m);font-weight:500}
td:first-child{text-align:left}
.track{position:relative;height:8px;background:var(--g);border-radius:2px;margin:2px 0}
.track::after{content:"";position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--a)}
.fill{position:absolute;top:1px;bottom:1px;border-radius:2px}
.btn{border:1px solid var(--a);background:var(--s);color:var(--i);border-radius:7px;
padding:4px 12px;font-size:12px;cursor:pointer}
.search{border:1px solid var(--a);border-radius:7px;padding:4px 10px;font-size:12px;
background:var(--p);color:var(--i);width:120px}
.flt{display:flex;gap:8px;align-items:center}
</style>
</head>
<body>
<div class="hdr">
  <div><h1>板块温度计 <span class="sub" id="hInfo"></span></h1></div>
  <div class="mktbar"><span class="sub">大盘情绪</span><span class="sc" id="mktSc">--</span>
  <span class="sub" id="mktDate"></span></div>
  <div class="flt">
    <input class="search" id="q" placeholder="搜索板块名">
    <button class="btn" id="btnAll">Top 80</button>
    <button class="btn" id="btnHot">最热</button>
    <button class="btn" id="btnCold">最冷</button>
    <button class="btn" id="themeToggle">◐</button>
  </div>
</div>
<div id="slider"></div>
<div id="rank"></div><div id="curve"></div>
<div id="side"></div>
<script>
window.onerror = function(msg, src, line, col) {
  var d = document.createElement("div");
  d.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:999;background:#d03b3b;color:#fff;padding:6px 12px;font:12px monospace";
  d.textContent = "JS错误: " + msg + " @" + line + ":" + col;
  document.body.appendChild(d);
  return false;
};
</script>
<script>__ECHARTS__</script>
<script>
const D = __PAYLOAD__;
const N_D = D.dates.length, N_C = D.codes.length;
let curDay = N_D - 1, curCode = -1, filter = "all", query = "";

const rankC = echarts.init(document.getElementById('rank'));
const curveC = echarts.init(document.getElementById('curve'));
const slideC = echarts.init(document.getElementById('slider'));

function mktAt(di) {
  const d = D.dates[di];
  for (let i = D.mkt.length - 1; i >= 0; i--) if (D.mkt[i][0] <= d) return D.mkt[i];
  return null;
}
function scoreAt(di, ci) { return D.mat[di] ? D.mat[di][ci] : null; }

function updateMkt() {
  const m = mktAt(curDay);
  const el = document.getElementById('mktSc');
  if (!m || m[1] === null) { el.textContent = '--'; return; }
  el.textContent = (m[1] >= 0 ? '+' : '') + m[1];
  el.style.color = m[1] >= 0 ? 'var(--rd)' : 'var(--bl)';
  document.getElementById('mktDate').textContent = m[0];
}

function rankData() {
  const row = D.mat[curDay] || [];
  const items = [];
  for (let ci = 0; ci < N_C; ci++) {
    const nm = D.names[ci];
    if (query && !nm.includes(query)) continue;
    const v = row[ci];
    if (v === null) continue;
    items.push({ name: nm, v: v, ci: ci });
  }
  items.sort((a, b) => b.v - a.v);
  let show = items;
  if (filter === 'hot') show = items.slice(0, 40);
  else if (filter === 'cold') show = items.slice(-40).reverse();
  else show = items.slice(0, 80);
  return show;
}

function drawRank() {
  const items = rankData();
  const day = D.dates[curDay];
  document.getElementById('hInfo').textContent =
    `${day} · ${items.length > 0 ? N_C : 0} 板块`;
  rankC.setOption({
    animation: false,
    title: { text: `温度排行 · ${day}`, left: 10, top: 6, textStyle: { fontSize: 13, color: '#52514e' } },
    tooltip: { trigger: 'item', formatter: p => `${p.name}<br/>温度 ${p.value > 0 ? '+' : ''}${p.value}` },
    grid: { left: 10, right: 40, top: 34, bottom: 10, containLabel: true },
    xAxis: { type: 'value', min: -105, max: 105, axisLabel: { color: '#898781', fontSize: 10 },
      splitLine: { lineStyle: { color: '#e1e0d9' } } },
    yAxis: { type: 'category', inverse: true,
      data: items.map(i => i.name),
      axisLabel: { color: '#52514e', fontSize: 10.5 },
      axisTick: { show: false }, axisLine: { lineStyle: { color: '#c3c2b7' } } },
    series: [{ type: 'bar', data: items.map(i => ({
        value: i.v, itemStyle: { color: i.v >= 0
          ? `rgba(227,73,72,${Math.min(0.25 + Math.abs(i.v) / 130, 0.95)})`
          : `rgba(42,120,214,${Math.min(0.25 + Math.abs(i.v) / 130, 0.95)})` } })),
      barMaxWidth: 11, barCategoryGap: '18%',
      label: { show: true, position: 'right', fontSize: 9.5, color: '#898781',
        formatter: p => (p.value > 0 ? '+' : '') + p.value } }]
  }, true);
  rankC.off('click');
  rankC.on('click', p => {
    const it = items[p.dataIndex];
    if (it) selectCode(it.ci);
  });
}

function drawCurve() {
  if (curCode < 0) {
    curveC.clear();
    curveC.setOption({ title: { text: '点击左侧板块查看温度曲线', left: 'center', top: 'center',
      textStyle: { color: '#898781', fontSize: 14 } }, xAxis: {show:false}, yAxis: {show:false} });
    return;
  }
  const nm = D.names[curCode];
  const data = [];
  for (let di = 0; di < N_D; di++) {
    const v = scoreAt(di, curCode);
    data.push(v === null ? null : [D.dates[di], v]);
  }
  curveC.setOption({
    animation: false,
    title: { text: `${nm} · 板块温度`, left: 10, top: 6, textStyle: { fontSize: 13, color: '#0b0b0b' } },
    tooltip: { trigger: 'axis', valueFormatter: v => v === null ? '--' : (v > 0 ? '+' : '') + v },
    grid: { left: 44, right: 16, top: 34, bottom: 46 },
    xAxis: { type: 'time', axisLabel: { color: '#898781', fontSize: 10 } },
    yAxis: { type: 'value', min: -105, max: 105,
      axisLabel: { color: '#898781' }, splitLine: { lineStyle: { color: '#e1e0d9' } } },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 8, height: 18,
      borderColor: '#e1e0d9', fillerColor: 'rgba(42,120,214,0.08)' }],
    series: [{ type: 'line', data: data, showSymbol: false,
      lineStyle: { width: 1.8, color: '#2a78d6' }, itemStyle: { color: '#2a78d6' },
      markLine: { silent: true, symbol: 'none', label: { show: false },
        lineStyle: { color: '#c3c2b7', type: 'dashed', width: 1 },
        data: [{ yAxis: 0 }, { yAxis: 60 }, { yAxis: -60 }] },
      markArea: { silent: true, itemStyle: { color: 'rgba(227,73,72,0.05)' },
        data: [[{ yAxis: 60 }, { yAxis: 100 }], [{ yAxis: -100 }, { yAxis: -60 }]] } }]
  }, true);
  drawSide();
}

function drawSide() {
  const side = document.getElementById('side');
  if (curCode < 0) { side.style.display = 'none'; return; }
  const c = D.codes[curCode];
  const cd = D.comp[c];
  if (!cd) { side.style.display = 'none'; return; }
  const latest = Object.keys(cd).sort().pop();
  const comps = cd[latest];
  const order = D.corder.filter(k => comps[k] !== undefined && comps[k] !== null);
  let h = `<h3>${D.names[curCode]}</h3><div class="date">${latest} 分量明细</div><table>
<tr><th>分量</th><th>分位分</th></tr>`;
  for (const k of order) {
    const v = comps[k];
    const w = Math.min(Math.abs(v), 100) / 2;
    const pos = v >= 0 ? `left:50%;width:${w}%;background:var(--rd)`
                       : `left:${50 - w}%;width:${w}%;background:var(--bl)`;
    h += `<tr><td>${D.clabels[k] || k}</td><td style="min-width:90px">
<div class="track"><div class="fill" style="${pos}"></div></div></td>
<td style="text-align:right;color:${v >= 0 ? 'var(--rd)' : 'var(--bl)'}">${v > 0 ? '+' : ''}${v}</td></tr>`;
  }
  h += '</table>';
  side.innerHTML = h;
  side.style.display = 'block';
}

function selectCode(ci) {
  curCode = ci;
  drawCurve();
}

slideC.setOption({
  animation: false,
  grid: { left: 130, right: 130, top: 4, bottom: 22 },
  xAxis: { show: false, min: 0, max: N_D - 1 },
  yAxis: { show: false, min: 0, max: 1 },
  graphic: [],
  dataZoom: [{
    type: 'slider', xAxisIndex: 0, start: Math.max(0, (N_D - 31) / N_D * 100), end: 100,
    height: 18, bottom: 2, borderColor: '#c3c2b7',
    fillerColor: 'rgba(42,120,214,0.12)',
    labelFormatter: v => D.dates[Math.round(v / 100 * (N_D - 1))]?.slice(5) || ''
  }]
});
slideC.on('datazoom', () => {
  const opt = slideC.getOption();
  const z = opt.dataZoom[0];
  const span = (z.end - z.start) / 100 * (N_D - 1);
  const idx = Math.round(z.end / 100 * (N_D - 1));
  const newDay = Math.max(0, Math.min(N_D - 1, idx - 1));   // 对齐滑块右端前1日
  if (newDay !== curDay) { curDay = newDay; drawRank(); updateMkt(); }
});

document.getElementById('btnAll').onclick = () => { filter = 'all'; drawRank(); };
document.getElementById('btnHot').onclick = () => { filter = 'hot'; drawRank(); };
document.getElementById('btnCold').onclick = () => { filter = 'cold'; drawRank(); };
document.getElementById('q').addEventListener('input', e => { query = e.target.value.trim(); drawRank(); });
document.getElementById('themeToggle').onclick = () => {
  const h = document.documentElement;
  const t = h.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  h.setAttribute('data-theme', t);
};
window.addEventListener('resize', () => { rankC.resize(); curveC.resize(); slideC.resize(); });

drawRank(); updateMkt(); drawCurve();
</script>
</body>
</html>
"""


def generate_html(years=2, out=None):
    payload = build_payload(years)
    js = ECHARTS.read_text(encoding="utf-8") if ECHARTS.exists() else ""
    html = (HTML
            .replace("__ECHARTS__", js)
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False,
                                                separators=(",", ":"))))
    out = Path(out) if out else (BASE / "out" / "sector_heat.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[sector_viewer] {out}  ({out.stat().st_size / 1e6:.2f} MB, "
          f"{len(payload['codes'])} 板块 × {len(payload['dates'])} 日, "
          f"最新 {payload['dates'][-1]})")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--years", type=int, default=2)
    args = ap.parse_args()
    generate_html(years=args.years, out=args.out)


if __name__ == "__main__":
    main()
