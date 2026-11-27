// 情绪图页脚本 × 真实 ECharts: 用仓库自带的 core/vendor/echarts.min.js(服务端渲染模式, 不需要浏览器)跑
// 真实的页面脚本, 驱动 mount / activate / renderAux / setZoom 并渲染出 SVG。
// viewer_behavior.js 用的是手写的 ECharts 桩 — 它不校验传进去的配置, "真实 ECharts 会抛错"的这一类问题抓不到
// (11-27 实测: 指数页签 K 线窗口外写成裸 null, 点页签整张图停住; 颜色占位符少写两个字符, 阴线全变红)。
// 这里的页面是 generate_html 真正生成出来的(占位符已替换), 由 tests/test_mi3_viewer.py 调用:
//     node viewer_echarts_real.js <全历史.html> <近两年.html>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ECHARTS = path.resolve(__dirname, '..', 'core', 'vendor', 'echarts.min.js');
const COLORS = {blue: '#2a78d6', red: '#e34948', aqua: '#1baf7a', yellow: '#eda100', ink: '#0b0b0b', ink2: '#52514e',
                muted: '#898781', grid: '#e1e0d9', axis: '#c3c2b7'};
const SH_NAMES = ['上证指数', '两市成交量(亿手)', '情绪分', '涨停家数', '跌停家数'];

function assert(cond, msg) { if (!cond) throw new Error(msg); }

// ── 最小 DOM: 页面脚本自己只用到很少几个方法 ──
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.style = {}; this.dataset = {}; this._html = ''; this.textContent = '';
    this.value = ''; this.type = ''; this.checked = false; this.parent = null; this._l = {}; this.attrs = {};
    this.className = '';
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  addEventListener(ev, fn) { (this._l[ev] = this._l[ev] || []).push(fn); }
  fire(ev) { (this._l[ev] || []).forEach(fn => fn({})); }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  removeAttribute(n) { delete this.attrs[n]; }
  getBoundingClientRect() { return {left: 0, top: 0, width: 1400, height: 2200, right: 1400, bottom: 2200, x: 0, y: 0}; }
}

function open(htmlPath, store) {                       // 在一个新上下文里打开一张页面
  const html = fs.readFileSync(htmlPath, 'utf8');
  assert(html.includes('const BASE = {'), '不是生成出来的页面: ' + htmlPath);
  const script = html.match(/<script>([\s\S]*?)<\/script>/g)
    .map(x => x.replace(/<\/?script>/g, '')).sort((a, b) => b.length - a.length)[0];
  const els = {}, evtEls = [];
  for (const m of html.matchAll(/<input[^>]*\sid="([^"]+)"[^>]*>/g)) {     // 页面里的静态控件(按 id)
    const el = new El('input');
    el.type = /type="checkbox"/.test(m[0]) ? 'checkbox' : '';
    el.checked = /\schecked\b/.test(m[0]);
    els[m[1]] = el;
  }
  for (const m of html.matchAll(/<input[^>]*class="ckEvt"[^>]*>/g)) {      // 事件等级勾选(页面按 class 查)
    const el = new El('input');
    el.type = 'checkbox';
    el.value = /value="(\d)"/.exec(m[0])[1];
    el.checked = /\schecked\b/.test(m[0]);
    evtEls.push(el);
  }
  const $id = id => els[id] || (els[id] = new El('div'));
  const querySelectorAll = s => {                       // 页面只按两种方式查: 事件等级勾选(.ckEvt)、叠加勾选框
    if (s === '.ckEvt') return evtEls;
    const m = /^#(\w+) input$/.exec(s);
    if (m) return $id(m[1]).children.map(lb => lb.children[0]).filter(Boolean);
    return [];
  };
  // ECharts 量文字宽度要用 canvas: 给它一个能量宽的假 canvas(宽度按字数估), 其余绘图方法都是空操作
  const canvasCtx = new Proxy({}, {get: (t, k) => (k === 'measureText' ? s => ({width: String(s).length * 7}) : () => {}),
                                   set: () => true});
  const createElement = tag => (String(tag).toLowerCase() === 'canvas'
    ? {style: {}, width: 0, height: 0, getContext: () => canvasCtx} : new El(tag));
  const document = {
    title: '', body: new El('body'), getElementById: $id, createElement,
    createTextNode: t => { const e = new El('#text'); e.textContent = t; return e; },
    querySelectorAll, addEventListener() {},
  };
  const localStorage = {_m: Object.assign({}, store || {}),
    getItem(k) { return k in this._m ? this._m[k] : null; }, setItem(k, v) { this._m[k] = String(v); }};
  // ECharts 与页面脚本放在同一个上下文里; 给上下文一个 process, ECharts 靠它认出自己跑在 node 上、支持服务端渲染
  const ctx = vm.createContext({document, localStorage, window: {addEventListener() {}}, console, process,
                                setTimeout, clearTimeout, setInterval, clearInterval});
  vm.runInContext(fs.readFileSync(ECHARTS, 'utf8'), ctx);
  vm.runInContext(`
    const __realInit = echarts.init;
    const __proto = __realInit(null, null, {renderer: 'svg', ssr: true, width: 10, height: 10}).constructor.prototype;
    __proto.resize = function () {};                // 服务端渲染没有容器可量, 尺寸固定
    echarts.init = function () { return __realInit(null, null, {renderer: 'svg', ssr: true, width: 1400, height: 2300}); };
  `, ctx);
  vm.runInContext(script, ctx);
  return {G: e => vm.runInContext(e, ctx), els, evtEls, localStorage, script};
}

