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

    # S2.1-A: 当日摘要(最热/最冷 Top5)
    latest = dates[-1] if dates else None
    top5, bot5 = [], []
    if latest:
        ranked = conn.execute(
            "SELECT sc.code, s.name, sc.score, sc.rank_pct FROM sector_scores sc "
            "JOIN sectors s ON sc.code=s.code WHERE sc.date=? ORDER BY sc.score DESC",
            (latest,)).fetchall()
        top5 = [{"c": r[0], "n": r[1], "s": round(r[2] or 0), "r": round(r[3] or 0)}
                for r in ranked[:5]]
        bot5 = [{"c": r[0], "n": r[1], "s": round(r[2] or 0), "r": round(r[3] or 0)}
                for r in sorted(ranked, key=lambda x: x[2] or 0)[:5]]

    # S2.2-A: 申万一级分组(code index → sw1 name)
    sw1_groups = {}
    sw1_map = dict(conn.execute("SELECT bk_code, sw1_name FROM sw1_map").fetchall())
    for ci, c in enumerate(codes):
        sw1 = sw1_map.get(c, "")
        sw1_groups.setdefault(sw1, []).append(ci)

    conn.close()
    return {"dates": dates, "codes": codes, "names": [names.get(c, c) for c in codes],
            "mat": mat,
            "mkt": [[d, round(s, 1) if s is not None else None] for d, s in mkt],
            "comp": comp, "clabels": CLABELS, "corder": CORDER,
            "top5": top5, "bot5": bot5, "latest": latest,
            "sw1_groups": sw1_groups}


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
font:13px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
display:flex;flex-direction:column;height:100vh;overflow:hidden}
/* ── 顶栏 ── */
.hdr{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;
padding:8px 16px;background:var(--s);border-bottom:1px solid var(--g);flex-shrink:0}
.hdr h1{font-size:14px;margin:0}
.hdr .sub{color:var(--i2);font-size:11px}
.flt{display:flex;gap:6px;align-items:center}
/* ── 上行: 信息聚合层 ── */
.info-row{display:flex;gap:12px;padding:8px 16px;background:var(--s);
border-bottom:1px solid var(--g);flex-shrink:0;align-items:stretch}
.cards{flex:1;display:flex;gap:6px;overflow-x:auto}
.card{min-width:100px;padding:6px 10px;border-radius:8px;background:var(--p);
border:1px solid var(--g);cursor:pointer;transition:all .15s;text-align:center}
.card:hover{border-color:var(--m);transform:translateY(-1px)}
.card .nm{font-size:11px;color:var(--i2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .sc{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums}
.card .rk{font-size:10px;color:var(--m)}
.card.sel{border-width:2px}
.mkt-box{flex:0 0 200px;display:flex;flex-direction:column;align-items:center;justify-content:center;
border-left:1px solid var(--g);padding-left:14px}
.mkt-box .sc{font-size:28px;font-weight:700;line-height:1.1}
.mkt-box .lb{font-size:11px;color:var(--i2)}
#mktSpark{width:180px;height:36px;margin-top:2px}
/* ── 滑块行 ── */
#slider{margin:4px 16px;background:var(--s);border:1px solid var(--g);border-radius:8px;
padding:2px 8px;flex-shrink:0}
/* ── 下行: 分析层 ── */
.chart-row{flex:1;display:flex;gap:8px;padding:4px 16px 8px;min-height:0}
#rank{flex:6;background:var(--s);border:1px solid var(--g);border-radius:10px;min-width:0}
#curve{flex:4;background:var(--s);border:1px solid var(--g);border-radius:10px;min-width:0;position:relative}
#side{position:absolute;right:6px;top:6px;width:250px;max-height:calc(100% - 12px);overflow-y:auto;
background:var(--s);border:1px solid var(--g);border-radius:8px;padding:10px 12px;display:none;z-index:10;
box-shadow:0 2px 8px rgba(0,0,0,0.08)}
#side h3{margin:0 0 2px;font-size:12px}
#side .date{color:var(--m);font-size:10px;margin-bottom:6px}
table{border-collapse:collapse;width:100%}
th,td{padding:1px 3px;border-bottom:1px solid var(--g);font-size:10px;white-space:nowrap}
th{color:var(--m);font-weight:500}
td:first-child{text-align:left}
.track{position:relative;height:7px;background:var(--g);border-radius:2px;margin:1px 0}
.track::after{content:"";position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--a)}
.fill{position:absolute;top:1px;bottom:1px;border-radius:2px}
.btn{border:1px solid var(--a);background:var(--s);color:var(--i);border-radius:6px;
padding:3px 10px;font-size:11px;cursor:pointer}
.search{border:1px solid var(--a);border-radius:6px;padding:3px 8px;font-size:11px;
background:var(--p);color:var(--i);width:110px}
</style>
</head>
<body>
<div class="hdr">
  <div><h1>板块温度计 <span class="sub" id="hInfo"></span></h1></div>
  <div class="flt">
    <input class="search" id="q" placeholder="搜索板块名">
    <button class="btn" id="btnAll">Top80</button>
    <button class="btn" id="btnHot">最热</button>
    <button class="btn" id="btnCold">最冷</button>
    <span style="border-left:1px solid var(--a);margin:0 4px"></span>
    <button class="btn" id="btnRankMode" style="font-weight:bold">排行</button>
    <button class="btn" id="btnHeatMode">热力图</button>
    <button class="btn" id="btnClear">清空对比</button>
    <button class="btn" id="themeToggle">◐</button>
  </div>
