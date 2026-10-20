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
    import base64
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    # P5: 全历史(不再按years截断); years仅保留CLI兼容
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM sector_scores ORDER BY date")]
    dset = set(dates)
    ph = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT date,code,score FROM sector_scores WHERE date IN ({ph})", dates).fetchall()
    codes = sorted({r[1] for r in rows})
    names = dict(conn.execute("SELECT code,name FROM sectors").fetchall())

    # P5 紧凑编码: int8分数 + 空值位图 + base64(JSON内嵌体积 ~1/6)
    N_D, N_C = len(dates), len(codes)
    idx_d = {d: i for i, d in enumerate(dates)}
    idx_c = {c: i for i, c in enumerate(codes)}
    cells = bytearray(N_D * N_C)                   # 0=占位(空)
    bitmap = bytearray((N_D * N_C + 7) // 8)       # bit=1 → 有效
    for d, c, s in rows:
        if s is None:
            continue
        k = idx_d[d] * N_C + idx_c[c]
        cells[k] = max(-100, min(100, int(round(s)))) & 0xFF
        bitmap[k >> 3] |= 1 << (k & 7)
    mat = {"b64": base64.b64encode(bytes(cells)).decode(),
           "null": base64.b64encode(bytes(bitmap)).decode()}

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

    # S2.1-A: 当日摘要(最热/最冷 Top5) — 取BK行最新日(SW行可能更新一日, JOIN sectors取不到)
    latest = next((d for d in reversed(dates)
                   if conn.execute("SELECT 1 FROM sector_scores sc JOIN sectors s "
                                   "ON sc.code=s.code WHERE sc.date=? LIMIT 1", (d,)).fetchone()), None)
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
    # SW1_/SW2_/SW3_ 独立温度行 + BK 行(P5 四层级)
    sw1_agg_cis = [ci for ci, c in enumerate(codes) if c.startswith("SW1_")]
    sw2_agg_cis = [ci for ci, c in enumerate(codes) if c.startswith("SW2_")]
    sw3_agg_cis = [ci for ci, c in enumerate(codes) if c.startswith("SW3_")]
    bk_cis = [ci for ci, c in enumerate(codes) if c.startswith("BK")]
    level_data = {"L1": sw1_agg_cis, "L2": sw2_agg_cis, "SW3": sw3_agg_cis, "BK": bk_cis}

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
display:flex;flex-direction:column;min-height:100vh;overflow-x:hidden;overflow-y:auto}
/* D1-8#3: 窗口非最大化时底部不再被裁剪 — 允许纵向滚动, 图区保最小高度 */
.chart-row{min-height:430px}
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
.card{min-width:104px;padding:8px 12px;border-radius:8px;background:var(--p);
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
.chart-row{flex:1;display:flex;gap:12px;padding:4px 16px 8px;min-height:0}
#rank{flex:8;background:var(--s);border:1px solid var(--g);border-radius:10px;min-width:0}
#curve{flex:4;background:var(--s);border:1px solid var(--g);border-radius:10px;min-width:0;position:relative}
#side{position:absolute;right:6px;top:6px;width:250px;max-height:calc(100% - 12px);overflow-y:auto;
background:var(--s);border:1px solid var(--g);border-radius:8px;padding:10px 12px;display:none;z-index:10;
box-shadow:0 2px 8px rgba(0,0,0,0.08)}
#side h3{margin:0 0 2px;font-size:12px}
#side .date{color:var(--m);font-size:10px;margin-bottom:6px}
#side .sem{font-size:11px;color:var(--i2);margin:2px 0}
#side .sem.warn{color:#eda100}
#side table.cmp{margin:2px 0 8px}
#side table.cmp td:first-child{max-width:86px;overflow:hidden;text-overflow:ellipsis}
.card .sem{font-size:10px;color:var(--m);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mkt-box .sem{font-size:10px;color:var(--i2)}
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
    <button class="btn" id="btnRankDir" style="padding:2px 6px;font-size:10px">高→低</button>
    <button class="btn" id="lvL1" style="padding:2px 6px;font-size:10px">一级</button>
    <button class="btn" id="lvL2" style="padding:2px 6px;font-size:10px">二级</button>
    <button class="btn" id="lvSW3" style="padding:2px 6px;font-size:10px">三级</button>
    <button class="btn" id="lvBK" style="padding:2px 6px;font-size:10px;font-weight:bold">行业</button>
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
    <span class="sem" id="mktSem"></span>
    <div id="mktSpark"></div>
  </div>
</div>
<div style="padding:0 16px;font-size:9px;color:var(--m);flex-shrink:0">
口径: SW行(一级/二级/三级)=独立14分量温度, L1/L2自2011·L3自2006; 行业(BK)=2019起。
早期段量价主导(涨停估算·资金流2007+缺失自动归一化)。热力图窗口上限3年(750交易日), 可拖动滑块左右移动。
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
// P5 紧凑矩阵解码: int8 + 空值位图 → 有符号访问
const _u8 = (b64) => Uint8Array.from(atob(b64), c => c.charCodeAt(0));
const MAT_CELLS = _u8(D.mat.b64), MAT_NULL = _u8(D.mat.null);
function matAt(di, ci) {
  const k = di * N_C + ci;
  if (!((MAT_NULL[k >> 3] >> (k & 7)) & 1)) return null;
  const v = MAT_CELLS[k];
  return v > 127 ? v - 256 : v;                   // int8 符号还原
}
let curDay = (function () {              // D1-8#6: 默认取BK最近有数日(SW行可能新1日, BK全null)
  for (let d = N_D - 1; d >= 0; d--)
    if (matAt(d, D.codes.findIndex(c => c.startsWith('BK'))) !== null) return d;
  return N_D - 1;
})(), query = "";
let selected = [];   // 多板块对比: 已选板块的 code index 数组(≤5)
const PALETTE = ["#2a78d6","#e34948","#1baf7a","#eda100","#6250d6"];

/* ── S3-D: 主题令牌 — CSS变量为单一色彩源, 切换主题后重取并重绘 ── */
const cssVar = n => (typeof getComputedStyle === 'function'
  ? getComputedStyle(document.documentElement).getPropertyValue(n).trim() : '');
function pal() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  return {
    txt: cssVar('--i2') || '#52514e',   // 标题/图例/主文字
    axis: cssVar('--m') || '#898781',   // 坐标轴/辅助文字
    grid: cssVar('--g') || '#e1e0d9',   // 网格线
    a: cssVar('--a') || '#c3c2b7',      // markLine/滑块边框
    pos: cssVar('--rd') || '#e34948',   // 正温
    neg: cssVar('--bl') || '#2a78d6',   // 负温
    strong: cssVar('--i') || '#0b0b0b', // 组名/hover描边/tooltip正文
    warn: '#eda100',                    // 背离警示(双主题可读)
    tipBg: dark ? 'rgba(38,38,37,0.96)' : 'rgba(255,255,255,0.96)',
    splitA: dark ? ['rgba(255,255,255,0.02)', 'rgba(255,255,255,0.05)']
                 : ['rgba(0,0,0,0.02)', 'rgba(0,0,0,0.04)'],
    shadow: dark ? 'rgba(0,0,0,0.6)' : 'rgba(0,0,0,0.3)'
  };
}
let PAL = pal();

