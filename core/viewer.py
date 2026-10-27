# -*- coding: utf-8 -*-
"""交互式情绪行情图查看器: 生成自包含 HTML(内嵌 ECharts + 数据)

用法:
  python viewer.py                # 全历史 → out/sentiment_viewer.html
  python viewer.py --years 2      # 近两年
  python viewer.py --open         # 生成后自动用浏览器打开

功能: K线+成交量 / 情绪分 / 涨跌停家数 / 炸板率·接力率 五联面板,
      十字光标联动, 悬停右侧面板实时显示当日全部构成指标(原始值/分位分/权重/贡献),
      滚轮缩放 + 底部区间选择。
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402

from score import compute, load_cfg  # noqa: E402
from metrics import LABELS  # noqa: E402

ECHARTS_URLS = [
    "https://registry.npmmirror.com/echarts/5.5.1/files/dist/echarts.min.js",
    "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js",
]
VENDOR = BASE / "vendor" / "echarts.min.js"

# dataviz 参考色板
C = {
    "surface": "#fcfcfb", "page": "#f9f9f7", "ink": "#0b0b0b", "ink2": "#52514e",
    "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
    "blue": "#2a78d6", "red": "#e34948", "aqua": "#1baf7a", "yellow": "#eda100",
}


def _j(v):
    """NaN/inf → null"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (np.isnan(f) or np.isinf(f)) else round(f, 4)


DB_PATH = Path(__file__).resolve().parent / "data" / "sentiment.db"


def _read_aux(dates):
    """D1-14 H4/D1-15 I2: 副图与悬停数据(估值官方/两融/ETF/自算估值) — 对齐全量日期轴。

    四源各自独立容错(旧库可能只有部分表): 任一源缺失仅降级对应功能,
    不拖累其余源。
    """
    out = {"val": {}, "margin": [], "etf": {}, "mval": {}}
    import sqlite3 as _sq
    try:
        con = _sq.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001
        return out
    try:
        try:
            from fetch_val import VAL_INDEXES
            for code, name in VAL_INDEXES.items():
                pe = dict(con.execute(
                    "SELECT date, pe FROM index_val_daily WHERE code=?", (code,)).fetchall())
                pb = dict(con.execute(
                    "SELECT date, pb FROM index_val_daily WHERE code=?", (code,)).fetchall())
                out["val"][code] = {"n": name,
                                    "pe": [_j(pe.get(d)) for d in dates],
                                    "pb": [_j(pb.get(d)) for d in dates]}
        except Exception:  # noqa: BLE001
            out["val"] = {}
        try:
            mg = dict(con.execute("SELECT date, rzrqye FROM margin_daily").fetchall())
            out["margin"] = [_j(v / 1e8) if v is not None else None
                             for v in (mg.get(d) for d in dates)]      # 元→亿
        except Exception:  # noqa: BLE001
            out["margin"] = []
        try:
            from fetch_etf import ETF_INDEXES
            for code, name in ETF_INDEXES.items():
                am = dict(con.execute(
                    "SELECT date, amount FROM etf_daily WHERE code=?", (code,)).fetchall())
                out["etf"][code] = {"n": name,
                                    "amt": [_j(v / 1e5) if v is not None else None
                                            for v in (am.get(d) for d in dates)]}  # 千元→亿
        except Exception:  # noqa: BLE001
            out["etf"] = {}
        # D1-15 I2: 自算全A/宽基估值(整体法)
        try:
            cols = ("alla_pe", "alla_pb", "pe50", "pb50", "pe300", "pb300",
                    "pe500", "pb500", "pe1000", "pb1000")
            rows = con.execute(
                "SELECT date," + ",".join(cols) + " FROM mkt_val_daily").fetchall()
            for i, c in enumerate(cols):
                m = {r[0]: r[i + 1] for r in rows}
                out["mval"][c] = [_j(m.get(d)) for d in dates]
        except Exception:  # noqa: BLE001
            out["mval"] = {}
    finally:
        con.close()
    return out