</div>
<div class="info-row">
  <div class="cards" id="hotCards"></div>
  <div class="cards" id="botCards"></div>
  <div class="mkt-box">
    <span class="lb">大盘情绪</span>
    <span class="sc" id="mktSc">--</span>
    <span class="lb" id="mktDate"></span>
    <div id="mktSpark"></div>
  </div>
</div>
<div id="slider"></div>
<div class="chart-row">
  <div id="rank"></div>
  <div style="flex:4;position:relative;min-width:0">
    <div id="curve" style="width:100%;height:100%"></div>
    <div id="side"></div>
  </div>
</div>
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
let curDay = N_D - 1, filter = "all", query = "";
let selected = [];   // 多板块对比: 已选板块的 code index 数组(≤5)
const PALETTE = ["#2a78d6","#e34948","#1baf7a","#eda100","#6250d6"];

const rankC = echarts.init(document.getElementById('rank'));
const curveC = echarts.init(document.getElementById('curve'));
const slideC = echarts.init(document.getElementById('slider'));
const sparkC = echarts.init(document.getElementById('mktSpark'));

function mktAt(di) {
  const d = D.dates[di];
  for (let i = D.mkt.length - 1; i >= 0; i--) if (D.mkt[i][0] <= d) return D.mkt[i];
  return null;
}
function scoreAt(di, ci) { return D.mat[di] ? D.mat[di][ci] : null; }

/* ── 大盘情绪更新 ── */
function updateMkt() {
  const m = mktAt(curDay);
  const el = document.getElementById('mktSc');
  if (!m || m[1] === null) { el.textContent = '--'; return; }
  el.textContent = (m[1] >= 0 ? '+' : '') + m[1];
  el.style.color = m[1] >= 0 ? 'var(--rd)' : 'var(--bl)';
  document.getElementById('mktDate').textContent = m[0];
  const recent = D.mkt.slice(-30);
  sparkC.setOption({
    animation: false,
    grid: { left: 0, right: 0, top: 2, bottom: 0 },
    xAxis: { type: 'category', show: false, data: recent.map(r => r[0]) },
    yAxis: { type: 'value', show: false, min: -100, max: 100 },
    series: [{ type: 'line', data: recent.map(r => r[1]), showSymbol: false,
      lineStyle: { width: 1.5, color: m[1] >= 0 ? '#e34948' : '#2a78d6' },
      itemStyle: { color: m[1] >= 0 ? '#e34948' : '#2a78d6' } }]
  }, true);
}

/* ── 信息卡 ── */
function renderCards() {
  const mkCard = (item, idx) => {
    const ci = D.codes.indexOf(item.c);
    const isSel = selected.includes(ci);
    const color = isSel ? PALETTE[selected.indexOf(ci)] : null;
    return `<div class="card ${isSel ? 'sel' : ''}" data-ci="${ci}"
      style="${color ? `border-color:${color}` : ''}">
      <div class="nm">${item.n}</div>
      <div class="sc" style="color:${item.s >= 0 ? 'var(--rd)' : 'var(--bl)'}">${item.s > 0 ? '+' : ''}${item.s}</div>
      <div class="rk">排名${item.r}%</div>
    </div>`;
  };
  const hot = document.getElementById('hotCards');
  const bot = document.getElementById('botCards');
  if (D.top5 && D.top5.length) {
    hot.innerHTML = D.top5.map(mkCard).join('');
    bot.innerHTML = D.bot5.map(mkCard).join('');
    hot.querySelectorAll('.card').forEach(c => c.onclick = () => toggleSel(parseInt(c.dataset.ci)));
    bot.querySelectorAll('.card').forEach(c => c.onclick = () => toggleSel(parseInt(c.dataset.ci)));
  }
}