const rankC = echarts.init(document.getElementById('rank'));
const curveC = echarts.init(document.getElementById('curve'));
const slideC = echarts.init(document.getElementById('slider'));
const sparkC = echarts.init(document.getElementById('mktSpark'));

function mktAt(di) {
  const d = D.dates[di];
  for (let i = D.mkt.length - 1; i >= 0; i--) if (D.mkt[i][0] <= d) return D.mkt[i];
  return null;
}
function scoreAt(di, ci) { return (di < 0 || di >= N_D) ? null : matAt(di, ci); }

/* ── S3-C: 温度语义引擎 ── */
// n日前温度差(hover 日期感知); 早期数据不足返回 null
function deltaAt(di, ci, n) {
  const v = scoreAt(di, ci), p = scoreAt(di - n, ci);
  return (v === null || p === null) ? null : v - p;
}
// 一句话定性: 温度水平 × 5日趋势
function semanticOf(v, d5) {
  const d = d5 === null ? 0 : d5;
  if (v >= 60 && d <= -10) return '高位回落 · 警惕退潮';
  if (v >= 60 && d >= 10) return '加速冲顶 · 情绪亢奋';
  if (v >= 60) return '过热区 · 谨慎追高';
  if (v >= 40 && d > 0) return '强势升温 · 趋势延续';
  if (v >= 40) return '强势区 · 动能减弱';
  if (v >= 20 && d > 5) return '升温发酵 · 关注持续性';
  if (v >= 20) return '温热区 · 滞涨迹象';
  if (v > -20) return d > 5 ? '中性回暖' : (d < -5 ? '中性转弱' : '中性震荡');
  if (v <= -60 && d <= -10) return '恐慌探底 · 关注超跌反弹';
  if (v <= -60) return '深度冰点 · 左侧观察区';
  if (v <= -40 && d > 5) return '冰点修复 · 反弹尝试';
  if (v <= -40) return '偏冷区 · 观望为主';
  return d > 5 ? '低位回暖' : '持续偏冷';
}
function semShort(phrase) { return phrase.split(' · ')[0]; }
// 量价背离检测(最新日分量交叉) → 提示文本或 null
// 阈值±50=四分位语义(量能分位<25%且涨幅分位>75%): 正常日~0%触发,
// 全市场缩量反弹等异常日(如2026-10-16)保留最极端一成(10%/493板)
function divergenceOf(comps) {
  if (!comps) return null;
  const chg = comps['chg'], vd = comps['vol_dir'], ff = comps['fflow'];
  if (chg === undefined || chg === null) return null;
  if (vd !== undefined && vd !== null && vd <= -50 && chg >= 50) return '⚠ 缩量上涨 · 量价背离';
  if (ff !== undefined && ff !== null && ff <= -50 && chg >= 50) return '⚠ 价涨资金流出 · 背离';
  if (vd !== undefined && vd !== null && vd >= 50 && chg <= -50) return '放量下跌 · 恐慌宣泄';
  if (ff !== undefined && ff !== null && ff >= 50 && chg <= -50) return '价跌资金流入 · 有承接';
  return null;
}
// 最新日分量(近5日明细中取最大日期)
function latestComps(ci) {
  const cd = D.comp[D.codes[ci]];
  if (!cd) return null;
  const k = Object.keys(cd).sort().pop();
  return { date: k, comps: cd[k] || null };
}

