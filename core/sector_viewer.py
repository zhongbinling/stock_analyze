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
        mat[idx_d[d]][idx_c[c]] = None if s is None else round(s)

    # 大盘情绪对照(S3-F: 拉取15年, 供右侧曲线独立缩放到更长区间)
    mkt = conn.execute(
        "SELECT date,score FROM sentiment ORDER BY date DESC LIMIT 4000").fetchall()
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

    # S3-E: 申万二级分组 + SW1/SW2聚合行索引
    sw2_groups = {}
    try:
        sw2_map = dict(conn.execute("SELECT bk_code, sw2_name FROM sw2_map").fetchall())
        for ci, c in enumerate(codes):
            sw2 = sw2_map.get(c, "")
            if sw2:
                sw2_groups.setdefault(sw2, []).append(ci)
    except Exception:
        pass
    # SW1_/SW2_ 聚合行的 code index(用于一级/二级模式)
    sw1_agg_cis = [ci for ci, c in enumerate(codes) if c.startswith("SW1_")]
    sw2_agg_cis = [ci for ci, c in enumerate(codes) if c.startswith("SW2_")]
    bk_cis = [ci for ci, c in enumerate(codes) if c.startswith("BK")]
    level_data = {"L1": sw1_agg_cis, "L2": sw2_agg_cis, "L3": bk_cis}

    # SW2组的父级(SW1)归属
    sw2_to_sw1 = {}
    try:
        for sw2_name, sw1_name in conn.execute("SELECT DISTINCT sw2_name, sw1_name FROM sw2_map"):
            sw2_to_sw1[sw2_name] = sw1_name
    except Exception:
        pass

    conn.close()

    def disp_name(c):
        n = names.get(c)
        if n:
            return n
        return c.replace("SW1_", "").replace("SW2_", "")

    return {"dates": dates, "codes": codes, "names": [disp_name(c) for c in codes],
            "mat": mat,
            "mkt": [[d, round(s, 1) if s is not None else None] for d, s in mkt],
            "comp": comp, "clabels": CLABELS, "corder": CORDER,
            "top5": top5, "bot5": bot5, "latest": latest,
            "sw1_groups": sw1_groups, "sw2_groups": sw2_groups,
            "level_data": level_data, "sw2_to_sw1": sw2_to_sw1}


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
  <div style="display:flex;align-items:center;gap:8px">
    <h1>板块温度计</h1>
    <span class="sub" id="hInfo"></span>
  </div>
  <div class="flt" style="flex:1;justify-content:center">
    <input class="search" id="q" placeholder="搜索板块名">
    <select class="input" id="swFilter" style="width:110px;font-size:11px;padding:3px 4px"><option value="">全部申万一级</option></select>
    <select class="input" id="sortMode" style="width:90px;font-size:11px;padding:3px 4px">
      <option value="score">当日温度</option>
      <option value="chg">今日变化</option>
      <option value="ext">区间极值</option>
    </select>
    <span style="border-left:1px solid var(--a);margin:0 2px"></span>
    <button class="btn" id="lvL1" style="padding:2px 6px;font-size:10px">一级</button>
    <button class="btn" id="lvL2" style="padding:2px 6px;font-size:10px">二级</button>
    <button class="btn" id="lvL3" style="padding:2px 6px;font-size:10px;font-weight:bold">三级</button>
  </div>
  <div class="flt">
    <span style="border-left:1px solid var(--a);margin:0 4px"></span>
    <button class="btn" id="btnRankMode" style="font-weight:bold">排行</button>
    <button class="btn" id="btnHeatMode">热力图</button>
    <button class="btn" id="btnClear">清空对比</button>
    <button class="btn" id="themeToggle">◐</button>
  </div>
</div>
<div class="info-row">
  <div class="cards" id="hotCards" style="overflow-x:auto;white-space:nowrap"></div>
  <div class="cards" id="botCards" style="overflow-x:auto;white-space:nowrap"></div>
  <div class="mkt-box">
    <span class="lb">大盘情绪</span>
    <span class="sc" id="mktSc">--</span>
    <span class="lb" id="mktDate"></span>
    <div id="mktSpark"></div>
  </div>