/* ── 多板块 toggle ── */
function toggleSel(ci) {
  const idx = selected.indexOf(ci);
  if (idx >= 0) selected.splice(idx, 1);
  else {
    if (selected.length >= 5) selected.shift();
    selected.push(ci);
  }
  renderCards(); drawCurve();
  drawLeft();  // 高亮已选板块
}

/* ── 排行榜 ── */
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
  document.getElementById('hInfo').textContent = `${day} · ${N_C} 板块`;
  rankC.setOption({
    animation: false,
    title: { text: `温度排行 · ${day}`, left: 10, top: 6, textStyle: { fontSize: 12, color: '#52514e' } },
    tooltip: { trigger: 'item', formatter: p => {
      const it = items[p.dataIndex];
      const sel = it && selected.includes(it.ci);
      return `${p.name}<br/>温度 ${p.value > 0 ? '+' : ''}${p.value}${sel ? '<br/><b>已选对比</b>' : ''}`;
    }},
    grid: { left: 10, right: 40, top: 30, bottom: 8, containLabel: true },
    xAxis: { type: 'value', min: -105, max: 105, axisLabel: { color: '#898781', fontSize: 10 },
      splitLine: { lineStyle: { color: '#e1e0d9' } } },
    yAxis: { type: 'category', inverse: true,
      data: items.map(i => i.name),
      axisLabel: { color: '#52514e', fontSize: 10,
        formatter: v => selected.some(ci => D.names[ci] === v) ? `▶ ${v}` : v },
      axisTick: { show: false }, axisLine: { lineStyle: { color: '#c3c2b7' } } },
    series: [{ type: 'bar', data: items.map(i => {
        const selIdx = selected.indexOf(i.ci);
        const isSel = selIdx >= 0;
        return { value: i.v, itemStyle: { color: isSel ? PALETTE[selIdx]
          : i.v >= 0 ? `rgba(227,73,72,${Math.min(0.25 + Math.abs(i.v) / 130, 0.95)})`
                     : `rgba(42,120,214,${Math.min(0.25 + Math.abs(i.v) / 130, 0.95)})` } };
      }),
      barMaxWidth: 10, barCategoryGap: '18%',
      label: { show: true, position: 'right', fontSize: 9, color: '#898781',
        formatter: p => (p.value > 0 ? '+' : '') + p.value } }]
  }, true);
  rankC.off('click');
  rankC.on('click', p => {
    const it = items[p.dataIndex];
    if (it) toggleSel(it.ci);
  });
}

/* ── 多板块曲线 ── */
function drawCurve() {
  const side = document.getElementById('side');
  if (selected.length === 0) {
    curveC.clear();
    curveC.setOption({ title: { text: '点击排行或信息卡\n选择板块对比(≤5个)', left: 'center', top: 'center',
      textStyle: { color: '#898781', fontSize: 13 } }, xAxis: {show:false}, yAxis: {show:false} });
    side.style.display = 'none';
    return;
  }
  const series = selected.map((ci, si) => {
    const data = [];
    for (let di = 0; di < N_D; di++) {
      const v = scoreAt(di, ci);
      data.push(v === null ? null : [D.dates[di], v]);
    }
    return { name: D.names[ci], type: 'line', data: data, showSymbol: false,
      lineStyle: { width: si === 0 ? 2 : 1.6, color: PALETTE[si] },
      itemStyle: { color: PALETTE[si] },
      markLine: si === 0 ? { silent: true, symbol: 'none', label: { show: false },
        lineStyle: { color: '#c3c2b7', type: 'dashed', width: 1 },
        data: [{ yAxis: 0 }, { yAxis: 60 }, { yAxis: -60 }] } : undefined,
      markArea: si === 0 ? { silent: true, itemStyle: { color: 'rgba(227,73,72,0.04)' },
        data: [[{ yAxis: 60 }, { yAxis: 100 }], [{ yAxis: -100 }, { yAxis: -60 }]] } : undefined };
  });
  curveC.setOption({
    animation: false,
    legend: { top: 6, left: 10, itemWidth: 14, itemHeight: 8,
      textStyle: { fontSize: 10, color: '#52514e' } },
    tooltip: { trigger: 'axis', valueFormatter: v => v === null || v === undefined ? '--' : (v > 0 ? '+' : '') + v },
    grid: { left: 40, right: 12, top: 32, bottom: 40 },
    xAxis: { type: 'time', axisLabel: { color: '#898781', fontSize: 9 } },
    yAxis: { type: 'value', min: -105, max: 105,
      axisLabel: { color: '#898781', fontSize: 9 }, splitLine: { lineStyle: { color: '#e1e0d9' } } },
    dataZoom: [{ type: 'inside' }],
    series: series
  }, true);
  drawSide();
}