/* ── 大盘情绪更新 ── */
function updateMkt() {
  const m = mktAt(curDay);
  const el = document.getElementById('mktSc');
  const semEl = document.getElementById('mktSem');
  if (!m || m[1] === null) { el.textContent = '--'; if (semEl) semEl.textContent = ''; return; }
  el.textContent = (m[1] >= 0 ? '+' : '') + m[1];
  el.style.color = m[1] >= 0 ? 'var(--rd)' : 'var(--bl)';
  document.getElementById('mktDate').textContent = m[0];
  // S3-C: 大盘语义(mkt序列上的5日差)
  const mi = D.mkt.lastIndexOf(m);
  const mPrev = mi >= 5 ? D.mkt[mi - 5] : null;
  const md5 = (mPrev && mPrev[1] !== null) ? m[1] - mPrev[1] : null;
  if (semEl) semEl.textContent = semanticOf(m[1], md5);
  const recent = D.mkt.slice(-30);
  sparkC.setOption({
    animation: false,
    grid: { left: 0, right: 0, top: 2, bottom: 0 },
    xAxis: { type: 'category', show: false, data: recent.map(r => r[0]) },
    yAxis: { type: 'value', show: false, min: -100, max: 100 },
    series: [{ type: 'line', data: recent.map(r => r[1]), showSymbol: false,
      lineStyle: { width: 1.5, color: m[1] >= 0 ? PAL.pos : PAL.neg },
      itemStyle: { color: m[1] >= 0 ? PAL.pos : PAL.neg } }]
  }, true);
}