const fills = svg => { const out = {}; for (const m of svg.matchAll(/fill="(#[0-9a-f]{6})"/g)) out[m[1]] = (out[m[1]] || 0) + 1; return out; };

let n = 0, bad = 0;
function test(name, fn) {
  try { fn(); n++; console.log('ok   ' + name); }
  catch (e) { n++; bad++; console.log('FAIL ' + name + '\n     ' + String(e && e.stack || e).split('\n').slice(0, 5).join('\n     ')); }
}

for (const [label, file] of [['全历史', process.argv[2]], ['近两年规则', process.argv[3]]]) {
  const P = open(file);
  const G = P.G;
  const series = () => G('chart.getOption().series');
  const kData = () => G('chart.getOption().series[0].data');
  const kIsSafe = k => Array.isArray(k) && k.every(x => Array.isArray(x));     // 裸 null 会让真实 ECharts 抛错

  test(`${label} · 页面在真实 ECharts 下出图; 颜色占位符全部替换成了颜色值`, () => {
    assert(JSON.stringify(G('C')) === JSON.stringify(COLORS), '颜色常量 C = ' + JSON.stringify(G('C')));
    assert(!/__C_[A-Z]+_|__PAYLOAD__|__GENERATED__|__ECHARTS__/.test(P.script), '页面脚本里残留模板占位符');
    assert(G('D.key') === 'sh' && series().map(s => s.name).slice(0, 5).join() === SH_NAMES.join(),
           'series ' + series().map(s => s.name).slice(0, 5).join());
    assert(kIsSafe(kData()), '上证的 K 线');
  });

  function near(a, b) { return Math.abs(a - b) < 1e-6; }

  test(`${label} · 默认缩放窗口是最近 250 个交易日(滚轮与滑块两个组件都带起点; 只给滑块写的话真实 ECharts 以滚轮的 0 ~ 100% 为准)`, () => {
    const z = G('chart.getOption().dataZoom.map(z => [z.type, z.start, z.end])');
    const n = G('D.n'), dates = G('D.dates');
    assert(z.length === 2 && near(z[0][1], z[1][1]) && near(z[0][2], 100) && near(z[1][2], 100), JSON.stringify(z));
    assert(near(z[0][1], 100 - 250 / n * 100), JSON.stringify(z) + ' n=' + n);
    const win = G('zoomDates()');                                 // 图上实际可见的那一段
    assert(win[0] === dates[n - 250] && win[1] === dates[n - 1],
           JSON.stringify(win) + ' 应为 ' + dates[n - 250] + ' ~ ' + dates[n - 1]);
  });

  test(`${label} · 上证页签的 K 线有涨有跌两种颜色(占位符没替换时阴线会全红)`, () => {
    G('setZoom(60)');
    const f = fills(G('chart.renderToSVGString()'));
    assert(f[COLORS.red] > 0 && f[COLORS.aqua] > 0, 'fill 统计 ' + JSON.stringify(f));
  });

  test(`${label} · 四个页签逐个切换、叠加、副图与底部面板开关、事件等级、快捷缩放、悬停框 — 真实 ECharts 不抛错`, () => {
    for (const key of G('TAB_ORDER')) {
      G(`activate(${JSON.stringify(key)})`);
      assert(series().length >= 5, key + ' 页签没有序列');
      assert(kIsSafe(kData()), key + ' 页签的 K 线里有不是数组的项');
      for (const i of [0, Math.floor(G('D.n') / 2), G('D.n') - 1])
        assert(typeof G('chart.getOption().tooltip[0].formatter')([{dataIndex: i}]) === 'string', key + ' 悬停框');
      G(`render(${key === 'sh' ? 0 : 'D.first - 1'})`);
    }
    // 叠加: 上一个页签(最后一个)上是 3 个勾选框, 全勾上; 之后每个页签画的是"勾选的里面除自己以外"的那几条
    const checked = G(`(() => { document.querySelectorAll('#overlayBox input').forEach(c => { c.checked = true; c.onchange(); });
                                return OVERLAY.slice(); })()`);
    assert(checked.length === 3, '叠加勾选 ' + checked.length);
    for (const key of G('TAB_ORDER')) {
      G(`activate(${JSON.stringify(key)})`);
      const dashed = series().filter(s => s.type === 'line' && s.lineStyle && s.lineStyle.type === 'dashed');
      assert(dashed.length === checked.filter(k => k !== key).length,
             key + ' 页签上的叠加线 ' + dashed.length + ' 条');
    }
    for (const id of ['ckPE', 'ckPB', 'ckMargin', 'ckEtf', 'ckEtfFlow', 'ckMf', 'ckPEPct', 'ckPBPct',
                      'ckZb', 'ckJl', 'ckRsi', 'ckBias', 'ckVol', 'ckUd']) {
      const el = P.els[id];
      assert(el, '没有控件 ' + id);
      el.checked = true; el.fire('change');
      el.checked = false; el.fire('change');
    }
    for (const el of P.evtEls) { el.checked = true; el.fire('change'); }
    G('setZoom(250)'); G('setZoom(500)'); G('setZoom(0)');
    G('render(0)'); G('render(D.n - 1)');
    const svg = G('chart.renderToSVGString()');
    assert(svg.length > 10000 && !svg.includes('__C_'), 'svg ' + svg.length);
  });

  test(`${label} · 缩放窗口按日期保持(来回切几遍不漂移); 切页签后右侧"时间区事件"按新窗口重算`, () => {
    G("activate('sh')");
    const nSh = G('D.n');
    const dates = G('D.dates');
    const ev = G('Object.fromEntries(BASE.events.map(e => [e.title, e.date]))');
    const i0 = dates.indexOf(ev['MI3-EVENT-IN']) - 10;    // 窗口从 i0 起: 事件一在窗口外前一天, 事件二在窗口内
    assert(i0 > 0 && dates.indexOf(ev['MI3-EVENT-OUT']) === i0 - 1, '夹具里的两条事件 ' + JSON.stringify(ev));
    G(`chart.dispatchAction({type: 'dataZoom', start: ${i0} / D.n * 100, end: 100})`);
    const z0 = G('zoomDates()');
    assert(z0[0] === dates[i0], JSON.stringify(z0));
    // 切到日期轴最短的页签: 旧的百分比落在短轴上对应的是靠后的位置(差几十天), 只按旧百分比算出来的
    // "时间区事件"会漏掉窗口开头的事件 — 必须按换算后的新窗口重算
    const short = G('TAB_ORDER.slice(1).reduce((a, b) => (VIEWS[a].n <= VIEWS[b].n ? a : b))');
    assert(G(`VIEWS[${JSON.stringify(short)}].n`) < nSh - 30, '夹具里应有一个日期轴明显更短的页签');
    G(`activate(${JSON.stringify(short)})`);
    const html = G("document.getElementById('panel').innerHTML");
    assert(html.includes('MI3-EVENT-IN') && !html.includes('MI3-EVENT-OUT'),
           '切页签后"时间区事件"的窗口不对: ' + (html.match(/时间区事件[\s\S]{0,200}/) || ['(没有这张表)'])[0].replace(/\s+/g, ' '));
    assert(JSON.stringify(G('zoomDates()')) === JSON.stringify(z0), '窗口变成 ' + JSON.stringify(G('zoomDates()')));
    for (let round = 0; round < 5; round++) for (const key of G('TAB_ORDER')) G(`activate(${JSON.stringify(key)})`);
    assert(JSON.stringify(G('zoomDates()')) === JSON.stringify(z0), '五轮之后漂到 ' + JSON.stringify(G('zoomDates()')));
  });
}

console.log(bad ? `FAILED ${bad} of ${n}` : `ALL TESTS PASS passed ${n}`);
process.exit(bad ? 1 : 0);