/* ── 分量明细(跟随最后选中的板块) ── */
function drawSide() {
  const side = document.getElementById('side');
  if (selected.length === 0) { side.style.display = 'none'; return; }
  const lastCi = selected[selected.length - 1];
  const c = D.codes[lastCi];
  const cd = D.comp[c];
  if (!cd) { side.style.display = 'none'; return; }
  const latest = Object.keys(cd).sort().pop();
  const comps = cd[latest];
  const order = D.corder.filter(k => comps[k] !== undefined && comps[k] !== null);
  let h = `<h3>${D.names[lastCi]}</h3><div class="date">${latest} 分量明细</div><table>
<tr><th>分量</th><th>分位分</th></tr>`;
  for (const k of order) {
    const v = comps[k];
    const w = Math.min(Math.abs(v), 100) / 2;
    const pos = v >= 0 ? `left:50%;width:${w}%;background:var(--rd)`
                       : `left:${50 - w}%;width:${w}%;background:var(--bl)`;
    h += `<tr><td>${D.clabels[k] || k}</td><td style="min-width:80px">
<div class="track"><div class="fill" style="${pos}"></div></div></td>
<td style="text-align:right;color:${v >= 0 ? 'var(--rd)' : 'var(--bl)'}">${v > 0 ? '+' : ''}${v}</td></tr>`;
  }
  h += '</table>';
  side.innerHTML = h;
  side.style.display = 'block';
}

/* ── 日期滑块 ── */
slideC.setOption({
  animation: false,
  grid: { left: 130, right: 130, top: 4, bottom: 22 },
  xAxis: { show: false, min: 0, max: N_D - 1 },
  yAxis: { show: false, min: 0, max: 1 },
  graphic: [],
  dataZoom: [{
    type: 'slider', xAxisIndex: 0, start: Math.max(0, (N_D - 31) / N_D * 100), end: 100,
    height: 16, bottom: 2, borderColor: '#c3c2b7',
    fillerColor: 'rgba(42,120,214,0.12)',
    labelFormatter: v => D.dates[Math.round(v / 100 * (N_D - 1))]?.slice(5) || ''
  }]
});
function drawLeft() { if (mode === 'heat') drawHeatmap(); else drawRank(); }
slideC.on('datazoom', () => {
  const opt = slideC.getOption();
  const z = opt.dataZoom[0];
  const idx = Math.round(z.end / 100 * (N_D - 1));
  const newDay = Math.max(0, Math.min(N_D - 1, idx - 1));
  if (newDay !== curDay) { curDay = newDay; drawLeft(); updateMkt(); }
});

/* ── 事件 ── */
document.getElementById('btnAll').onclick = () => { filter = 'all'; setMode('rank'); };
document.getElementById('btnHot').onclick = () => { filter = 'hot'; setMode('rank'); };
document.getElementById('btnCold').onclick = () => { filter = 'cold'; setMode('rank'); };

// S2.2-A: 热力图模式
let mode = 'rank';
const btnR = document.getElementById('btnRankMode');
const btnH = document.getElementById('btnHeatMode');
function setMode(m) {
  mode = m;
  btnR.style.fontWeight = m === 'rank' ? 'bold' : 'normal';
  btnH.style.fontWeight = m === 'heat' ? 'bold' : 'normal';
  if (m === 'heat') drawHeatmap(); else drawRank();
}
btnR.onclick = () => setMode('rank');
btnH.onclick = () => setMode('heat');