/* ── 信息卡 ── */
function renderCards() {
  const mkCard = (item, idx) => {
    const ci = D.codes.indexOf(item.c);
    const isSel = selected.includes(ci);
    const color = isSel ? PALETTE[selected.indexOf(ci)] : null;
    // S3-C: 卡片短语义
    const d5 = deltaAt(N_D - 1, ci, 5);
    const sem = semShort(semanticOf(item.s, d5));
    return `<div class="card ${isSel ? 'sel' : ''}" data-ci="${ci}"
      style="${color ? `border-color:${color}` : ''}">
      <div class="nm">${item.n}</div>
      <div class="sc" style="color:${item.s >= 0 ? 'var(--rd)' : 'var(--bl)'}">${item.s > 0 ? '+' : ''}${item.s}</div>
      <div class="rk">分位${item.r}%</div>
      <div class="sem">${sem}</div>
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
  // D1-8#8: 不再全图重绘(点击闪烁); 选中态由曲线颜色/卡片表达
}

/* ── 排行榜 ── */
function effDay() {                      // D1-8#6: 该层级最近有数日(回溯, SW/BK最新日可能差1日)
  for (let d = curDay; d >= Math.max(0, curDay - 10); d--) {
    const cis = (D.level_data && D.level_data[levelVal]) || D.codes.map((_, i) => i);
    if (cis.some(ci => scoreAt(d, ci) !== null)) return d;
  }
  return curDay;
}
function rankData() {
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
    const v = scoreAt(effDay(), ci);
    if (v === null) continue;
    items.push({ name: nm, v: v, ci: ci });
  }
  // S3-B: 排序模式
  if (sortModeVal === 'chg') {
    const prev = (i) => scoreAt(effDay() - 1, i) || 0, cur = (i) => scoreAt(effDay(), i) || 0;
    items.sort((a, b) => (cur(b.ci) - prev(b.ci)) - (cur(a.ci) - prev(a.ci)));
  } else if (sortModeVal === 'ext') {
    const hmDays = Math.min(hmDaysGlobal || 60, 750);   // D1-8#10: 跟随时间按钮
    const dStart = Math.max(0, effDay() - hmDays);       // D1-8b-review: 与items同基准
    const ext = (ci) => {
      let m = 0;
      for (let di = dStart; di <= effDay(); di++) {
        const v = scoreAt(di, ci);
        if (v !== null) m = Math.abs(v) > Math.abs(m) ? v : m;
      }
      return m;
    };
    items.sort((a, b) => Math.abs(ext(b.ci)) - Math.abs(ext(a.ci)));
  } else {
    items.sort((a, b) => rankDesc ? b.v - a.v : a.v - b.v);   // D1-8#9 高低切换
  }
  return items.slice(0, 600);  // S3-F: 提高上限, yAxis dataZoom 控制显示数量
}

function drawRank() {
  const items = rankData();
  const day = D.dates[effDay()];
  document.getElementById('hInfo').textContent = `${day} · ${N_C} 板块`;
  rankC.setOption({
    animation: false,
    title: { text: `温度排行 · ${day} ${rankDesc ? '↓高→低' : '↑低→高'}`, left: 10, top: 6,
             textStyle: { fontSize: 12, color: PAL.txt } },
    tooltip: { trigger: 'item', backgroundColor: PAL.tipBg, borderColor: PAL.grid,
      textStyle: { color: PAL.strong }, formatter: p => {
      const it = items[p.dataIndex];
      const sel = it && selected.includes(it.ci);
      // S3-C: 5日变化 + 语义短语
      const d5 = it ? deltaAt(curDay, it.ci, 5) : null;
      let h = `${p.name}<br/>温度 ${p.value > 0 ? '+' : ''}${p.value}`;
      if (d5 !== null) h += `<br/><span style="color:${d5 >= 0 ? PAL.pos : PAL.neg}">5日 ${d5 > 0 ? '+' : ''}${d5}</span> · ${semanticOf(p.value, d5)}`;
      else h += `<br/>${semanticOf(p.value, null)}`;
      if (sel) h += '<br/><b>已选对比</b>';
      return h;
    }},
    grid: { left: 10, right: 40, top: 30, bottom: 8, containLabel: true },
    xAxis: { type: 'value', min: -105, max: 105, axisLabel: { color: PAL.axis, fontSize: 10 },
      splitLine: { lineStyle: { color: PAL.grid } } },
    yAxis: { type: 'category', inverse: true,
      data: items.map(i => i.name),
      axisLabel: { color: PAL.txt, fontSize: 10,
        formatter: v => selected.some(ci => D.names[ci] === v) ? `▶ ${v}` : v },
      axisTick: { show: false }, axisLine: { lineStyle: { color: PAL.a } } },
    series: [{ type: 'bar', data: items.map(i => {
        const selIdx = selected.indexOf(i.ci);
        const isSel = selIdx >= 0;
        return { value: i.v, itemStyle: { color: isSel ? PALETTE[selIdx]
          : i.v >= 0 ? `rgba(227,73,72,${Math.min(0.25 + Math.abs(i.v) / 130, 0.95)})`
                     : `rgba(42,120,214,${Math.min(0.25 + Math.abs(i.v) / 130, 0.95)})` } };
      }),
      barMaxWidth: 10, barCategoryGap: '18%',
      label: { show: true, position: 'right', fontSize: 9, color: PAL.axis,
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
// 图例隐藏态随重绘保留(仅对当前仍存在的系列; notMerge=true 会清掉 legend.selected)
function curveLegendHidden(names) {
  const out = {};
  try {
    const lg = curveC.getOption().legend[0];
    if (lg && lg.selected) for (const n of names) if (lg.selected[n] === false) out[n] = false;
  } catch (e) { /* 首次渲染无 legend */ }
  return out;
}
function drawCurve() {
  const side = document.getElementById('side');
  side.style.display = 'none';               // D1-8#5: 曲线交互时隐藏, hover热力图再显示
  if (selected.length === 0) {
    // S3-F: 默认显示大盘情绪分曲线
    const mktData = (D.mkt || []).map(m => m[1] === null ? null : [m[0], m[1]]);
    curveC.setOption({
      animation: false,
      title: { text: '大盘情绪分(点击左侧板块可叠加对比)', left: 10, top: 6,
        textStyle: { fontSize: 12, color: PAL.txt } },
      tooltip: { trigger: 'axis', backgroundColor: PAL.tipBg, borderColor: PAL.grid,
        textStyle: { color: PAL.strong },
        valueFormatter: v => v === null ? '--' : (v > 0 ? '+' : '') + v },
      grid: { left: 40, right: 12, top: 30, bottom: 40 },
      xAxis: { type: 'time', axisLabel: { color: PAL.axis, fontSize: 9 } },
      yAxis: { type: 'value', min: -105, max: 105,
        axisLabel: { color: PAL.axis, fontSize: 9 }, splitLine: { lineStyle: { color: PAL.grid } } },
      dataZoom: curveZoomOpt(),
      series: [{ name: '大盘情绪分', type: 'line', data: mktData, showSymbol: false,
        lineStyle: { width: 2, color: PAL.neg }, itemStyle: { color: PAL.neg },
        markLine: { silent: true, symbol: 'none', label: { show: false },
          lineStyle: { color: PAL.a, type: 'dashed', width: 1 },
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
        lineStyle: { color: PAL.a, type: 'dashed', width: 1 },
        data: [{ yAxis: 0 }, { yAxis: 60 }, { yAxis: -60 }] } : undefined,
      markArea: si === 0 ? { silent: true, itemStyle: { color: 'rgba(227,73,72,0.04)' },
        data: [[{ yAxis: 60 }, { yAxis: 100 }], [{ yAxis: -100 }, { yAxis: -60 }]] } : undefined };
  });
  // 大盘情绪分始终作为背景层(图例可点击隐藏)
  const mktBgData = (D.mkt || []).map(m => m[1] === null ? null : [m[0], m[1]]);
  const mktSeries = { name: '大盘情绪分', type: 'line', data: mktBgData,
    showSymbol: false, lineStyle: { width: 1, color: PAL.a, type: 'dashed' },
    itemStyle: { color: PAL.a }, silent: true, z: 1 };
  // 选中板块曲线在上层
  series.forEach(s => { s.z = 10; });
  curveC.setOption({
    animation: false,
    legend: { type: 'scroll', top: 6, left: 10, itemWidth: 14, itemHeight: 8,
      textStyle: { fontSize: 10, color: PAL.txt },
      data: ['大盘情绪分', ...series.map(s => s.name)],
      selected: curveLegendHidden(['大盘情绪分', ...series.map(s => s.name)]) },
    tooltip: { trigger: 'axis', backgroundColor: PAL.tipBg, borderColor: PAL.grid,
      textStyle: { color: PAL.strong },
      valueFormatter: v => v === null || v === undefined ? '--' : (v > 0 ? '+' : '') + v },
    grid: { left: 40, right: 12, top: 32, bottom: 40 },
    xAxis: { type: 'time', axisLabel: { color: PAL.axis, fontSize: 9 } },
    yAxis: { type: 'value', min: -105, max: 105,
      axisLabel: { color: PAL.axis, fontSize: 9 }, splitLine: { lineStyle: { color: PAL.grid } } },
    dataZoom: curveZoomOpt(),
    series: [mktSeries, ...series]
  }, true);
  drawSide();
}

/* ── 分量明细(统一渲染: hover联动/选中跟随) ── */
// S3-C: drawSideFor 增加语义头+背离徽标+多选对比摘要; drawSide 复用同一渲染
function hideSide() { document.getElementById('side').style.display = 'none'; }
function drawSideFor(ci) {
  const side = document.getElementById('side');
  if (ci < 0) { side.style.display = 'none'; return; }
  const lc = latestComps(ci);
  if (!lc || !lc.comps) { side.style.display = 'none'; return; }
  // 语义头(温度×5日趋势) + 背离徽标 — 与分量表同日(lc.date)对齐, 不随日期滑块漂移
  const at = D.dates.indexOf(lc.date);
  const adi = at >= 0 ? at : curDay;
  const v = scoreAt(adi, ci);
  const d5 = deltaAt(adi, ci, 5);
  let h = `<h3>${D.names[ci]}</h3>`;
  if (v !== null) h += `<div class="sem">${d5 !== null ? `5日${d5 > 0 ? '+' : ''}${d5} · ` : ''}${semanticOf(v, d5)}</div>`;
  const divg = divergenceOf(lc.comps);
  if (divg) h += `<div class="sem warn">${divg}</div>`;
  h += `<div class="date">${lc.date} 分量明细（按贡献排序）</div>`;
  // S3-C: 多选(≥2)时顶部对比摘要表(同日对比)
  if (selected.length >= 2) {
    h += '<table class="cmp"><tr><th>对比</th><th>温度</th><th>5日</th><th>语义</th></tr>';
    for (const sci of selected) {
      const sv = scoreAt(adi, sci), sd5 = deltaAt(adi, sci, 5);
      h += `<tr${sci === ci ? ' style="font-weight:700"' : ''}><td>${D.names[sci]}</td>` +
        `<td style="color:${sv !== null && sv >= 0 ? 'var(--rd)' : 'var(--bl)'}">${sv === null ? '--' : (sv > 0 ? '+' : '') + sv}</td>` +
        `<td>${sd5 === null ? '--' : (sd5 > 0 ? '+' : '') + sd5}</td>` +
        `<td>${sv === null ? '--' : semShort(semanticOf(sv, sd5))}</td></tr>`;
    }
    h += '</table>';
  }
  const comps = lc.comps;
  const order = D.corder.filter(k => comps[k] !== undefined && comps[k] !== null)
    .sort((a, b) => Math.abs(comps[b]) - Math.abs(comps[a]));
  h += '<table><tr><th>分量</th><th>分位分</th></tr>';
  for (const k of order) {
    const val = comps[k];
    const w = Math.min(Math.abs(val), 100) / 2;
    const pos = val >= 0 ? `left:50%;width:${w}%;background:var(--rd)`
                         : `left:${50 - w}%;width:${w}%;background:var(--bl)`;
    h += `<tr><td>${D.clabels[k] || k}</td><td style="min-width:80px">
<div class="track"><div class="fill" style="${pos}"></div></div></td>
<td style="text-align:right;color:${val >= 0 ? 'var(--rd)' : 'var(--bl)'}">${val > 0 ? '+' : ''}${val}</td></tr>`;
  }
  h += '</table>';
  side.innerHTML = h;
  side.style.display = 'block';
}

function drawSide() {
  if (selected.length === 0) { document.getElementById('side').style.display = 'none'; return; }
  drawSideFor(selected[selected.length - 1]);
}

/* ── 日期滑块 ── */
// S3-D: 包成函数, 主题切换后随 PAL 重绘(事件绑定仅一次)
function drawSlider() {
  // 保留当前滑块位置(重绘不重置; setOption不触发datazoom事件, 否则与curDay失同步)
  let z = null;
  try { const o = slideC.getOption(); z = o && o.dataZoom && o.dataZoom[0]; } catch (e) { z = null; }
  const start = z && typeof z.start === 'number' ? z.start : Math.max(0, (N_D - 31) / N_D * 100);
  const end = z && typeof z.end === 'number' ? z.end : 100;
  slideC.setOption({
    animation: false,
    grid: { left: 130, right: 130, top: 4, bottom: 22 },
    xAxis: { show: false, min: 0, max: N_D - 1 },
    yAxis: { show: false, min: 0, max: 1 },
    graphic: [],
    dataZoom: [{
      type: 'slider', xAxisIndex: 0, start: start, end: end,
      height: 16, bottom: 2, borderColor: PAL.a,
      fillerColor: 'rgba(42,120,214,0.12)',
      labelFormatter: v => D.dates[Math.round(v / 100 * (N_D - 1))]?.slice(5) || ''
    }]
  });
}
drawSlider();
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
let swFilterVal = '', sortModeVal = 'score', levelVal = 'BK', rankDesc = true;
// S3-E: 层级切换
function setLevel(lv) {
  levelVal = lv;
  for (const [id, l] of [['lvL1','L1'],['lvL2','L2'],['lvSW3','SW3'],['lvBK','BK']]) {
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
  hmDaysGlobal = Math.min(days || 750, 750);  // P5: 3年滑动窗口上限(全部=750)
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
for (const [id, l] of [['lvL1','L1'],['lvL2','L2'],['lvSW3','SW3'],['lvBK','BK']]) {
  const b = document.getElementById(id);
  if (b) b.onclick = () => setLevel(l);
}
{ // D1-8#9: 排行高低切换
  const b = document.getElementById('btnRankDir');
  if (b) b.onclick = () => { rankDesc = !rankDesc; b.textContent = rankDesc ? '高→低' : '低→高'; drawLeft(); };
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

function heatZoomOpt() {
  // D1-8#5: 层级切换/重绘保留当前缩放与平移位置
  // D1-8b-review: 只认x轴dataZoom — rankC排行模式共用, 其yAxis滚动slider的
  // start/end 百分比不得误当x窗口(默认路径rank→heat切换即踩)
  try { const o = rankC.getOption();
    const z = o && o.dataZoom && o.dataZoom.find(d => d.xAxisIndex !== undefined
      && d.yAxisIndex === undefined && typeof d.start === 'number');
    if (z && (z.start > 0.01 || z.end < 99.99))
      return { start: z.start, end: z.end };
  } catch (e) { /* 首次 */ }
  const win = Math.min(hmDaysGlobal || 120, 750);
  return { start: Math.max(0, 100 - win / N_D * 100), end: 100 };
}
function drawHeatmap() {
  // D1-8#3: hmDates 常驻全量(4481日), 窗口= dataZoom start/end; 可平移任意历史
  const hmDates = D.dates;
  const z = heatZoomOpt(), zStart = z.start, zEnd = z.end;
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
  } else if (levelVal === 'SW3') {
    // P5: 三级=SW3独立行, 平铺(无父子映射表)
    groups = { '三级行业': levelCis || [] };
    groupKey = 'sw3';
  } else {
    // BK行业: 按SW1分组(旧L3模式)
    const allGroups = D.sw1_groups || {};
    groups = swFilterVal ? { [swFilterVal]: allGroups[swFilterVal] || [] } : allGroups;
    groupKey = 'sw1';
    if (levelCis) {
      const bkSet = new Set(levelCis);
      const filtered = {};
      for (const [k, v] of Object.entries(groups)) {
        filtered[k] = v.filter(ci => bkSet.has(ci));
      }
      groups = filtered;
    }
  }
  // D1-8#11: 去掉组名分隔行(灰色条干扰色块连续性) — 同组相邻排列即可
  const sortedCis = [];
  const yLabels = [];
  const swNames = Object.keys(groups).sort();
  for (const sw of swNames) {
    for (const ci of groups[sw]) { sortedCis.push(ci); yLabels.push(D.names[ci]); }
  }
  const hmData = [];
  for (let xi = 0; xi < hmDates.length; xi++) {
    for (let yi = 0; yi < sortedCis.length; yi++) {
      const ci = sortedCis[yi];
      if (ci < 0) continue;
      const v = scoreAt(xi, ci);            // D1-8#3: 全量日期, xi即全局日索引
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
  rankC.setOption({
    animation: false,
    title: { text: `热力图 · ${hmDates[0]?.slice(5)} ~ ${hmDates[hmDates.length-1]?.slice(5)}`,
             left: 10, top: 6, textStyle: { fontSize: 12, color: PAL.txt } },
    tooltip: { position: 'top', backgroundColor: PAL.tipBg, borderColor: PAL.grid,
      textStyle: { color: PAL.strong }, formatter: p => {
      if (!p.data) return '';                       // D1-8#7: 空hover守卫
      const ci = sortedCis[p.data[1]];
      if (ci < 0) return yLabels[p.data[1]] ? `<b>${yLabels[p.data[1]]}</b>` : '';
      const sw = Object.keys(groups).find(s => groups[s]?.includes(ci));
      const v = p.data[2];
      const drv = driversOf(ci);
      // S3-C: 5日变化 + 语义短语 + 量价背离
      const d5 = deltaAt(p.data[0], ci, 5);   // D1-8#3: 全量日期
      let h = `<b>${D.names[ci]}</b>${sw && sw ? `（SW: ${sw}）` : ''}`;
      h += `<br/>${hmDates[p.data[0]]}`;
      h += `<br/>温度 <b style="color:${v >= 0 ? PAL.pos : PAL.neg}">${v > 0 ? '+' : ''}${v}</b>（${labelOf(v)}）`;
      if (d5 !== null) h += `<br/><span style="color:${d5 >= 0 ? PAL.pos : PAL.neg};font-size:11px">5日 ${d5 > 0 ? '+' : ''}${d5}</span><span style="color:${PAL.axis};font-size:11px"> · ${semanticOf(v, d5)}</span>`;
      else h += `<br/><span style="color:${PAL.axis};font-size:11px">${semanticOf(v, null)}</span>`;
      const lc = latestComps(ci);
      const divg = divergenceOf(lc && lc.comps);
      if (divg) h += `<br/><span style="color:${PAL.warn};font-size:11px">${divg}</span>`;
      if (drv) h += `<br/><span style="color:${PAL.axis};font-size:11px">${drv}</span>`;
      return h;
    }},
    grid: { left: 10, right: 60, top: 32, bottom: 56, containLabel: true },
    xAxis: { type: 'category', data: hmDates.map(d => d.slice(5)),
      axisLabel: { color: PAL.axis, fontSize: 9, rotate: 45 },
      splitArea: { show: true, areaStyle: { color: PAL.splitA } } },
    yAxis: { type: 'category', data: yLabels,
      axisLabel: { fontSize: 8, color: PAL.txt },   // D1-8b-review: 组名行已移除, rich标记清理
      splitArea: { show: true, areaStyle: { color: PAL.splitA } } },
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
      orient: 'horizontal', left: 'center', top: 18,   // D1-8#4: 色标移grid上方(矮容器不再挤叠)
      itemWidth: 8, itemHeight: 8, itemGap: 0,
      textStyle: { fontSize: 10, color: PAL.axis },
      formatter: (v, v2) => {
        const mid = (v + v2) / 2 || v;
        return labelOf(mid);
      },
    },
    dataZoom: [{ type: 'inside', xAxisIndex: 0, start: zStart, end: zEnd },
               { type: 'slider', xAxisIndex: 0, start: zStart, end: zEnd,
      bottom: 26, height: 14, borderColor: PAL.a, fillerColor: 'rgba(42,120,214,0.1)' }],
    series: [{ type: 'heatmap', data: hmData,
      // S3-D: 常态无边框(降噪), 仅hover描边
      itemStyle: { borderWidth: 0 },
      progressive: 5000, progressiveThreshold: 10000,
      emphasis: { itemStyle: { shadowBlur: 4, shadowColor: PAL.shadow, borderColor: PAL.strong, borderWidth: 1.5 } } }]
  }, true);
  rankC.off('click');
  rankC.on('click', p => {
    const ci = sortedCis[p.data[1]];
    if (ci >= 0) toggleSel(ci);
  });
  // S3-A: hover联动右侧分量
  rankC.off('mouseover');
  rankC.on('mouseover', p => {
    const yi = (p.componentType === 'series' && p.data) ? p.data[1] : -1;   // D1-8#7 守卫
    const ci = yi >= 0 ? sortedCis[yi] : -1;
    if (ci >= 0) drawSideFor(ci); else hideSide();
  });
}
document.getElementById('btnClear').onclick = () => { selected = []; renderCards(); drawCurve(); drawLeft(); };
document.getElementById('q').addEventListener('input', e => { query = e.target.value.trim(); drawLeft(); });
document.getElementById('themeToggle').onclick = () => {
  const h = document.documentElement;
  const t = h.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  h.setAttribute('data-theme', t);
  // S3-D: 重取主题令牌并全量重绘(缩放态/图例隐藏态由既有机制保留)
  PAL = pal();
  drawLeft(); drawCurve(); drawSlider(); updateMkt();
};
// D1-8#2: 曲线↔热力图拖动联动已移除(降复杂度) — 曲线缩放只管曲线;
// 热力图窗口由自身 slider/inside/时间按钮控制, 全量日期常驻可平移到任意历史
// D1-8#6/#8: 曲线上任何交互(legend切换/点击/缩放) → 隐藏side; legend切换后回放缩放
curveC.on('legendselectchanged', () => {
  hideSide();
  const o = curveC.getOption(), z = o && o.dataZoom && o.dataZoom[0];
  if (z && typeof z.start === 'number')
    setTimeout(() => curveC.dispatchAction({ type: 'dataZoom', start: z.start, end: z.end }), 0);
});
curveC.on('click', hideSide);
curveC.on('datazoom', hideSide);

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
            borderColor: PAL.a, fillerColor: 'rgba(42,120,214,0.08)',
            textStyle: { fontSize: 8, color: PAL.axis } },
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