def build_payload(cfg, years=None):
    df, comp, score_df = compute(cfg)
    n = len(df)
    if years:
        cutoff = df["date"].max() - pd_offset(years)
        idx0 = df.index[df["date"] >= cutoff]
        i0 = int(idx0[0]) if len(idx0) else 0
    else:
        i0 = 0

    # S2.1-B: 全量日期为统一轴; 主图数据(K线/情绪分)在 years 窗口外填 null
    # 附图数据(涨停/涨跌/炸板/接力)全量——拖动 dataZoom 可看全历史
    keys = [k for k in cfg["weights"] if k in comp.columns]
    payload = {
        "dates": [d.strftime("%Y-%m-%d") for d in df["date"]],  # 统一用全量日期
        "k": [[_j(r["open"]), _j(r["close"]), _j(r["low"]), _j(r["high"])]
              if i >= i0 else [None, None, None, None]
              for i, (_, r) in enumerate(df.iterrows())],
        "vol": [_j(v / 1e8) if i >= i0 else None
                for i, v in enumerate(df["vol_total"])],
        "pct": [_j(v) if i >= i0 else None for i, v in enumerate(df["chg"])],
        "score": [_j(v) if i >= i0 else None for i, v in enumerate(score_df["score"])],
        "v1": [_j(v) if i >= i0 else None for i, v in enumerate(score_df["v1_score"])],
        "stage": list(score_df["stage"]),
        "zt": [_j(v) for v in df["zt_count"]],
        "dt": [_j(-v) for v in df["dt_count"]],
        "zb": [_j(v) for v in df["zb_rate"]],
        "jl": [_j(v) for v in df["jl_rate"]],
        "raw": {k: [_j(v) if i >= i0 else None
                    for i, v in enumerate(df[k])] for k in keys},
        "cs": {k: [_j(v) if i >= i0 else None
                   for i, v in enumerate(comp[k])] for k in keys},
        "labels": {k: LABELS.get(k, k) for k in keys},
        "weights": {k: cfg["weights"][k] for k in keys},
        "n": n,
    }
    # S2.2-B: 附图各序列数据起始日
    def _first_date(series):
        for i, v in enumerate(series):
            if v is not None:
                return payload["dates"][i]
        return None
    payload["sub_starts"] = {
        "zt": _first_date(payload["zt"]),
        "dt": _first_date(payload["dt"]),
        "zb": _first_date(payload["zb"]),
        "jl": _first_date(payload["jl"]),
        "score": _first_date(payload["score"]),
    }
    # D1-14 H4: 估值/两融/ETF 副图(表缺失→空, 副图自动隐藏)
    aux = _read_aux(payload["dates"])
    payload.update(aux)
    return payload


def pd_offset(years):
    from pandas import DateOffset
    return DateOffset(years=years)