</div>
<div id="slider"></div>
<div style="display:flex;gap:6px;padding:2px 16px 4px;flex-shrink:0;align-items:center">
  <span style="font-size:10px;color:var(--m)">时间:</span>
  <button class="btn" id="tq5" style="padding:2px 8px;font-size:10px">近5日</button>
  <button class="btn" id="tq20" style="padding:2px 8px;font-size:10px">近20日</button>
  <button class="btn" id="tq60" style="padding:2px 8px;font-size:10px">近60日</button>
  <button class="btn" id="tq120" style="padding:2px 8px;font-size:10px;font-weight:bold">近120日</button>
  <button class="btn" id="tq250" style="padding:2px 8px;font-size:10px">近250日</button>
  <button class="btn" id="tqAll" style="padding:2px 8px;font-size:10px">全部</button>
</div>
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
let curDay = N_D - 1, query = "";
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
      <div class="rk">分位${item.r}%</div>
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
  const groups = levelVal === 'L2' ? (D.sw2_groups || {}) : (D.sw1_groups || {});
  const levelCis = (D.level_data && D.level_data[levelVal]) || null;
  const items = [];
  for (let ci = 0; ci < N_C; ci++) {
    if (levelCis && !levelCis.includes(ci)) continue;
    const nm = D.names[ci];
    if (query && !nm.includes(query)) continue;
    if (swFilterVal) {
      const sw = Object.keys(groups).find(s => groups[s]?.includes(ci)) || '';
      if (sw !== swFilterVal) continue;
    }
    const v = row[ci];
    if (v === null) continue;
    items.push({ name: nm, v: v, ci: ci });
  }
  // S3-B: 排序模式
  if (sortModeVal === 'chg') {
    const prev = D.mat[curDay - 1] || [];
    items.sort((a, b) => ((row[b.ci]||0) - (prev[b.ci]||0)) - ((row[a.ci]||0) - (prev[a.ci]||0)));
  } else if (sortModeVal === 'ext') {
    const hmDays = 60;
    const dStart = Math.max(0, curDay - hmDays);
    const ext = (ci) => {
      let m = 0;
      for (let di = dStart; di <= curDay; di++) {
        const v = D.mat[di] ? D.mat[di][ci] : null;
        if (v !== null) m = Math.abs(v) > Math.abs(m) ? v : m;
      }
      return m;
    };
    items.sort((a, b) => Math.abs(ext(b.ci)) - Math.abs(ext(a.ci)));
  } else {
    items.sort((a, b) => b.v - a.v);
  }
  return items.slice(0, 600);  // S3-F: 提高上限, yAxis dataZoom 控制显示数量
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
  enableRankScroll(items);  // S3-F: y轴滚动
}