function drawHeatmap() {
  const hmDays = 60;
  const dStart = Math.max(0, N_D - hmDays);
  const hmDates = D.dates.slice(dStart);
  // 按申万一级排序板块
  const groups = D.sw1_groups || {};
  const sortedCis = [];
  const yLabels = [];
  const swLabels = [];
  const swNames = Object.keys(groups).sort();
  for (const sw of swNames) {
    if (sw) swLabels.push({ y: yLabels.length + sortedCis.length / 2, name: sw });
    for (const ci of groups[sw]) { sortedCis.push(ci); yLabels.push(D.names[ci]); }
    yLabels.push(''); sortedCis.push(-1);  // 组间空行
  }
  yLabels.pop(); sortedCis.pop();  // 去掉最后一行的空行
  const hmData = [];
  for (let xi = 0; xi < hmDates.length; xi++) {
    for (let yi = 0; yi < sortedCis.length; yi++) {
      const ci = sortedCis[yi];
      if (ci < 0) continue;
      const v = scoreAt(dStart + xi, ci);
      if (v !== null) hmData.push([xi, yi, v]);
    }
  }
  rankC.setOption({
    animation: false,
    title: { text: `热力图 · ${hmDates[0]?.slice(5)} ~ ${hmDates[hmDates.length-1]?.slice(5)}`,
             left: 10, top: 6, textStyle: { fontSize: 12, color: '#52514e' } },
    tooltip: { position: 'top', formatter: p => {
      const ci = sortedCis[p.data[1]];
      if (ci < 0) return '';
      const sw = Object.keys(groups).find(s => groups[s]?.includes(ci));
      return `${D.names[ci]}<br/>${hmDates[p.data[0]]} · 温度 ${p.data[2] > 0 ? '+' : ''}${p.data[2]}${sw ? '<br/>SW: ' + sw : ''}`;
    }},
    grid: { left: 10, right: 60, top: 32, bottom: 50, containLabel: true },
    xAxis: { type: 'category', data: hmDates.map(d => d.slice(5)),
      axisLabel: { color: '#898781', fontSize: 9, rotate: 45 },
      splitArea: { show: true, areaStyle: { color: ['rgba(0,0,0,0.02)', 'rgba(0,0,0,0.04)'] } } },
    yAxis: { type: 'category', data: yLabels,
      axisLabel: { color: '#52514e', fontSize: 8 },
      splitArea: { show: true, areaStyle: { color: ['rgba(0,0,0,0.02)', 'rgba(0,0,0,0.04)'] } } },
    visualMap: { min: -100, max: 100, calculable: true, orient: 'horizontal',
      left: 'center', bottom: 0, itemHeight: 80, itemWidth: 12,
      textStyle: { color: '#898781', fontSize: 9 },
      inRange: { color: ['#2a78d6', '#e1e0d9', '#e34948'] } },
    dataZoom: [{ type: 'inside', xAxisIndex: 0 }, { type: 'slider', xAxisIndex: 0,
      bottom: 22, height: 14, borderColor: '#c3c2b7', fillerColor: 'rgba(42,120,214,0.1)' }],
    series: [{ type: 'heatmap', data: hmData,
      itemStyle: { borderColor: 'rgba(0,0,0,0.1)', borderWidth: 0.5 },
      progressive: 5000, progressiveThreshold: 10000,
      emphasis: { itemStyle: { shadowBlur: 4, shadowColor: 'rgba(0,0,0,0.3)' } } }]
  }, true);
  rankC.off('click');
  rankC.on('click', p => {
    const ci = sortedCis[p.data[1]];
    if (ci >= 0) toggleSel(ci);
  });
}
document.getElementById('btnClear').onclick = () => { selected = []; renderCards(); drawCurve(); drawLeft(); };
document.getElementById('q').addEventListener('input', e => { query = e.target.value.trim(); drawLeft(); });
document.getElementById('themeToggle').onclick = () => {
  const h = document.documentElement;
  const t = h.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  h.setAttribute('data-theme', t);
};
window.addEventListener('resize', () => { rankC.resize(); curveC.resize(); slideC.resize(); sparkC.resize(); });

renderCards(); drawLeft(); updateMkt(); drawCurve();
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
    ap.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    args = ap.parse_args()
    out = generate_html(years=args.years, out=args.out)
    if args.open:
        import webbrowser
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