def get_echarts_inline() -> str:
    """vendor 缓存 → 下载; 都失败则返回 CDN 引用标签"""
    if VENDOR.exists() and VENDOR.stat().st_size > 500_000:
        return VENDOR.read_text(encoding="utf-8")
    VENDOR.parent.mkdir(exist_ok=True)
    for url in ECHARTS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                js = r.read().decode("utf-8")
            if len(js) > 500_000:
                VENDOR.write_text(js, encoding="utf-8")
                print(f"[echarts] 已下载并缓存 ({len(js) // 1024}KB) <- {url}")
                return js
        except Exception as e:  # noqa: BLE001
            print(f"[echarts] {url} 失败: {e}")
    print("[echarts] 内联失败, 改用CDN引用(打开页面需联网)")
    return None


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>A股市场情绪 · 交互行情图</title>
<style>
  :root {
    --surface: __C_SURFACE__; --page: __C_PAGE__; --ink: __C_INK__;
    --ink2: __C_INK2__; --muted: __C_MUTED__; --grid: __C_GRID__;
    --axis: __C_AXIS__; --blue: __C_BLUE__; --red: __C_RED__;
    --aqua: __C_AQUA__; --yellow: __C_YELLOW__;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--page); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
  }
  header {
    display: flex; align-items: baseline; gap: 18px; flex-wrap: wrap;
    padding: 14px 20px 10px; border-bottom: 1px solid var(--grid);
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .hero { font-size: 30px; font-weight: 700; font-variant-numeric: tabular-nums; }
  header .sub { color: var(--ink2); }
  .chip {
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-size: 12px; color: #fff; background: var(--muted); vertical-align: middle;
  }
  .wrap { display: flex; gap: 14px; padding: 14px 20px; align-items: flex-start; }
  #chart { flex: 1 1 auto; min-width: 0; height: 470px; background: var(--surface);
           border: 1px solid var(--grid); border-radius: 10px; }
  aside {
    flex: 0 0 330px; max-height: 92vh; overflow-y: auto;
    background: var(--surface); border: 1px solid var(--grid);
    border-radius: 10px; padding: 14px 16px; position: sticky; top: 12px;
  }
  aside h2 { font-size: 13px; color: var(--ink2); margin: 0 0 4px; font-weight: 600; }
  .stage-line { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
  .stage-line .big { font-size: 34px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .kv { color: var(--ink2); line-height: 1.7; }
  .kv b { color: var(--ink); font-variant-numeric: tabular-nums; }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; }
  th, td { padding: 3px 4px; text-align: right; font-variant-numeric: tabular-nums;
           border-bottom: 1px solid var(--grid); white-space: nowrap; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  td:first-child, th:first-child { text-align: left; }
  .bar-cell { width: 90px; }
  .track { position: relative; height: 10px; background: var(--grid); border-radius: 2px; }
  .track::after { content: ""; position: absolute; left: 50%; top: -2px; bottom: -2px;
                  width: 1px; background: var(--axis); }
  .fill { position: absolute; top: 1px; bottom: 1px; border-radius: 2px; }
  footer { color: var(--muted); padding: 8px 20px 18px; }
  .hint { color: var(--muted); font-size: 12px; }
  .btn-sm {
    border: 1px solid var(--axis); background: var(--surface); color: var(--ink2);
    border-radius: 6px; padding: 3px 10px; font-size: 11px; cursor: pointer;
  }
  .btn-sm:hover { border-color: var(--blue); color: var(--blue); }
  #subPanelCtl label { cursor: pointer; user-select: none; }
  #subPanelCtl input { margin-right: 2px; }
</style>
</head>
<body>
<script>
window.onerror = function(msg, src, line, col) {
  var d = document.createElement("div");
  d.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:999;background:#d03b3b;color:#fff;padding:6px 12px;font:12px monospace";
  d.textContent = "页面脚本错误: " + msg + " @" + line + ":" + col + " (请截图反馈)";
  document.body.appendChild(d);
};
</script>
<header>
  <h1>A股市场情绪 · 交互行情图</h1>
  <span class="hero" id="heroScore">--</span>
  <span class="chip" id="heroStage">--</span>
  <span class="sub" id="heroDate">--</span>
  <span class="sub">V1量价对照 <b id="heroV1">--</b></span>
  <span style="flex:1"></span>
  <button class="btn-sm" onclick="setZoom(250)">最新1年</button>
  <button class="btn-sm" onclick="setZoom(500)">近2年</button>
  <button class="btn-sm" onclick="setZoom(0)">全部</button>
</header>
<div class="wrap">
  <div id="chart"></div>
  <aside id="panel">加载中…</aside>
</div>
<div id="subPanelCtl" style="padding:0 20px 6px;display:flex;gap:10px;align-items:center;font-size:12px;color:#898781">
  <span>底部面板:</span>
  <label><input type="checkbox" id="ckZb" checked> 炸板率</label>
  <label><input type="checkbox" id="ckJl" checked> 接力率</label>
  <label><input type="checkbox" id="ckUd"> 涨跌家数</label>
  <label><input type="checkbox" id="ckVol"> 成交额</label>
  <label><input type="checkbox" id="ckRsi"> RSI</label>
  <label><input type="checkbox" id="ckBias"> 乖离率</label>
</div>
<div id="auxPanelCtl" style="padding:0 20px 6px;display:flex;gap:10px;align-items:center;font-size:12px;color:#898781;flex-wrap:wrap">
  <span>分析副图(D1-14, 开关记忆):</span>
  <label><input type="checkbox" id="ckPE" checked> 指数PE</label>
  <label><input type="checkbox" id="ckPB" checked> 指数PB</label>
  <label><input type="checkbox" id="ckMargin" checked> 两融余额</label>
  <label><input type="checkbox" id="ckEtf" checked> 宽基ETF成交额</label>
</div>
<footer>
  鼠标在图上移动查看当日构成 · 滚轮缩放 · 底部滑块选区间 ·
  情绪分是温度计不是买卖信号 · 生成于 __GENERATED__
</footer>
<script>__ECHARTS__</script>
<script>
const D = __PAYLOAD__;
const IDX = {}; D.dates.forEach((d, i) => IDX[d] = i);
const C = { blue: "__C_BLUE__", red: "__C_RED__", aqua: "__C_AQUA__",
            yellow: "__C_YELLOW__", ink: "__C_INK__", ink2: "__C_INK2__",
            muted: "__C_MUTED__", grid: "__C_GRID__", axis: "__C_AXIS__" };
const KEYS = Object.keys(D.weights);
const nullish = v => (v === null || v === undefined);

const chart = echarts.init(document.getElementById("chart"));
// S2.1-A: 两列布局(K线+量 左列, 情绪分+涨停 右列, 炸板率全宽)
const grids = [
  { top: 56,  height: 220, left: '3%',  width: '44%' },  // K线(左上)
  { top: 286, height: 60,  left: '3%',  width: '44%' },  // 成交量(左中)
  { top: 56,  height: 220, left: '52%', width: '46%' },  // 情绪分(右上)
  { top: 286, height: 60,  left: '52%', width: '46%' },  // 涨停/跌停(右中)
  { top: 360, height: 80,  left: '3%',  width: '95%' },  // 炸板率/接力率(全宽底)
];
const catAxis = (gi) => ({
  type: "category", gridIndex: gi, data: D.dates, show: gi === 4,
  axisLine: { lineStyle: { color: C.axis } },
  axisLabel: { color: C.muted }, axisTick: { show: false },
});
const valAxis = (gi) => ({
  type: "value", gridIndex: gi, scale: true,
  axisLabel: { color: C.muted }, splitLine: { lineStyle: { color: C.grid } },
});

const baseOption = {
  animation: false,
  backgroundColor: "transparent",
  // S2.2-A: 面板标题
  title: [
    { text: "上证指数", left: "4%", top: "38px", textStyle: { fontSize: 11, color: C.ink2 } },
    { text: "情绪分", left: "53%", top: "38px", textStyle: { fontSize: 11, color: C.ink2 } },
    { text: "底部面板(可勾选)", left: "4%", top: "352px", textStyle: { fontSize: 10, color: C.muted } },
  ],
  axisPointer: { link: [{ xAxisIndex: "all" }],
                 lineStyle: { color: C.axis } },
  tooltip: {
    trigger: "axis",
    axisPointer: { type: "cross", label: { backgroundColor: C.ink2 } },
    backgroundColor: "rgba(252,252,251,0.96)", borderColor: C.grid,
    textStyle: { color: C.ink, fontSize: 12 },
    formatter: (ps) => {
      const i = ps[0].dataIndex;
      const k = D.k[i], pct = D.pct[i];
      let h = `<b>${D.dates[i]}</b>`;
      if (k && !nullish(k[1])) h += `<br/>上证 收 <b>${k[1]}</b> (${nullish(pct) ? "--" : (pct >= 0 ? "+" : "") + pct}%) 开 ${k[0]} 高 ${k[3]} 低 ${k[2]}`;
      h += `<br/>两市量 ${nullish(D.vol[i]) ? "--" : D.vol[i]} 亿手`;
      const s = D.score[i];
      h += `<br/>情绪分 <b style="color:${nullish(s) ? C.muted : (s >= 0 ? C.red : C.blue)}">${nullish(s) ? "--" : (s >= 0 ? "+" : "") + s}</b> · ${D.stage[i] || "--"}`;
      if (!nullish(D.zt[i])) h += `<br/>涨停 <b>${D.zt[i]}</b> / 跌停 <b>${-D.dt[i]}</b> · 炸板率 ${D.zb[i]}% · 接力率 ${nullish(D.jl[i]) ? "--" : D.jl[i]}%`;
      // S2.2-C: 追加详细分量信息
      const v1 = D.v1[i];
      if (!nullish(v1)) h += `<br/><span style="color:#898781">V1对照 ${v1 >= 0 ? "+" : ""}${v1}</span>`;
      if (i > 0 && !nullish(D.score[i-1]) && !nullish(s)) {
        const d = s - D.score[i-1];
        h += ` <span style="color:${d >= 0 ? C.red : C.blue}">环比${d >= 0 ? "+" : ""}${d.toFixed(1)}</span>`;
      }
      // 分量明细(来自 cs, key与config weights一致)
      const compBits = [];
      const cKeys = ['rsi', 'vol_total', 'bias20', 'zt_count', 'amp'];
      const cNms = {rsi: 'RSI', vol_total: '量能', bias20: '乖离', zt_count: '涨停', amp: '振幅'};
      for (const ck of cKeys) {
        if (D.cs && D.cs[ck] && !nullish(D.cs[ck][i]))
          compBits.push(`${cNms[ck]} ${D.cs[ck][i] > 0 ? '+' : ''}${Math.round(D.cs[ck][i])}`);
      }
      if (compBits.length) h += `<br/><span style="color:#898781;font-size:11px">${compBits.join(' · ')}</span>`;
      // D1-15 I2: 估值全套(自算整体法) + 两融/ETF
      const mv = D.mval || {};
      const fmtPE = (k) => nullish(mv[k] && mv[k][i]) ? null : mv[k][i].toFixed(1);
      const fmtPB = (k) => nullish(mv[k] && mv[k][i]) ? null : mv[k][i].toFixed(2);
      const peBits = [];
      if (!nullish(mv.alla_pe && mv.alla_pe[i]))
        peBits.push(`全A ${fmtPE('alla_pe')}${fmtPB('alla_pb') ? '/' + fmtPB('alla_pb') : ''}`);
      if (mv.pe50 && !nullish(mv.pe50[i])) peBits.push(`50 ${fmtPE('pe50')}`);
      if (mv.pe300 && !nullish(mv.pe300[i]))
        peBits.push(`300 ${fmtPE('pe300')}${fmtPB('pb300') ? '/' + fmtPB('pb300') : ''}`);
      if (mv.pe500 && !nullish(mv.pe500[i])) peBits.push(`500 ${fmtPE('pe500')}`);
      if (mv.pe1000 && !nullish(mv.pe1000[i])) peBits.push(`1000 ${fmtPE('pe1000')}`);
      if (peBits.length)
        h += `<br/><span style="color:#898781;font-size:11px">PE ${peBits.join(' · ')}</span>`;
      if (D.margin && !nullish(D.margin[i]))
        h += ` <span style="color:#898781;font-size:11px">· 两融 ${D.margin[i]}亿</span>`;
      if (D.etfSum && !nullish(D.etfSum[i]))
        h += ` <span style="color:#898781;font-size:11px">· 宽基ETF ${Math.round(D.etfSum[i])}亿</span>`;
      return h;
    },
  },
  grid: grids.map(g => ({ ...g, containLabel: true, right: 20 })),
  xAxis: grids.map((_, gi) => catAxis(gi)),
  yAxis: [
    valAxis(0),                                   // 0 K线
    valAxis(1),                                   // 1 量
    { type: "value", gridIndex: 2, min: -105, max: 105,   // 2 情绪分(定域)
      axisLabel: { color: C.muted },
      splitLine: { lineStyle: { color: C.grid } } },
    valAxis(3),                                   // 3 家数
    valAxis(4),                                   // 4 %
  ],
  dataZoom: [
    { type: "inside", xAxisIndex: [0, 1, 2, 3, 4] },
    { type: "slider", xAxisIndex: [0, 1, 2, 3, 4], bottom: 4, height: 20,
      borderColor: C.grid, fillerColor: "rgba(42,120,214,0.10)",
      handleStyle: { color: C.blue }, textStyle: { color: C.muted },
      start: Math.max(0, 100 - (250 / D.n) * 100) },  // S2.2-A: 默认近~1年窗口
  ],
  series: [
    { name: "上证指数", type: "candlestick", xAxisIndex: 0, yAxisIndex: 0,
      data: D.k, barMaxWidth: 12,
      itemStyle: { color: C.red, color0: C.aqua,
                   borderColor: C.red, borderColor0: C.aqua } },
    { name: "两市成交量(亿手)", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
      data: D.vol, barMaxWidth: 12, itemStyle: { color: "rgba(137,135,129,0.55)" } },
    { name: "情绪分", type: "line", xAxisIndex: 2, yAxisIndex: 2,
      data: D.score, showSymbol: false, lineStyle: { width: 2, color: C.blue },
      itemStyle: { color: C.blue },
      markArea: {
        silent: true, itemStyle: { color: "rgba(227,73,72,0.06)" },
        data: [[{ yAxis: 60 }, { yAxis: 100 }], [{ yAxis: -100 }, { yAxis: -60 }]],
      },
      markLine: {
        silent: true, symbol: "none", label: { show: false },
        lineStyle: { color: C.axis, type: "dashed", width: 1 },
        data: [{ yAxis: 0 }, { yAxis: 60 }, { yAxis: -60 },
               { yAxis: 80, lineStyle: { color: C.axis, opacity: 0.6 } },
               { yAxis: -80, lineStyle: { color: C.axis, opacity: 0.6 } }],
      } },
    { name: "涨停家数", type: "bar", xAxisIndex: 3, yAxisIndex: 3,
      data: D.zt, barMaxWidth: 8, itemStyle: { color: C.red } },
    { name: "跌停家数", type: "bar", xAxisIndex: 3, yAxisIndex: 3,
      data: D.dt, barMaxWidth: 8, itemStyle: { color: C.aqua } },
    // D1-14r: 炸板率/接力率不进base series — 由底部面板勾选(SUB_OPTS默认勾选)
    // 统一提供, 避免render()再拼时重复添加(旧rebuildSubPanel的top5语义)
  ],
  // S2.2-B: 附图数据起始日标注
  graphic: (() => {
    const els = [];
    const ss = D.sub_starts || {};
    const mk = (text, x, y) => ({
      type: 'text', left: x, top: y, silent: true, z: 100,
      style: { text, fontSize: 9, fill: '#898781', fontFamily: 'sans-serif' } });
    if (ss.zt) els.push(mk(`涨停起 ${ss.zt}`, '52%', '348px'));
    if (ss.zb) els.push(mk(`炸板率起 ${ss.zb}`, '3%', '438px'));
    if (ss.jl && ss.jl !== ss.zb) els.push(mk(`接力率起 ${ss.jl}`, '20%', '438px'));
    return els;
  })(),
};

// ---- D1-14 H4: 分析副图(估值PE/PB·两融余额·宽基ETF) — 可折叠+开关记忆 ----
const P_COLORS = ['#2f5cd6', '#e34448', '#1baf7a', '#eda100', '#8e44ad', '#0f9bd7', '#7f8c8d'];
const VAL_SHOW = ['000016.SH', '000300.SH', '000905.SH', '000852.SH'];   // 50/300/500/1000
const AUX_STORE = 'auxPanels_v1';
function auxState() {
  let st = null;
  try { st = JSON.parse(localStorage.getItem(AUX_STORE) || 'null'); } catch (e) {}
  if (!st) st = { pe: true, pb: true, margin: true, etf: true };
  return st;
}
function saveAuxState(st) {
  try { localStorage.setItem(AUX_STORE, JSON.stringify(st)); } catch (e) {}
}
// ETF 合计(tooltip用)
D.etfSum = (() => {
  if (!D.etf) return null;
  const codes = Object.keys(D.etf), n = D.dates.length;
  const sum = new Array(n).fill(null);
  codes.forEach(c => {
    const a = D.etf[c].amt;
    for (let i = 0; i < n; i++)
      if (a[i] != null) sum[i] = (sum[i] || 0) + a[i];
  });
  return sum;
})();
function valSeries(gi, k) {          // k='pe'|'pb' — 4宽基多线
  const out = [];
  VAL_SHOW.forEach((code, i) => {
    const o = D.val && D.val[code];
    if (!o) return;
    out.push({ name: `${o.n}${k === 'pe' ? 'PE' : 'PB'}`, type: 'line',
      xAxisIndex: gi, yAxisIndex: gi, data: o[k], showSymbol: false,
      lineStyle: { width: 1.3, color: P_COLORS[i] }, itemStyle: { color: P_COLORS[i] } });
  });
  return out;
}
function marginSeries(gi) {
  return [{ name: '两融余额(亿)', type: 'line', xAxisIndex: gi, yAxisIndex: gi,
    data: D.margin, showSymbol: false, lineStyle: { width: 1.4, color: '#6250d6' },
    itemStyle: { color: '#6250d6' }, areaStyle: { opacity: 0.08 } }];
}
function etfSeries(gi) {             // 8只堆叠=总额观感, 图例式查看不便→色阶
  const out = [];
  Object.keys(D.etf || {}).forEach((c, i) => {
    const o = D.etf[c];
    out.push({ name: o.n, type: 'bar', stack: 'etf', xAxisIndex: gi, yAxisIndex: gi,
      data: o.amt, barMaxWidth: 10,
      itemStyle: { color: P_COLORS[i % P_COLORS.length], opacity: 0.85 } });
  });
  return out;
}
const PANELS = [
  { key: 'pe',     id: 'ckPE',     name: '宽基指数PE(50/300/500/1000)', h: 84, build: gi => valSeries(gi, 'pe') },
  { key: 'pb',     id: 'ckPB',     name: '宽基指数PB',                  h: 84, build: gi => valSeries(gi, 'pb') },
  { key: 'margin', id: 'ckMargin', name: '两融余额(亿, 沪深北合计)',    h: 84, build: gi => marginSeries(gi) },
  { key: 'etf',    id: 'ckEtf',    name: '宽基ETF成交额(亿·8只堆叠)',   h: 84, build: gi => etfSeries(gi) },
];
function auxDataEmpty(p) {
  if (p.key === 'margin') return !D.margin || !D.margin.some(v => v != null);
  if (p.key === 'etf') return !D.etf || !Object.keys(D.etf).length;
  return !D.val || !VAL_SHOW.some(c => D.val[c]);
}
function renderAux() {
  const st = auxState();
  const vis = PANELS.filter(p => st[p.key] && !auxDataEmpty(p));
  // 动态画布高度(基础470 + 每副图112)
  const el = document.getElementById('chart');
  el.style.height = (470 + vis.length * 112) + 'px';
  chart.resize();
  const opt = Object.assign({}, baseOption, {   // 浅克隆可变数组, tooltip.formatter按引用保留
    grid: [...baseOption.grid], xAxis: [...baseOption.xAxis],
    yAxis: [...baseOption.yAxis], title: [...baseOption.title],
    series: [...baseOption.series],
    dataZoom: baseOption.dataZoom.map(z => Object.assign({}, z)),
  });
  let top = 466;
  vis.forEach((p, pi) => {
    const gi = 5 + pi;
    opt.grid.push({ top, height: p.h, left: '3%', width: '95%', containLabel: true, right: 20 });
    opt.xAxis.push(catAxis(gi));
    opt.yAxis.push(valAxis(gi));
    opt.title.push({ text: p.name, left: '3%', top: (top - 14) + 'px',
                     textStyle: { fontSize: 10, color: C.muted } });
    opt.series.push(...p.build(gi));
    top += p.h + 28;
  });
  // 副图x轴标签显示(最末可见grid的axisLabel展示)
  if (vis.length) {
    const lastGi = 5 + vis.length - 1;
    opt.xAxis = opt.xAxis.map((a, i) =>
      i === lastGi ? { ...a, show: true } : { ...a, show: i === lastGi });
  }
  const allIdx = opt.grid.map((_, i) => i);
  opt.dataZoom[0].xAxisIndex = allIdx;
  opt.dataZoom[1].xAxisIndex = allIdx;
  // 底部面板勾选线(原rebuildSubPanel逻辑并入)
  for (const o of SUB_OPTS) {
    const ck = document.getElementById(o.id);
    if (ck && ck.checked) { const s = subSeriesFor(o.id); if (s) opt.series.push(s); }
  }
  // 保留当前缩放窗口
  let zs = null;
  try { const cur = chart.getOption();
    if (cur && cur.dataZoom && cur.dataZoom[0] && cur.dataZoom[0].start != null)
      zs = { s: cur.dataZoom[0].start, e: cur.dataZoom[0].end };
  } catch (e) {}
  chart.setOption(opt, { replaceMerge: ['grid', 'xAxis', 'yAxis', 'series', 'title', 'dataZoom'] });
  if (zs) chart.dispatchAction({ type: 'dataZoom', start: zs.s, end: zs.e });
}
function initAuxCtl() {
  const st = auxState();
  PANELS.forEach(p => {
    const el = document.getElementById(p.id);
    if (el) { el.checked = !!st[p.key];
      el.addEventListener('change', () => {
        st[p.key] = el.checked; saveAuxState(st); renderAux();
      }); }
  });
}

// ---- 右侧详情面板: 悬停联动显示当日全部构成 ----
const fmtv = (v, plus) => {
  if (nullish(v)) return "--";
  const s = typeof v === "number" && Number.isInteger(v) ? v.toString()
          : v.toFixed(2);
  return plus && v >= 0 ? "+" + s : s;
};
function barHTML(v) {
  if (nullish(v)) return '<div class="track"></div>';
  const w = Math.min(Math.abs(v), 100) / 2;
  const pos = v >= 0
    ? `left:50%;width:${w}%;background:${C.red}`
    : `left:${50 - w}%;width:${w}%;background:${C.blue}`;
  return `<div class="track"><div class="fill" style="${pos}"></div></div>`;
}
function render(i) {
  const s = D.score[i];
  const d = D.dates[i];
  const k = D.k[i];
  const prev = i > 0 ? D.score[i - 1] : null;
  const delta = (!nullish(s) && !nullish(prev)) ? s - prev : null;
  document.getElementById("heroDate").textContent = d;
  const hs = document.getElementById("heroScore");
  hs.textContent = nullish(s) ? "--" : (s >= 0 ? "+" : "") + s.toFixed(1);
  hs.style.color = nullish(s) ? C.muted : (s >= 0 ? C.red : C.blue);
  const chip = document.getElementById("heroStage");
  chip.textContent = D.stage[i] || "--";
  chip.style.background = nullish(s) ? C.muted : (s >= 60 ? C.red : s <= -60 ? C.blue : C.muted);
  document.getElementById("heroV1").textContent = nullish(D.v1[i]) ? "--" : (D.v1[i] >= 0 ? "+" : "") + D.v1[i].toFixed(1);

  let h = `<h2>${d} 当日构成</h2>`;
  h += `<div class="stage-line"><span class="big" style="color:${nullish(s) ? C.muted : (s >= 0 ? C.red : C.blue)}">${nullish(s) ? "--" : (s >= 0 ? "+" : "") + (Math.round(s * 10) / 10)}</span>
        <span class="chip" style="background:${nullish(s) ? C.muted : (s >= 60 ? C.red : s <= -60 ? C.blue : C.muted)}">${D.stage[i] || "--"}</span></div>`;
  h += `<div class="kv">前日差 <b>${nullish(delta) ? "--" : (delta >= 0 ? "+" : "") + delta.toFixed(1)}</b> · V1量价对照 <b>${nullish(D.v1[i]) ? "--" : (D.v1[i] >= 0 ? "+" : "") + D.v1[i].toFixed(1)}</b><br>`;
  if (k && !nullish(k[1])) h += `上证 收<b>${k[1]}</b> (${fmtv(D.pct[i], true)}%) · 两市量 <b>${fmtv(D.vol[i])}</b> 亿手<br>`;
  if (!nullish(D.zt[i])) h += `涨停 <b>${D.zt[i]}</b> 跌停 <b>${-D.dt[i]}</b> · 炸板率 <b>${fmtv(D.zb[i])}</b>% · 接力率 <b>${fmtv(D.jl[i])}</b>%`;
  h += `</div>`;
  h += `<table><tr><th>分量</th><th>原始值</th><th class="bar-cell">分位分</th><th>权重</th><th>贡献</th></tr>`;
  let rows = KEYS.map(kk => {
    const w = D.weights[kk];
    const sv = D.cs[kk][i];
    return { kk, raw: D.raw[kk][i], s: sv, w, c: nullish(sv) ? null : sv * w };
  });
  const wsum = rows.filter(r => !nullish(r.s)).reduce((a, r) => a + r.w, 0) || 1;
  rows.forEach(r => { r.w = r.w / wsum; r.c = nullish(r.s) ? null : r.s * r.w; });
  rows.forEach(r => {
    h += `<tr><td>${D.labels[r.kk]}</td><td>${fmtv(r.raw)}</td>
          <td class="bar-cell">${barHTML(r.s)}</td>
          <td>${(r.w * 100).toFixed(0)}%</td>
          <td style="color:${nullish(r.c) ? C.muted : (r.c >= 0 ? C.red : C.blue)}">${fmtv(r.c, true)}</td></tr>`;
  });
  h += `</table>`;
  if (nullish(s)) h += `<p class="hint">该日评分窗口数据不足(池数据2025-10起)。</p>`;
  document.getElementById("panel").innerHTML = h;
}

let curIdx = (() => { for (let i = D.n - 1; i >= 0; i--) if (!nullish(D.score[i])) return i; return D.n - 1; })();
render(curIdx);
chart.on("updateAxisPointer", (e) => {
  const ax = (e.axesInfo || []).find(a => a.axisDim === "x");
  if (!ax) return;
  const v = ax.value;
  let i = typeof v === "number" ? v : IDX[v];
  if (i === undefined || i === curIdx) return;
  curIdx = i; render(i);
});
window.addEventListener("resize", () => chart.resize());

// S2.2-A: 快捷缩放
function setZoom(days) {
  if (!days) chart.dispatchAction({ type: 'dataZoom', start: 0, end: 100 });
  else {
    const pct = Math.max(0, 100 - (days / D.n) * 100);
    chart.dispatchAction({ type: 'dataZoom', start: pct, end: 100 });
  }
}

// S2.2-A: 底部面板可选(checkbox) — D1-14 H4: 显隐并入render统一重排
const SUB_OPTS = [
  { id: 'ckZb',  name: '炸板率%',   key: 'zb',  color: '#eda100' },
  { id: 'ckJl',  name: '接力率%',   key: 'jl',  color: '#1baf7a' },
  { id: 'ckRsi', name: 'RSI',      key: 'rsi', color: '#eb6834' },
  { id: 'ckBias', name: '20日乖离',  key: 'bias20', color: '#6250d6' },
  { id: 'ckVol', name: '成交额(亿)', key: 'vol', color: '#898781' },
];
function subSeriesFor(id) {
  const opt = SUB_OPTS.find(o => o.id === id);
  if (!opt) return null;
  let data = null;
  if (opt.key === 'zb') data = D.zb;
  else if (opt.key === 'jl') data = D.jl;
  else if (opt.key === 'vol') data = D.vol;
  else if (D.cs && D.cs[opt.key]) data = D.cs[opt.key];
  if (!data) return null;
  return { name: opt.name, type: 'line', xAxisIndex: 4, yAxisIndex: 4,
    data: data, showSymbol: false,
    lineStyle: { width: 1.4, color: opt.color }, itemStyle: { color: opt.color } };
}
for (const opt of SUB_OPTS) {
  const el = document.getElementById(opt.id);
  if (el) el.addEventListener('change', renderAux);
}
// D1-14 H4: 初始渲染(副图开关记忆 + 底部面板勾选)
initAuxCtl();
renderAux();
</script>
</body>
</html>
"""


def generate_html(cfg=None, years=None, out=None):
    """生成交互式查看器 HTML, 返回文件路径(供 admin.py 调用)"""
    import time as _time

    cfg = cfg or load_cfg()
    payload = build_payload(cfg, years)
    js = get_echarts_inline()
    echarts_tag = f'<script src="{ECHARTS_URLS[0]}"></script>' if js is None else None

    html = (HTML
            .replace("__C_SURFACE__", C["surface"]).replace("__C_PAGE__", C["page"])
            .replace("__C_INK2__", C["ink2"]).replace("__C_INK__", C["ink"])
            .replace("__C_MUTED__", C["muted"]).replace("__C_GRID__", C["grid"])
            .replace("__C_AXIS__", C["axis"]).replace("__C_BLUE__", C["blue"])
            .replace("__C_RED__", C["red"]).replace("__C_AQUA__", C["aqua"])
            .replace("__C_YELLOW__", C["yellow"])
            .replace("__GENERATED__", _time.strftime("%Y-%m-%d %H:%M"))
            .replace("__ECHARTS__", js if js is not None else "/*cdn*/")
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
    if echarts_tag:
        html = html.replace("/*cdn*/", "").replace("<script>/*cdn*/", echarts_tag + "<script>")

    out = Path(out) if out else (BASE / "out" / "sentiment_viewer.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[viewer] {out}  ({out.stat().st_size / 1e6:.2f} MB, "
          f"{payload['n']} 交易日, {payload['dates'][0]} ~ {payload['dates'][-1]})")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    out = generate_html(years=args.years, out=args.out)
    if args.open:
        import webbrowser
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