/* ── 多板块曲线 ── */
// S3-F: 曲线缩放态 — 重绘时保留用户当前缩放; 首次按 hmDaysGlobal 对齐热力图窗口
function curveTotalDays() {
  return Math.max((D.mkt || []).length, N_D);
}
function curveZoomOpt() {
  let z = null;
  try { const o = curveC.getOption(); z = o && o.dataZoom && o.dataZoom[0]; } catch (e) { z = null; }
  if (z && typeof z.start === 'number' && (z.start > 0.01 || z.end < 99.99)) {
    return [{ type: 'inside', start: z.start, end: z.end }];
  }
  return [{ type: 'inside',
    start: Math.max(0, 100 - (hmDaysGlobal / curveTotalDays()) * 100), end: 100 }];
}
function drawCurve() {
  const side = document.getElementById('side');
  if (selected.length === 0) {
    // S3-F: 默认显示大盘情绪分曲线
    const mktData = (D.mkt || []).map(m => m[1] === null ? null : [m[0], m[1]]);
    curveC.setOption({
      animation: false,
      title: { text: '大盘情绪分(点击左侧板块可叠加对比)', left: 10, top: 6,
        textStyle: { fontSize: 12, color: '#52514e' } },
      tooltip: { trigger: 'axis', valueFormatter: v => v === null ? '--' : (v > 0 ? '+' : '') + v },
      grid: { left: 40, right: 12, top: 30, bottom: 40 },
      xAxis: { type: 'time', axisLabel: { color: '#898781', fontSize: 9 } },
      yAxis: { type: 'value', min: -105, max: 105,
        axisLabel: { color: '#898781', fontSize: 9 }, splitLine: { lineStyle: { color: '#e1e0d9' } } },
      dataZoom: curveZoomOpt(),
      series: [{ name: '大盘情绪分', type: 'line', data: mktData, showSymbol: false,
        lineStyle: { width: 2, color: '#2a78d6' }, itemStyle: { color: '#2a78d6' },
        markLine: { silent: true, symbol: 'none', label: { show: false },
          lineStyle: { color: '#c3c2b7', type: 'dashed', width: 1 },
          data: [{ yAxis: 0 }, { yAxis: 60 }, { yAxis: -60 }] },
        markArea: { silent: true, itemStyle: { color: 'rgba(227,73,72,0.04)' },
          data: [[{ yAxis: 60 }, { yAxis: 100 }], [{ yAxis: -100 }, { yAxis: -60 }]] } }]
    }, true);
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
  // 大盘情绪分始终作为背景层(图例可点击隐藏)
  const mktBgData = (D.mkt || []).map(m => m[1] === null ? null : [m[0], m[1]]);
  const mktSeries = { name: '大盘情绪分', type: 'line', data: mktBgData,
    showSymbol: false, lineStyle: { width: 1, color: '#c3c2b7', type: 'dashed' },
    itemStyle: { color: '#c3c2b7' }, silent: true, z: 1 };
  // 选中板块曲线在上层
  series.forEach(s => { s.z = 10; });
  curveC.setOption({
    animation: false,
    legend: { top: 6, left: 10, itemWidth: 14, itemHeight: 8,
      textStyle: { fontSize: 10, color: '#52514e' },
      data: ['大盘情绪分', ...series.map(s => s.name)] },
    tooltip: { trigger: 'axis', valueFormatter: v => v === null || v === undefined ? '--' : (v > 0 ? '+' : '') + v },
    grid: { left: 40, right: 12, top: 32, bottom: 40 },
    xAxis: { type: 'time', axisLabel: { color: '#898781', fontSize: 9 } },
    yAxis: { type: 'value', min: -105, max: 105,
      axisLabel: { color: '#898781', fontSize: 9 }, splitLine: { lineStyle: { color: '#e1e0d9' } } },
    dataZoom: curveZoomOpt(),
    series: [mktSeries, ...series]
  }, true);
  drawSide();
}

/* ── 分量明细(跟随最后选中的板块) ── */
// S3-A: hover联动 — 指定板块的分量明细(不改变 selected)
function drawSideFor(ci) {
  const side = document.getElementById('side');
  if (ci < 0) { side.style.display = 'none'; return; }
  const c = D.codes[ci];
  const cd = D.comp[c];
  if (!cd) { side.style.display = 'none'; return; }
  const latest = Object.keys(cd).sort().pop();
  const comps = cd[latest];
  const order = D.corder.filter(k => comps[k] !== undefined && comps[k] !== null)
    .sort((a, b) => Math.abs(comps[b]) - Math.abs(comps[a]));  // 按贡献排序
  let h = `<h3>${D.names[ci]}</h3><div class="date">${latest} 分量明细（按贡献排序）</div><table>
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
// S3-B: 申万一级筛选 + 排序模式 + 时间快捷
let swFilterVal = '', sortModeVal = 'score', levelVal = 'L3';
// S3-E: 层级切换
function setLevel(lv) {
  levelVal = lv;
  for (const [id, l] of [['lvL1','L1'],['lvL2','L2'],['lvL3','L3']]) {
    const b = document.getElementById(id);
    if (b) b.style.fontWeight = l === lv ? 'bold' : 'normal';
  }
  // 重填 swFilter 选项
  if (swSel) {
    swSel.innerHTML = '<option value="">全部</option>';
    let groups;
    if (lv === 'L2') groups = Object.keys(D.sw2_groups || {}).sort();
    else groups = Object.keys(D.sw1_groups || {}).filter(s => s).sort();
    for (const g of groups) {
      const opt = document.createElement('option');
      opt.value = g;
      const cnt = lv === 'L2' ? (D.sw2_groups[g]||[]).length : (D.sw1_groups[g]||[]).length;
      opt.textContent = `${g}(${cnt})`;
      swSel.appendChild(opt);
    }
    swFilterVal = '';
  }
  drawLeft();
}
for (const [id, l] of [['lvL1','L1'],['lvL2','L2'],['lvL3','L3']]) {
  const b = document.getElementById(id);
  if (b) b.onclick = () => setLevel(l);
}
const swSel = document.getElementById('swFilter');
if (swSel && D.sw1_groups) {
  const sws = Object.keys(D.sw1_groups).filter(s => s).sort();
  for (const sw of sws) {
    const opt = document.createElement('option');
    opt.value = sw; opt.textContent = `${sw}(${D.sw1_groups[sw].length})`;
    swSel.appendChild(opt);
  }
  swSel.addEventListener('change', () => { swFilterVal = swSel.value; drawLeft(); });
}
const sortSel = document.getElementById('sortMode');
if (sortSel) {
  sortSel.addEventListener('change', () => { sortModeVal = sortSel.value; drawLeft(); });
}

// S3-B: 时间快捷 — 改变热力图天数并重绘
let hmDaysGlobal = 120;
function hmZoom(days) {
  hmDaysGlobal = days || N_D; // 0 = 全部
  drawLeft();
  const btns = [['tq5',5],['tq20',20],['tq60',60],['tq120',120],['tq250',250],['tqAll',0]];
  for (const [id, d] of btns) {
    const b = document.getElementById(id);
    if (b) b.style.fontWeight = d === days ? 'bold' : 'normal';
  }
}
for (const [id, d] of [['tq5',5],['tq20',20],['tq60',60],['tq120',120],['tq250',250],['tqAll',0]]) {
  const b = document.getElementById(id);
  if (b) b.onclick = () => hmZoom(d);
}

// S3-B: 模式切换
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
  const hmDays = hmDaysGlobal || 120;
  const dStart = Math.max(0, N_D - hmDays);
  const hmDates = D.dates.slice(dStart);
  // S3-E: 层级感知分组
  const levelCis = (D.level_data && D.level_data[levelVal]) || null;
  let groups, groupKey;
  if (levelVal === 'L1') {
    groups = {};
    if (levelCis) levelCis.forEach(ci => groups[D.names[ci]] = [ci]);
    groupKey = 'sw1';
  } else if (levelVal === 'L2') {
    groups = {};
    if (levelCis) {
      const s2s1 = D.sw2_to_sw1 || {};
      const byParent = {};
      levelCis.forEach(ci => {
        const nm = D.names[ci];
        const parent = s2s1[nm.replace('SW2_','')] || '';
        (byParent[parent] = byParent[parent] || []).push(ci);
      });
      groups = byParent;
    }
    groupKey = 'sw2';
  } else {
    const allGroups = D.sw1_groups || {};
    groups = swFilterVal ? { [swFilterVal]: allGroups[swFilterVal] || [] } : allGroups;
    groupKey = 'sw1';
    if (levelCis) {
      // L3模式: 只取BK码, 剔除SW1_/SW2_聚合行
      const bkSet = new Set(levelCis);
      const filtered = {};
      for (const [k, v] of Object.entries(groups)) {
        filtered[k] = v.filter(ci => bkSet.has(ci));
      }
      groups = filtered;
    }
  }
  const sortedCis = [];
  const yLabels = [];
  const swNames = Object.keys(groups).sort();
  for (const sw of swNames) {
    for (const ci of groups[sw]) { sortedCis.push(ci); yLabels.push(D.names[ci]); }
    yLabels.push(sw || ''); sortedCis.push(-1);  // 组名行
  }
  yLabels.pop(); sortedCis.pop();
  const hmData = [];
  for (let xi = 0; xi < hmDates.length; xi++) {
    for (let yi = 0; yi < sortedCis.length; yi++) {
      const ci = sortedCis[yi];
      if (ci < 0) continue;
      const v = scoreAt(dStart + xi, ci);
      if (v !== null) hmData.push([xi, yi, v]);
    }
  }
  // 温度定性
  function labelOf(v) {
    if (v <= -80) return '绝对冷'; if (v <= -70) return '极冷';
    if (v <= -60) return '超强冷'; if (v <= -50) return '强冷';
    if (v <= -40) return '较强冷'; if (v <= -30) return '冷';
    if (v <= -20) return '弱冷'; if (v <= 0) return '中性弱冷';
    if (v <= 20) return '中性弱热'; if (v <= 30) return '弱热';
    if (v <= 40) return '热'; if (v <= 50) return '较强热';
    if (v <= 60) return '强热'; if (v <= 70) return '超强热';
    if (v <= 80) return '极热'; return '狂热';
  }
  // 核心驱动(取分量绝对值最大的2个)
  function driversOf(ci) {
    const cd = D.comp[D.codes[ci]];
    if (!cd) return '';
    const latest = Object.keys(cd).sort().pop();
    const comps = cd[latest] || {};
    const arr = D.corder.filter(k => comps[k] !== undefined && comps[k] !== null)
      .map(k => ({ k, v: comps[k] })).sort((a, b) => Math.abs(b.v) - Math.abs(a.v)).slice(0, 2);
    return arr.map(x => `${D.clabels[x.k] || x.k} ${x.v > 0 ? '+' : ''}${Math.round(x.v)}`).join(' · ');
  }
  const swSet = new Set(swNames.filter(s => s));
  rankC.setOption({
    animation: false,
    title: { text: `热力图 · ${hmDates[0]?.slice(5)} ~ ${hmDates[hmDates.length-1]?.slice(5)}`,
             left: 10, top: 6, textStyle: { fontSize: 12, color: '#52514e' } },
    tooltip: { position: 'top', formatter: p => {
      const ci = sortedCis[p.data[1]];
      if (ci < 0) return yLabels[p.data[1]] ? `<b>${yLabels[p.data[1]]}</b>` : '';
      const sw = Object.keys(groups).find(s => groups[s]?.includes(ci));
      const v = p.data[2];
      const drv = driversOf(ci);
      let h = `<b>${D.names[ci]}</b>${sw && sw ? `（SW: ${sw}）` : ''}`;
      h += `<br/>${hmDates[p.data[0]]}`;
      h += `<br/>温度 <b style="color:${v >= 0 ? '#e34948' : '#2a78d6'}">${v > 0 ? '+' : ''}${v}</b>（${labelOf(v)}）`;
      if (drv) h += `<br/><span style="color:#898781;font-size:11px">${drv}</span>`;
      return h;
    }},
    grid: { left: 10, right: 60, top: 32, bottom: 56, containLabel: true },
    xAxis: { type: 'category', data: hmDates.map(d => d.slice(5)),
      axisLabel: { color: '#898781', fontSize: 9, rotate: 45 },
      splitArea: { show: true, areaStyle: { color: ['rgba(0,0,0,0.02)', 'rgba(0,0,0,0.04)'] } } },
    yAxis: { type: 'category', data: yLabels,
      axisLabel: { fontSize: 8,
        formatter: v => swSet.has(v) ? `{g|${v}}` : v,
        rich: { g: { fontWeight: 'bold', fontSize: 10, color: '#0b0b0b' } },
        color: '#52514e' },
      splitArea: { show: true, areaStyle: { color: ['rgba(0,0,0,0.02)', 'rgba(0,0,0,0.04)'] } } },
    // 16段色阶(深蓝→浅蓝→米白→橙红→深红, 每段带语义标签)
    visualMap: { type: 'piecewise',
      pieces: [
        { lte: -80, color: '#0d47a1', label: '绝对冷' },
        { gt: -80, lte: -70, color: '#1565c0', label: '极冷' },
        { gt: -70, lte: -60, color: '#1e88e5', label: '超强冷' },
        { gt: -60, lte: -50, color: '#42a5f5', label: '强冷' },
        { gt: -50, lte: -40, color: '#64b5f6', label: '较强冷' },
        { gt: -40, lte: -30, color: '#90caf9', label: '冷' },
        { gt: -30, lte: -20, color: '#bbdefb', label: '弱冷' },
        { gt: -20, lte: 0, color: '#e8f4fd', label: '弱冷' },
        { gt: 0, lte: 20, color: '#fef6ec', label: '弱热' },
        { gt: 20, lte: 30, color: '#ffe082', label: '弱热' },
        { gt: 30, lte: 40, color: '#ff9800', label: '热' },
        { gt: 40, lte: 50, color: '#ff5722', label: '较强热' },
        { gt: 50, lte: 60, color: '#d32f2f', label: '强热' },
        { gt: 60, lte: 70, color: '#b71c1c', label: '超强热' },
        { gt: 70, lte: 80, color: '#8e0000', label: '极热' },
        { gt: 80, color: '#6a0000', label: '狂热' },
      ],
      orient: 'horizontal', left: 'center', bottom: 0,
      itemWidth: 10, itemHeight: 8, itemGap: 1,
      textStyle: { fontSize: 8, color: '#898781' },
      formatter: (v, v2) => {
        const mid = (v + v2) / 2 || v;
        return labelOf(mid);
      },
    },
    dataZoom: [{ type: 'inside', xAxisIndex: 0 }, { type: 'slider', xAxisIndex: 0,
      bottom: 26, height: 14, borderColor: '#c3c2b7', fillerColor: 'rgba(42,120,214,0.1)' }],
    series: [{ type: 'heatmap', data: hmData,
      itemStyle: { borderColor: 'rgba(0,0,0,0.08)', borderWidth: 0.3 },
      progressive: 5000, progressiveThreshold: 10000,
      emphasis: { itemStyle: { shadowBlur: 4, shadowColor: 'rgba(0,0,0,0.3)', borderColor: '#0b0b0b', borderWidth: 1.5 } } }]
  }, true);
  rankC.off('click');
  rankC.on('click', p => {
    const ci = sortedCis[p.data[1]];
    if (ci >= 0) toggleSel(ci);
  });
  // S3-A: hover联动右侧分量
  rankC.off('mouseover');
  rankC.on('mouseover', p => {
    const ci = sortedCis[p.componentType === 'series' ? p.data[1] : -1];
    if (ci >= 0) drawSideFor(ci);
  });
}
document.getElementById('btnClear').onclick = () => { selected = []; renderCards(); drawCurve(); drawLeft(); };
document.getElementById('q').addEventListener('input', e => { query = e.target.value.trim(); drawLeft(); });
document.getElementById('themeToggle').onclick = () => {
  const h = document.documentElement;
  const t = h.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  h.setAttribute('data-theme', t);
};
// S3-F: 右侧曲线 dataZoom → 左侧热力图同步(曲线跨度15年, 按曲线总天数折算)
curveC.on('datazoom', () => {
  const opt = curveC.getOption();
  const z = opt.dataZoom[0];
  if (!z) return;
  const visibleDays = Math.round(((z.end - z.start) / 100) * curveTotalDays());
  if (Math.abs(visibleDays - hmDaysGlobal) > 10) {
    hmDaysGlobal = Math.min(Math.max(visibleDays, 5), N_D); // 热力图数据上限=全部日期
    drawLeft();
  }
});

// S3-F: 排行图y轴滚动(支持查看全部板块)
function enableRankScroll(items) {
  if (!items || items.length === 0) return;
  const opt = rankC.getOption();
  if (opt && opt.yAxis && opt.yAxis[0] && opt.yAxis[0].data) {
    const total = opt.yAxis[0].data.length;
    if (total > 60) {
      rankC.setOption({
        dataZoom: [
          { type: 'inside', yAxisIndex: 0, zoomOnMouseWheel: false, moveOnMouseWheel: true,
            startValue: 0, endValue: 60 },
          { type: 'slider', yAxisIndex: 0, right: 0, width: 12,
            startValue: 0, endValue: 60,
            borderColor: '#c3c2b7', fillerColor: 'rgba(42,120,214,0.08)',
            textStyle: { fontSize: 8, color: '#898781' } },
        ]
      });
    }
  }
}

window.addEventListener('resize', () => { rankC.resize(); curveC.resize(); slideC.resize(); sparkC.resize(); });

renderCards(); drawLeft(); updateMkt(); drawCurve();
// S3-A: 默认显示最热板块的分量明细
if (D.top5 && D.top5.length) {
  const ci = D.codes.indexOf(D.top5[0].c);
  if (ci >= 0) drawSideFor(ci);
}
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
