// 情绪图页脚本的行为测试: 把 viewer 的内联脚本放进 vm 沙箱执行, 用最小的 DOM / ECharts / localStorage 桩
// 驱动真实的页面函数(mount / activate / render / renderAux / setZoom)。
// 由 tests/test_mi3_viewer.py 调用; 断言失败退出码 1。
'use strict';
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const tpl = fs.readFileSync(process.argv[2], 'utf8');
const script = tpl.match(/<script>([\s\S]*?)<\/script>/g)
  .map(x => x.replace(/<\/?script>/g, '')).sort((a, b) => b.length - a.length)[0];
const KEYS = ['zt_count', 'dt_count', 'zb_rate', 'lb_max', 'jl_rate', 'vol_total', 'vr', 'rsi', 'chg',
              'gap', 'kr', 'bias20', 'bias60', 'close', 'amp'];
const STAGES = ['极度冰点', '冰点/恐慌', '偏冷/退潮', '中性偏弱', '中性', '中性偏强', '偏热/发酵', '狂热/高潮', '极度狂热'];
const D12 = ['2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15', '2026-10-16', '2026-10-19',
             '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-26', '2026-10-27'];
const W = Object.fromEntries(KEYS.map(k => [k, 1 / 15]));       // 15 个分量等权

// ── 夹具载荷: 上证(顶层) + 三个指数页签 ──
function mkSh() {                                    // 上证: 12 天, 全有分, -30 … -2.5
  const n = D12.length;
  const score = D12.map((_, i) => -30 + i * 2.5);
  return {
    dates: D12.slice(), n, k: D12.map((_, i) => [3000 + i, 3005 + i, 2990 + i, 3010 + i]),
    vol: D12.map(() => 9.99), pct: D12.map(() => 0.1), score, v1: score.map(s => s - 10),
    stage: score.map(s => (s <= -30 ? '偏冷/退潮' : s <= -10 ? '中性偏弱' : '中性')),
    zt: D12.map(() => 65), dt: D12.map(() => -4), zb: D12.map(() => 30.9), jl: D12.map(() => 45.1),
    raw: Object.fromEntries(KEYS.map((k, j) => [k, D12.map((_, i) => i * 100 + j)])),
    cs: Object.fromEntries(KEYS.map((k, j) => [k, D12.map((_, i) => -20 + j + (i % 5))])),
    labels: Object.fromEntries(KEYS.map(k => [k, '上证' + k])), weights: W,
    sub_starts: {zt: D12[0], dt: D12[0], zb: D12[0], jl: D12[0], score: D12[0]},
    val: {'000016.SH': {n: '上证50', pe: D12.map(() => 10.5), pb: D12.map(() => 1.2)}},
    margin: D12.map(() => 18000), etf: {'510050.SH': {n: '上证50ETF', amt: D12.map(() => 20)}},
    mval: {alla_pe: D12.map(() => 15), alla_pb: D12.map(() => 1.5),
           alla_pe_pct: D12.map(() => 0.46), alla_pb_pct: D12.map(() => 0.12)},
    events: [{date: '2026-10-09', level: 4, title: '事件一', type: '', source: '', content: ''},
             {date: '2026-10-13', level: 3, title: '事件二', type: '', source: '', content: ''},
             {date: '2026-10-15', level: 3, title: '事件三', type: '', source: '', content: ''},
             {date: '2026-10-16', level: 2, title: '事件四', type: '', source: '', content: ''}],
    etf_flow: [{date: '2026-10-13', net_yi: 5.5}, {date: '2026-10-20', net_yi: -3.25}],
    window: 250, stages: STAGES.slice(),
  };
}
const A_DATES = ['2026-10-08', '2026-10-09'].concat(D12);       // 深证成指: 日期轴比上证早 2 天
function aScore(i) { return (i < 4 || i === 10) ? null : -40 + i; }   // 第 4 天起有分; 第 10 天没出分
function mkA() {
  const dates = A_DATES, n = dates.length, d0 = 7;              // 悬停明细(d0 起)只带最近 7 天
  return {
    key: 'A', name: '深证成指', board: '深市', dates, n, d0,
    // years 窗口外的 K 线: 这里故意用裸 null(真实载荷里是四个空值组成的数组) — 页面对两种写法都要兜住
    k: dates.map((_, i) => (i < 2 ? null : [10000 + i, 10050 + i, 9990 + i, 10060 + i])),
    vol: dates.map((_, i) => (i < 2 ? null : 3381.06)), pct: dates.map((_, i) => (i < 2 ? null : -0.02)),
    score: dates.map((_, i) => aScore(i)), sc: dates.map((_, i) => (aScore(i) == null ? null : 2)),
    zt: dates.map(() => 45), dt: dates.map(() => -12), zb: dates.map(() => 52.63), jl: dates.map(() => 13.51),
    lim: dates.map((_, i) => (i % 3 ? null : [8, 1])),
    raw: Object.fromEntries(KEYS.map((k, j) => [k, dates.slice(d0).map((_, i) => i * 10 + j)])),
    cs: Object.fromEntries(KEYS.map((k, j) => [k, dates.slice(d0).map((_, i) => -9.5 + j + i)])),
    labels: Object.fromEntries(KEYS.map(k => [k, '深' + k])), weights: W,
    first: 4, notes: {10: '缺成交额数据'},
    aux: {val: {'399001.SZ': {n: '深证成指', pe: dates.map(() => 26.5), pb: dates.map(() => 2.05)}},
          margin: dates.map(() => 11000), etf: {'159903.SZ': {n: '深成指ETF', amt: dates.map(() => 1.04)}},
          etf_flow: dates.map((_, i) => (i % 4 === 1 ? 0.5 : null)), mf: dates.map((_, i) => (i % 5 === 0 ? -21.5 : null)),
          mval: {alla_pe: dates.map(() => 15), alla_pb: dates.map(() => 1.5), pe300: dates.map(() => 12.6),
                 alla_pe_pct: dates.map((_, i) => (i < 3 ? null : 0.46)), alla_pe_pct5: dates.map(() => 0.57),
                 alla_pb_pct: dates.map(() => 0.12), alla_pb_pct5: dates.map(() => 0.26),
                 pe300_pct: dates.map(() => 0.63), pe300_pct5: dates.map(() => 0.71)}},
  };
}
function mkB() {                                    // 创业板指(在深圳): 只有最近 5 天; 前两天在生成规则的 years 窗口外
  const dates = D12.slice(-5), n = dates.length;    // (那两天其实有分, 只是不带进这张图); 全A估值按偏移取
  return {
    key: 'B', name: '创业板指', board: '创业板', dates, n, d0: 0, w0: 2,
    k: dates.map((_, i) => (i < 2 ? [null, null, null, null] : [2180 + i, 2189.82, 2170 + i, 2195 + i])),
    vol: dates.map(() => 1432.37), pct: dates.map(() => 0.29),
    score: dates.map((_, i) => (i < 2 ? null : -30 - i)), sc: dates.map((_, i) => (i < 2 ? null : 2)),
    zt: dates.map(() => 12), dt: dates.map(() => -9), zb: dates.map(() => 73.33), jl: dates.map(() => 10),
    lim: dates.map(() => null),
    raw: Object.fromEntries(KEYS.map((k, j) => [k, dates.map((_, i) => i + j)])),
    cs: Object.fromEntries(KEYS.map((k, j) => [k, dates.map((_, i) => -j + i)])),
    labels: Object.fromEntries(KEYS.map(k => [k, '创' + k])), weights: W,
    first: 0, notes: {},
    aux: {val: {}, margin: [], etf: {}, etf_flow: [], mf: [], mval_off: A_DATES.length - n},
  };
}
function mkC() {                                    // 科创50(在上海): 一天分都没有
  const dates = D12.slice(-3), n = dates.length;
  return {
    key: 'C', name: '科创50', board: '科创板', dates, n, d0: 0,
    k: dates.map((_, i) => [1160 + i, 1169.99, 1150 + i, 1175 + i]), vol: dates.map(() => 733.77), pct: dates.map(() => -1.36),
    score: dates.map(() => null), sc: dates.map(() => null), zt: dates.map(() => 2), dt: dates.map(() => -3),
    zb: dates.map(() => 77.78), jl: dates.map(() => 0), lim: dates.map(() => null), raw: {}, cs: {}, labels: {}, weights: {},
    first: null, notes: {}, aux: {val: {}, margin: [], etf: {}, etf_flow: [], mf: [], mval_off: A_DATES.length - n},
  };
}

// ── 最小 DOM ──
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.style = {}; this.dataset = {}; this.className = '';
    this._html = ''; this.textContent = ''; this.value = ''; this.type = ''; this.checked = false;
    this.parent = null; this.onclick = null; this.onchange = null; this._l = {};
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  addEventListener(ev, fn) { (this._l[ev] = this._l[ev] || []).push(fn); }
  fire(ev) { (this._l[ev] || []).forEach(fn => fn({})); }
}
const STATIC = {}, dyn = {};                        // STATIC: 模板里的静态控件(各次运行共用); dyn: 脚本按 id 取用时现建的
const staticCtl = (into) => (m) => {                // 记下模板里的初始勾选状态(_checked0): 每次运行前复原, 用例之间不串状态
  const el = new El('input');
  el.type = /type="checkbox"/.test(m[0]) ? 'checkbox' : '';
  el.checked = el._checked0 = /\schecked\b/.test(m[0]);
  if (/value="(\d)"/.test(m[0])) el.value = /value="(\d)"/.exec(m[0])[1];
  into.push(el);
};
for (const m of tpl.matchAll(/<input[^>]*\sid="([^"]+)"[^>]*>/g)) {
  const box = [];
  staticCtl(box)(m);
  STATIC[m[1]] = box[0];
}
const evtEls = [];                                  // 事件等级勾选(页面按 class 查)
for (const m of tpl.matchAll(/<input[^>]*class="ckEvt"[^>]*>/g)) staticCtl(evtEls)(m);
const $id = id => STATIC[id] || dyn[id] || (dyn[id] = new El('div'));
const document = {
  title: '', body: new El('body'),
  getElementById: $id,
  createElement: tag => new El(tag),
  createTextNode: txt => { const e = new El('#text'); e.textContent = txt; return e; },
  querySelectorAll: sel => (sel === '.ckEvt' ? evtEls : []),
  addEventListener() {},
};

// ── ECharts 桩: setOption / getOption / dispatchAction / on ──
const chart = {
  opt: null, calls: 0, acts: [], _h: {},
  setOption(o, opts) {
    if (opts && opts.replaceMerge) this.opt = o;                       // 整页重排: 传进来的就是完整配置
    else if (this.opt && o.series) {                                    // 按 id 增量(事件标记)
      this.opt = Object.assign({}, this.opt);
      this.opt.series = this.opt.series.map(s => {
        const r = o.series.find(x => x.id && x.id === s.id);
        return r ? Object.assign({}, s, r) : s;
      });
    } else this.opt = Object.assign({}, this.opt, o);
    this.calls++;
  },
  getOption() { return this.opt; },
  resize() {},
  on(ev, fn) { (this._h[ev] = this._h[ev] || []).push(fn); },
  dispatchAction(a) {
    this.acts.push(a);
    if (a.type === 'dataZoom' && a.start != null && this.opt)
      for (const z of this.opt.dataZoom) { z.start = a.start; z.end = a.end; }
  },
  hover(i) { (this._h.updateAxisPointer || []).forEach(f => f({axesInfo: [{axisDim: 'x', value: i}]})); },
};
const echarts = {init: () => chart};
const getOption0 = chart.getOption;

function run(store, payload) {                       // 新开一个沙箱跑一遍页面脚本
  for (const k of Object.keys(dyn)) delete dyn[k];
  for (const el of Object.values(STATIC).concat(evtEls)) { el.checked = el._checked0; el._l = {}; }   // 勾选状态与监听器回到初始: 用例之间不串(静态控件在各次运行间共用, 页面脚本每次都会再绑一遍)
  chart.opt = null; chart.calls = 0; chart.acts.length = 0; chart._h = {};
  chart.getOption = getOption0;                        // 用例里可能临时换掉过(伪造缩放窗口)
  const localStorage = {_m: Object.assign({}, store || {}),
    getItem(k) { return k in this._m ? this._m[k] : null; },
    setItem(k, v) { this._m[k] = String(v); }};
  const ctx = vm.createContext({document, echarts, localStorage, window: {addEventListener() {}}, console});
  vm.runInContext(script.replace('__PAYLOAD__', JSON.stringify(payload)), ctx);
  return {ctx, localStorage, G: expr => vm.runInContext(expr, ctx)};
}
const PAYLOAD = Object.assign(mkSh(), {tabs: [mkA(), mkB(), mkC()]});
const win = R => {                                    // 当前缩放窗口覆盖的日期(与页面 zoomDates 同一个换算: 带容差)
  const z = chart.opt.dataZoom[1], n = R.G('D.n'), dates = R.G('D.dates');
  return [dates[Math.max(0, Math.floor(z.start / 100 * n + 1e-6))],
          dates[Math.min(n - 1, Math.ceil(z.end / 100 * n - 1e-6) - 1)]];
};
const tabBtn = key => $id('tabs').children.find(b => b.dataset.tab === key);       // 当前挂着的那一个(切页签后会重建)
const card = key => $id('cmp').children.find(b => b.dataset.tab === key);
const ovBox = () => $id('overlayBox').children.map(lb => lb.children[0]);
const baseSeries = () => chart.opt.series.slice(0, 5).map(s => s.name);
const panelTitles = () => chart.opt.title.slice(3).map(t => t.text);
const dashed = () => chart.opt.series.filter(s => s.lineStyle && s.lineStyle.type === 'dashed');
const has = (html, frag, what) => assert.ok(html.includes(frag), (what || '') + ' 缺: ' + frag);

let n = 0, bad = 0;
function test(name, fn) {
  try { fn(); n++; console.log('ok   ' + name); }
  catch (e) { n++; bad++; console.log('FAIL ' + name + '\n     ' + String(e && e.stack || e).split('\n').slice(0, 4).join('\n     ')); }
}

// ════════════ 上证页签: 与改动前相同 ════════════
// 本节前 2 条 + "没出分的日子仍是原文案" 只用新旧页面都有的东西(D / chart / 控件), 拿改动前的模板跑也应当通过 —
// 它们是"上证页签逐项一致"的守卫; 其余用例用到新页面才有的标识, 对改动前的模板应当失败。
test('默认打开是上证页签: 序列名 / 面板标题 / 日期轴与原来相同', () => {
  const R = run({}, PAYLOAD);
  assert.equal(R.G('D.key'), 'sh');
  assert.deepEqual(baseSeries(), ['上证指数', '两市成交量(亿手)', '情绪分', '涨停家数', '跌停家数']);
  assert.equal(chart.opt.title[0].text, '上证指数');
  assert.equal(chart.opt.title[1].text, '情绪分');
  assert.equal(chart.opt.title[2].text, '底部面板(可勾选)');
  assert.ok(chart.opt.xAxis.every(a => a.data === R.G('SH.dates')), '所有横轴用上证的日期');
  assert.equal(chart.opt.series[0].data, R.G('SH.k'));
  assert.equal(chart.opt.series[2].lineStyle.color, R.G('C.blue'));
  assert.equal(chart.opt.series[2].data, R.G('SH.score'));
  assert.equal(chart.opt.dataZoom[1].start, Math.max(0, 100 - 250 / 12 * 100));
  assert.equal(chart.opt.graphic[0].style.text, '涨停起 2026-10-12');
  assert.equal(chart.opt.graphic[1].style.text, '炸板率起 2026-10-12');
});

test('上证页签的头部 / 右侧面板: 分数、分档、日期、V1 对照, 文案与原来逐字相同', () => {
  const R = run({}, PAYLOAD);
  assert.equal($id('heroScore').textContent, '-2.5');
  assert.equal($id('heroStage').textContent, '中性');
  assert.equal($id('heroDate').textContent, '2026-10-27');
  assert.equal($id('heroV1').textContent, '-12.5');
  assert.notEqual($id('heroV1Wrap').style.display, 'none');
  const p = $id('panel').innerHTML;
  for (const frag of ['<h2>2026-10-27 当日构成</h2>', '前日差 <b>+2.5</b> · V1量价对照 <b>-12.5</b><br>',
                      '上证 收<b>3016</b> (+0.10%) · 两市量 <b>9.99</b> 亿手<br>',
                      '涨停 <b>65</b> 跌停 <b>4</b> · 炸板率 <b>30.90</b>% · 接力率 <b>45.10</b>%',
                      '<td>上证zt_count</td><td>1100</td>', '<td>7%</td>', '全A PE', '水位·全史',
                      '两融余额 <b>18000</b> 亿', '宽基ETF <b>20</b> 亿', '时间区事件'])
    has(p, frag);
  assert.equal(/本指数与板块/.test(p), false);
  assert.equal(p.includes('<p class="hint">'), false);            // 有分的日子不出提示
});

test('上证页签的悬停框文案与原来逐字相同(用真实的格式化函数)', () => {
  const R = run({}, PAYLOAD);
  const h = chart.opt.tooltip.formatter([{dataIndex: 11}, {seriesId: 'evIdx'}]);
  for (const frag of ['<b>2026-10-27</b>', '<br/>上证 收 <b>3016</b> (+0.1%) 开 3011 高 3021 低 3001',
                      '<br/>两市量 9.99 亿手', '情绪分 <b style="color:__C_BLUE__">-2.5</b> · 中性',
                      '<br/>涨停 <b>65</b> / 跌停 <b>4</b> · 炸板率 30.9% · 接力率 45.1%',
                      '<span style="color:#898781">V1对照 -12.5</span>', '环比+2.5',
                      'RSI -12 · 量能 -14 · 乖离 -8 · 涨停 -19 · 振幅 -5', 'PE 全A 15.0/1.50',
                      '· 两融 18000亿', '· 宽基ETF 20亿', '全A水位·全史/近5年 PE 46.0%'])
    has(h, frag, '悬停框');
});

test('上证页签的副图与底部面板: 面板名、控件文字不变; 多出来的只有页签 / 对比条 / 叠加框', () => {
  const R = run({}, PAYLOAD);
  assert.deepEqual(panelTitles(), ['宽基指数PE(50/300/500/1000)', '宽基指数PB', '两融余额(亿, 沪深北合计)',
                                    '宽基ETF成交额(亿·8只堆叠)', 'ETF净流入(亿·红进绿出·与其他图联动缩放)',
                                    '全A PE 分位%(全史·近5年)', '全A PB 分位%(全史·近5年)']);
  assert.equal(chart.opt.series.find(s => s.name === '炸板率%').data, R.G('D.zb'));
  assert.equal(chart.opt.series.find(s => s.name === '接力率%').data, R.G('D.jl'));
  assert.equal(chart.opt.series.find(s => s.name === '两融余额(亿)').name, '两融余额(亿)');
  assert.equal(chart.opt.series.find(s => s.name === 'ETF净流入(亿)').name, 'ETF净流入(亿)');
  assert.equal($id('lbZb').textContent, '炸板率');
  assert.equal($id('lbJl').textContent, '接力率');
  assert.equal($id('lbPE').textContent, '指数PE');
  assert.equal($id('lbEtf').textContent, '宽基ETF成交额');
  assert.equal($id('lbMargin').textContent, '两融余额');
  assert.equal($id('ckMfWrap').style.display, 'none');
  assert.equal($id('tabs').children.length, 4);
  assert.equal(tabBtn('sh').className, 'tab on');
  assert.equal(card('sh').className, 'cmp-card on');
  assert.equal(ovBox().length, 3);
  assert.ok(ovBox().every(c => !c.checked));
  assert.deepEqual(ovBox().map(c => c.parent.children[1].textContent), [' 深证成指', ' 创业板指', ' 科创50']);
  assert.equal(dashed().length, 0);
});

test('上证页签上的底部面板 / 副图 / 事件等级开关照旧生效', () => {
  const R = run({}, PAYLOAD);
  $id('ckRsi').checked = true;
  $id('ckRsi').fire('change');
  assert.deepEqual(chart.opt.series.find(s => s.name === 'RSI').data, R.G('D.cs.rsi'));
  $id('ckZb').checked = false;
  $id('ckZb').fire('change');
  assert.equal(chart.opt.series.find(s => s.name === '炸板率%'), undefined);
  $id('ckMargin').checked = false;
  $id('ckMargin').fire('change');
  assert.ok(!panelTitles().includes('两融余额(亿, 沪深北合计)'));
  const evDates = () => chart.opt.series.find(s => s.id === 'evIdx').data.map(d => d.value[0]);
  assert.deepEqual(evDates(), ['2026-10-12', '2026-10-13', '2026-10-15']);      // L2 默认不勾
  const l2 = evtEls.find(e => e.value === '2');
  l2.checked = true;
  l2.fire('change');
  assert.equal(R.G('EV_ON[2]'), true);
  assert.deepEqual(evDates(), ['2026-10-12', '2026-10-13', '2026-10-15', '2026-10-16']);
});

test('载荷里没有 tabs(老载荷 / 指数数据没入库): 页面照常出图, 只有上证一个页签', () => {
  const R = run({}, mkSh());
  assert.equal(R.G('D.key'), 'sh');
  assert.deepEqual(baseSeries(), ['上证指数', '两市成交量(亿手)', '情绪分', '涨停家数', '跌停家数']);
  assert.equal($id('tabs').children.length, 1);
  assert.equal($id('cmp').children.length, 1);
  assert.equal(ovBox().length, 0);
  assert.equal(dashed().length, 0);
  assert.equal($id('panel').innerHTML.includes('上证 收<b>3016</b>'), true);
});

// ════════════ 指数页签 ════════════
test('切到深证成指: 序列名 / 标题 / 日期轴 / 面板都换成该指数的', () => {
  const R = run({}, PAYLOAD);
  tabBtn('A').onclick();
  assert.equal(R.G('D.key'), 'A');
  assert.deepEqual(baseSeries(), ['深证成指', '指数成交额(亿)', '情绪分', '强势家数', '弱势家数']);
  assert.equal(chart.opt.title[0].text, '深证成指');
  assert.equal(chart.opt.title[1].text, '情绪分');
  assert.ok(chart.opt.xAxis.every(a => a.data === R.G('D.dates')));
  assert.equal(chart.opt.series[0].data.length, 14);
  assert.deepEqual(chart.opt.series[0].data[0], [null, null, null, null]);   // 与上证一致: 窗口外是四空数组(裸 null 会让真实 ECharts 抛错)
  assert.deepEqual(chart.opt.series[0].data[13], [10013, 10063, 10003, 10073]);
  assert.deepEqual(chart.opt.series[2].data.slice(0, 5), [null, null, null, null, -36]);   // 起步期留空
  assert.equal(chart.opt.series[2].data[10], null);                 // 没出分的日子留空
  assert.equal(chart.opt.series[2].lineStyle.color, R.G('TAB_COLOR.A'));
  assert.equal(chart.opt.graphic.at(-1).style.text, '情绪分起 2026-10-14');
  assert.equal(chart.opt.graphic[0].style.text, '强势起 2026-10-08');
  assert.deepEqual(panelTitles(), ['深证成指 PE', '深证成指 PB', '深市两融余额(亿)', '深成指ETF成交额(亿)',
                                    '深成指ETF净流入(亿)·红进绿出', '深市板块主力净流入(亿·板块内个股合计·红进绿出)',
                                    '全A PE 分位%(全史·近5年)', '全A PB 分位%(全史·近5年)']);
  assert.equal(chart.opt.series.find(s => s.name === '冲高回落率%').data, R.G('D.zb'));
  assert.equal(chart.opt.series.find(s => s.name === '强势家数').data[13], 45);
  assert.equal(chart.opt.series.find(s => s.name === '弱势家数').data[13], -12);
  const mf = chart.opt.series.find(s => s.name.startsWith('深市板块主力净流入'));
  assert.equal(mf.data.length, 14);
  assert.equal(mf.data[10].value[1], -21.5);
  assert.equal(mf.data[10].itemStyle.color, '#1baf7a');
  assert.equal(mf.data[11], null);
  assert.equal(chart.opt.series.find(s => s.name === '深成指ETF').data[13], 1.04);
});

test('指数页签上随页签变化的文字与显隐', () => {
  const R = run({}, PAYLOAD);
  tabBtn('A').onclick();
  assert.equal($id('lbZb').textContent, '冲高回落率');
  assert.equal($id('lbJl').textContent, '接力率');
  assert.equal($id('lbPE').textContent, '本指数PE');
  assert.equal($id('lbPB').textContent, '本指数PB');
  assert.equal($id('lbMargin').textContent, '深市两融余额');
  assert.equal($id('lbEtf').textContent, '深成指ETF成交额');
  assert.equal($id('lbEtfFlow').textContent, '挂钩ETF净流入');
  assert.equal($id('ckMfWrap').style.display, '');
  assert.equal($id('heroV1Wrap').style.display, 'none');
  assert.equal(R.G('document.title'), 'A股市场情绪 · 深证成指 · 交互行情图');
  assert.equal(tabBtn('sh').className, 'tab');
  assert.equal(tabBtn('A').className, 'tab on');
  assert.equal($id('tabs').children.length, 4);
  assert.equal(card('sh').className, 'cmp-card');
  assert.equal(card('A').className, 'cmp-card on');
  assert.deepEqual(ovBox().map(c => c.parent.children[1].textContent), [' 上证指数', ' 创业板指', ' 科创50']);
  assert.equal($id('heroScore').textContent, '-27.0');
  assert.equal($id('heroStage').textContent, '偏冷/退潮');          // 档位号 2 → 偏冷/退潮
  assert.equal($id('heroDate').textContent, '2026-10-27');
  assert.equal($id('heroV1').textContent, '--');
  tabBtn('sh').onclick();                                           // 切回上证: 文字与显隐复原
  assert.equal($id('lbZb').textContent, '炸板率');
  assert.equal($id('ckMfWrap').style.display, 'none');
  assert.notEqual($id('heroV1Wrap').style.display, 'none');
  assert.equal(R.G('document.title'), 'A股市场情绪 · 交互行情图');          // 与模板里的 <title> 相同
});

test('指数页签的悬停框: 指数的量价、板块结构(含封板口径)、本指数估值与板块资金, 没有 V1', () => {
  const R = run({}, PAYLOAD);
  tabBtn('A').onclick();
  const h = chart.opt.tooltip.formatter([{dataIndex: 13}]);
  for (const frag of ['<b>2026-10-27</b>', '<br/>深证成指 收 <b>10063</b> (-0.02%) 开 10013 高 10073 低 10003',
                      '<br/>成交额 3381.06 亿', '· 偏冷/退潮', '环比+1.0',
                      '<br/>强势 <b>45</b> / 弱势 <b>12</b> · 冲高回落率 52.63% · 接力率 13.51%',
                      'RSI +4 · 量能 +2 · 乖离 +8 · 强势 -3 · 振幅 +11', '深证成指 PE 26.5/PB 2.05',
                      '· 深市两融余额 11000亿', '· 深成指ETF 1.0亿', '全A水位·全史/近5年 PE 46.0%'])
    has(h, frag, '悬停框');
  assert.equal(/V1对照/.test(h), false);
  const h10 = chart.opt.tooltip.formatter([{dataIndex: 9}]);         // 封板口径只在有数的日子出现
  assert.equal(/\(封板涨停 8 \/ 跌停 1\)/.test(h10), true);          // 9 % 3 === 0
  assert.equal(/封板涨停/.test(h), false);                           // 13 % 3 === 1
  assert.equal(/主力净流入/.test(h), false);
  assert.equal(/深市板块主力净流入 -21\.5亿/.test(chart.opt.tooltip.formatter([{dataIndex: 10}])), true);
});

test('指数页签的右侧面板: 量价、板块结构、分量表(短标签 + 权重 + 贡献)、本指数与板块', () => {
  const R = run({}, PAYLOAD);
  tabBtn('A').onclick();
  const p = $id('panel').innerHTML;
  for (const frag of ['<h2>2026-10-27 当日构成</h2>', '深市板块里当日涨跌幅 ≥ +9.8%',
                      '前日差 <b>+1.0</b><br>', '深证成指 收<b>10063</b> (-0.02%) · 成交额 <b>3381.06</b> 亿<br>',
                      '强势 <b>45</b> 弱势 <b>12</b> · 冲高回落率 <b>52.63</b>% · 接力率 <b>13.51</b>%',
                      '<td>深zt_count</td><td>60</td>',                 // 明细只带 d0(第 7 天)起: 第 13 天 = 6×10+0
                      '<td>深jl_rate</td><td>64</td>', '<td>7%</td>', '本指数与板块',
                      '深证成指 PE', '26.5', '2.05', '深市两融余额(亿)', '11000', '深成指ETF成交额(亿)', '1.0',
                      '全A PE', '300 PE', '时间区事件'])
    has(p, frag);
  assert.equal(/V1量价对照/.test(p), false);
  assert.equal(p.includes('两融余额 <b>11000</b> 亿'), false);         // 上证的脚注不在指数页签上重复
  R.G('render(10)');                                               // 没出分但有主力净流入的日子
  const p10 = $id('panel').innerHTML;
  for (const frag of ['深市板块主力净流入(亿)', '-21.5', '该日没出分: 缺成交额数据。'])
    has(p10, frag);
  R.G('render(6)');                                                // 明细窗口之前的日子: 分量表留空
  const p6 = $id('panel').innerHTML;
  has(p6, '<td>深zt_count</td><td>--</td>');
  assert.equal(p6.includes('<td class="bar-cell"><div class="track"></div></td>'), true);   // 没有分量 → 分位条是空轨道
});

test('没出分的日子: 右侧面板写明原因 — 起步期 / 缺数据 / 一天都没出过', () => {
  const R = run({}, PAYLOAD);
  tabBtn('A').onclick();
  R.G('render(D.first - 1)');
  let p = $id('panel').innerHTML;
  has(p, '起步期: 量价分量要满一个分位窗口(250 个交易日)才有值, 本指数自 2026-10-14 起有情绪分。');
  has(p, '深证成指 收<b>10053</b>');                                // 行情照常显示
  assert.equal($id('heroScore').textContent, '--');
  assert.equal($id('heroStage').textContent, '--');
  tabBtn('C').onclick();
  R.G('render(2)');
  has($id('panel').innerHTML, '该指数还没有出过情绪分');
  assert.equal($id('heroScore').textContent, '--');
  assert.equal($id('heroStage').textContent, '--');
  has(card('C').innerHTML, '>--<');                                  // 对比条上没有分
  assert.equal(/<polyline/.test(card('C').innerHTML), false);
  has(card('C').innerHTML, '10-27 · --');
});

test('上证页签上没出分的日子仍是原文案', () => {
  const R = run({}, PAYLOAD);
  R.G('D.score[3] = null; render(3)');
  has($id('panel').innerHTML, '该日评分窗口数据不足(池数据2025-10起)。');
});

test('生成规则带 years 时: 窗口外那些有分的日子不会被说成"没出分"', () => {
  const R = run({}, PAYLOAD);
  tabBtn('B').onclick();
  R.G('render(1)');
  has($id('panel').innerHTML, '在生成规则的 years 窗口之外: 该日有情绪分, 只是没有带进这张图。');
  assert.equal(/该日没出分/.test($id('panel').innerHTML), false);
  R.G('render(2)');
  assert.equal(/窗口之外|该日没出分|起步期/.test($id('panel').innerHTML), false);
});

test('整页重排前先收掉悬停框: 悬停框还显示着时切页签 / 勾叠加 / 开关面板, ECharts 会拿旧的序列下标去刷新它(触摸 / 键盘操作下报错、旧悬停框留在新图上)', () => {
  const R = run({}, PAYLOAD);
  const tips = () => chart.acts.filter(a => a.type === 'hideTip').length;
  assert.ok(tips() >= 1);                                   // 初次出图
  const before = tips();
  tabBtn('A').onclick();
  assert.equal(tips(), before + 1);
  const ck = ovBox().find(c => c.dataset.overlay === 'sh');
  ck.checked = true;
  ck.onchange();
  assert.equal(tips(), before + 2);
  $id('ckRsi').checked = true;
  $id('ckRsi').fire('change');
  assert.equal(tips(), before + 3);
  const last = chart.acts.findLastIndex(a => a.type === 'hideTip');
  assert.ok(chart.acts.slice(last + 1).some(a => a.type === 'dataZoom'), '收悬停框之后才换图');
});

test('缩放窗口: 写进去的百分比读回来还是同一天(浮点误差不至于差一天), 各种日期轴长度都一样', () => {
  const R = run({}, PAYLOAD);
  const bad = R.G(`(() => {
    const saved = D, out = [];
    for (const n of [5561, 5318, 4005, 1673, 820, 14, 12, 5, 3]) {
      D = {n, dates: Array.from({length: n}, (_, i) => i)};
      for (const i0 of [0, 1, 2, 7, 97, 333, Math.floor(n / 2), n - 250, n - 2, n - 1]) {
        if (i0 < 0 || i0 >= n) continue;
        for (const i1 of [n - 1, Math.min(n - 1, i0 + 249), Math.min(n - 1, i0 + 1)]) {
          if (i1 < i0) continue;
          chart.getOption = () => ({dataZoom: [{start: i0 / n * 100, end: Math.min(100, (i1 + 1) / n * 100)}]});
          const z = zoomDates();
          if (z[0] !== i0 || z[1] !== i1) out.push([n, i0, i1, z]);
        }
      }
    }
    D = saved;
    return out;
  })()`);
  assert.deepEqual(bad, []);
});

test('切页签后右侧"时间区事件"按换算后的新窗口重算(不是按旧的百分比)', () => {
  const R = run({}, PAYLOAD);
  const l2 = evtEls.find(e => e.value === '2');
  l2.checked = true;
  l2.fire('change');
  chart.dispatchAction({type: 'dataZoom', start: 5 / 12 * 100, end: 100});   // 上证 10-19 起: 窗口里没有事件
  R.G('render(D.n - 1)');
  assert.equal(/时间区事件/.test($id('panel').innerHTML), false);
  tabBtn('A').onclick();                                     // 深证成指: 同一段日期是第 7 天起
  assert.equal(/时间区事件/.test($id('panel').innerHTML), false);           // 按旧百分比算是第 5 天起, 会把 10-15 / 10-16 两条算进来
  assert.deepEqual(win(R), ['2026-10-19', '2026-10-27']);
});

test('悬停联动: 轴指针落到哪天, 右侧面板与头部就是哪天', () => {
  const R = run({}, PAYLOAD);
  tabBtn('A').onclick();
  chart.hover(5);
  assert.equal($id('heroDate').textContent, '2026-10-15');
  has($id('panel').innerHTML, '<h2>2026-10-15 当日构成</h2>');
  chart.hover(5);
  chart.hover(9);
  assert.equal($id('heroDate').textContent, '2026-10-21');
  tabBtn('sh').onclick();
  assert.equal($id('heroDate').textContent, '2026-10-27');          // 切页签回到最新有分的一天
  chart.hover(2);
  assert.equal($id('heroDate').textContent, '2026-10-14');
});

// ════════════ 缩放窗口 ════════════
test('切换页签时缩放窗口按日期保持(各页签日期轴的长短不同)', () => {
  const R = run({}, PAYLOAD);
  chart.dispatchAction({type: 'dataZoom', start: 5 / 12 * 100, end: 10 / 12 * 100});   // 上证第 5 ~ 9 天: 10-19 ~ 10-23
  assert.deepEqual(win(R), ['2026-10-19', '2026-10-23']);
  tabBtn('A').onclick();
  assert.deepEqual(win(R), ['2026-10-19', '2026-10-23']);
  assert.ok(Math.abs(chart.opt.dataZoom[1].start - 7 / 14 * 100) < 1e-6);
  tabBtn('B').onclick();                                           // 创业板指 10-21 才有: 从它有数据的第一天起
  assert.deepEqual(win(R), ['2026-10-21', '2026-10-23']);
  tabBtn('sh').onclick();
  assert.deepEqual(win(R), ['2026-10-21', '2026-10-23']);           // 再切回来: 保持的是创业板指页签上的那段
});

test('窗口早于新页签的起点: 没有重叠 → 用该页签的默认窗口; 部分重叠 → 从第一天开始', () => {
  const R = run({}, PAYLOAD);
  chart.dispatchAction({type: 'dataZoom', start: 0, end: 4 / 12 * 100});              // 上证 10-12 ~ 10-15
  tabBtn('B').onclick();                                                          // 创业板指 10-22 才有
  assert.deepEqual(win(R), ['2026-10-21', '2026-10-27']);
  assert.equal(chart.opt.dataZoom[1].start, 0);                                   // 夹具只有 5 天, 默认窗口就是全部
  const R2 = run({}, PAYLOAD);
  chart.dispatchAction({type: 'dataZoom', start: 0, end: 8 / 12 * 100});            // 上证 10-12 ~ 10-21
  tabBtn('A').onclick();
  assert.deepEqual(win(R2), ['2026-10-12', '2026-10-21']);               // 两天都有 → 原样
  assert.ok(Math.abs(chart.opt.dataZoom[1].start - 2 / 14 * 100) < 1e-6);
  chart.dispatchAction({type: 'dataZoom', start: 0, end: 8 / 14 * 100});            // 深证成指 10-08 ~ 10-15
  tabBtn('sh').onclick();                                                          // 上证 10-12 才有: 从上证第一天起
  assert.deepEqual(win(R2), ['2026-10-12', '2026-10-19']);
  assert.equal(chart.opt.dataZoom[1].start, 0);
});

test('快捷缩放按当前页签的交易日数算', () => {
  const R = run({}, PAYLOAD);
  R.G('setZoom(4)');
  assert.ok(Math.abs(chart.opt.dataZoom[1].start - (100 - 4 / 12 * 100)) < 1e-9);
  R.G('setZoom(250)');
  assert.equal(chart.opt.dataZoom[1].start, 0);
  tabBtn('A').onclick();
  R.G('setZoom(2)');
  assert.ok(Math.abs(chart.opt.dataZoom[1].start - (100 - 2 / 14 * 100)) < 1e-9);
  R.G('setZoom(0)');
  assert.equal(chart.opt.dataZoom[1].start, 0);
  assert.equal(chart.opt.dataZoom[1].end, 100);
});

// ════════════ 情绪分叠加 ════════════
const aOnSh = D12.map((_, i) => aScore(i + 2));                      // 深证成指对齐到上证日期轴上的情绪分
test('叠加勾选: 在当前页签的日期轴上画其它指数的情绪分(虚线、各指数固定色), 取消即移除', () => {
  const R = run({}, PAYLOAD);
  const ck = ovBox().find(c => c.dataset.overlay === 'A');
  ck.checked = true;
  ck.onchange();
  const line = chart.opt.series.find(s => s.name === '深证成指情绪分');
  assert.ok(line, '叠加线');
  assert.equal(line.xAxisIndex, 2);
  assert.equal(line.yAxisIndex, 2);
  assert.equal(line.lineStyle.type, 'dashed');
  assert.equal(line.lineStyle.color, R.G('TAB_COLOR.A'));
  assert.deepEqual(line.data, aOnSh);                                 // 起步期与没出分的日子为空
  assert.deepEqual(JSON.parse(R.localStorage.getItem('sentOverlay_v1')), ['A']);
  has($id('overlayHint').textContent, '虚线为叠加的 1 个指数的情绪分');
  const cb = ovBox().find(c => c.dataset.overlay === 'B');
  cb.checked = true;
  cb.onchange();
  assert.equal(dashed().length, 2);
  const tip = chart.opt.tooltip.formatter([{dataIndex: 11}]);
  has(tip, '<span style="color:#e67e22">深证成指情绪分 -27</span>');
  has(tip, '<span style="color:#8e44ad">创业板指情绪分 -34</span>');
  ck.checked = false;
  ck.onchange();
  assert.equal(chart.opt.series.find(s => s.name === '深证成指情绪分'), undefined);
  has($id('overlayHint').textContent, '虚线为叠加的 1 个指数的情绪分');
  cb.checked = false;
  cb.onchange();
  assert.equal(dashed().length, 0);
  has($id('overlayHint').textContent, '勾选后在该指数的 K 线上对照其它指数的情绪分');
});

test('叠加不画当前页签自己; 勾选状态跨页签保留', () => {
  const R = run({}, PAYLOAD);
  for (const c of ovBox()) { c.checked = true; c.onchange(); }
  assert.equal(dashed().length, 3);
  tabBtn('A').onclick();
  assert.deepEqual(dashed().map(s => s.name), ['创业板指情绪分', '科创50情绪分']);
  assert.deepEqual(ovBox().map(c => c.dataset.overlay + ':' + c.checked), ['sh:false', 'B:true', 'C:true']);   // 上证在自己页签上没被勾过
  tabBtn('C').onclick();                                           // 科创50只有最近 3 天
  assert.deepEqual(chart.opt.series.find(s => s.name === '创业板指情绪分').data, [-32, -33, -34]);
  assert.deepEqual(chart.opt.series.find(s => s.name === '深证成指情绪分').data, [-29, -28, -27]);
  tabBtn('sh').onclick();
  assert.deepEqual(dashed().map(s => s.name), ['深证成指情绪分', '创业板指情绪分', '科创50情绪分']);
});

test('叠加的选择记在本浏览器, 重新打开时恢复; 已不在配置里的指数忽略', () => {
  const R1 = run({}, PAYLOAD);
  const ck = ovBox().find(c => c.dataset.overlay === 'A');
  ck.checked = true;
  ck.onchange();
  const R2 = run(R1.localStorage._m, PAYLOAD);
  assert.ok(chart.opt.series.find(s => s.name === '深证成指情绪分'));
  assert.equal(ovBox().find(c => c.dataset.overlay === 'A').checked, true);
  assert.equal(ovBox().filter(c => c.checked).length, 1);
  run({'sentOverlay_v1': '["A","zzz"]'}, PAYLOAD);
  assert.equal(dashed().length, 1);
  assert.equal(ovBox().filter(c => c.checked).length, 1);
});

// ════════════ 对比条 ════════════
test('对比条: 四个指数的最新分 / 分档 / 日期 / 较前一日 / 迷你走势, 点击切换页签', () => {
  const R = run({}, PAYLOAD);
  assert.equal($id('cmp').children.length, 4);
  for (const frag of ['上证指数<i>全市场</i>', '>-2.5<span class="chip"', '>中性</span>', '10-27 · 较前日 +2.5', '<polyline points="'])
    has(card('sh').innerHTML, frag);
  for (const frag of ['深证成指<i>深市</i>', '>-27.0', '>偏冷/退潮</span>', '10-27 · 较前日 +1.0'])
    has(card('A').innerHTML, frag);
  assert.equal(card('A').innerHTML.split('<polyline points="')[1].split('"')[0].split(' ').length, 9);   // 14 天里 9 天有分
  has(card('B').innerHTML, '创业板指<i>创业板</i>');
  has(card('B').innerHTML, '>-34.0');
  has(card('B').innerHTML, '较前日 -1.0');
  has(card('C').innerHTML, '科创50<i>科创板</i>');
  assert.ok(card('sh').title.includes('全市场口径') && card('A').title.includes('深市板块'));
  card('B').onclick();
  assert.equal(R.G('D.key'), 'B');
  assert.equal(card('B').className, 'cmp-card on');
  assert.equal($id('heroScore').textContent, '-34.0');
});

test('迷你走势的纵轴四个指数共用各自窗口里的最小 / 最大值(不压成平线), 0 线按同一比例画', () => {
  const R = run({}, PAYLOAD);
  const [ylo, yhi] = R.G('cmpScale()');
  const pad = (-2.5 + 36) * 0.12;                                    // 最小 -36(深证成指)、最大 -2.5(上证), 留 12% 边
  assert.ok(Math.abs(ylo - (-36 - pad)) < 1e-6 && Math.abs(yhi - (-2.5 + pad)) < 1e-6, [ylo, yhi].join());
  const html = card('sh').innerHTML;
  const ys = html.match(/<polyline points="([^"]+)"/)[1].split(' ').map(p => Number(p.split(',')[1]));
  assert.equal(ys.length, 12);
  assert.ok(Math.max(...ys) - Math.min(...ys) > 15, '不是一条平线');
  const zero = Number(/<line x1="0" x2="116" y1="([\d.]+)"/.exec(html)[1]);
  assert.ok(Math.abs(zero - (2 + yhi / (yhi - ylo) * 26)) < 0.06);
});

// ════════════ 页签记忆 / 口径文案 / 副图开关 / 事件 ════════════
test('页签选择记在本浏览器: 重新打开还是上次的页签; 记住的页签已不存在则回上证', () => {
  const R1 = run({}, PAYLOAD);
  tabBtn('A').onclick();
  assert.equal(R1.localStorage.getItem('sentTabs_v1'), 'A');
  const R2 = run(R1.localStorage._m, PAYLOAD);
  assert.equal(R2.G('D.key'), 'A');
  assert.equal($id('heroScore').textContent, '-27.0');
  assert.equal(tabBtn('A').className, 'tab on');
  assert.equal(R2.G('document.title'), 'A股市场情绪 · 深证成指 · 交互行情图');
  assert.equal(run({'sentTabs_v1': 'zzz'}, PAYLOAD).G('D.key'), 'sh');
  assert.equal(run({sentTabs_v1: 'C'}, PAYLOAD).G('D.key'), 'C');
  assert.equal(run({}, mkSh()).G('D.key'), 'sh');
});

test('两融的交易所随板块走: 科创板在上海 → 沪市两融; 创业板在深圳 → 深市两融; 没有挂钩ETF用通用名', () => {
  const R = run({}, PAYLOAD);
  tabBtn('C').onclick();
  assert.equal(R.G('D.t.margin'), '沪市两融余额');
  assert.equal($id('lbMargin').textContent, '沪市两融余额');
  assert.equal(R.G('D.t.scope'), '科创板板块');
  tabBtn('B').onclick();
  assert.equal(R.G('D.t.margin'), '深市两融余额');
  assert.equal(R.G('D.t.etfFlow'), 'ETF净流入(亿)');
  assert.equal($id('lbEtf').textContent, 'ETF成交额');
});

test('全A估值只带一份: 日期轴是后缀的页签按偏移截取', () => {
  const R = run({}, PAYLOAD);
  const A = R.G('VIEWS.A'), B = R.G('VIEWS.B');
  assert.equal(A.mval.alla_pe.length, 14);
  assert.equal(B.mval.alla_pe.length, 5);
  assert.deepEqual(B.mval.alla_pe_pct, [0.46, 0.46, 0.46, 0.46, 0.46]);
  assert.equal(R.G('D.key'), 'sh');
  R.G('activate("B")');
  assert.equal(R.G("auxDataEmpty({key: 'pePct'})"), false);
  assert.equal(R.G("auxDataEmpty({key: 'pbPct'})"), false);
  assert.equal(R.G("auxDataEmpty({key: 'mf'})"), true);             // B 没有主力净流入 → 面板不出
  assert.equal(R.G("auxDataEmpty({key: 'margin'})"), true);
});

test('没有数据的副图自动不出; 底部面板与副图开关在指数页签上同样生效(开关记忆各页签共用)', () => {
  const R = run({}, PAYLOAD);
  tabBtn('B').onclick();
  assert.deepEqual(panelTitles(), ['全A PE 分位%(全史·近5年)', '全A PB 分位%(全史·近5年)']);
  $id('ckRsi').checked = true;
  $id('ckRsi').fire('change');
  assert.deepEqual(chart.opt.series.find(s => s.name === 'RSI').data, R.G('D.cs.rsi'));
  $id('ckPE').checked = false;
  $id('ckPE').fire('change');
  tabBtn('A').onclick();
  assert.ok(!panelTitles().includes('深证成指 PE'));
  assert.ok(panelTitles().includes('深证成指 PB'));
  assert.equal(JSON.parse(R.localStorage.getItem('auxPanels_v1')).pe, false);
  $id('ckMf').checked = false;
  $id('ckMf').fire('change');
  assert.ok(!panelTitles().some(t => t.includes('主力净流入')));
});

test('事件标注按各页签自己的日期轴吸附; 没行情 / 没出分的日子不在对应图上标; 等级勾选对所有页签生效', () => {
  const R = run({}, PAYLOAD);
  assert.deepEqual(R.G('SH.evIdx.map(o => o.i)'), [0, 1, 3, 4]);     // 10-09 吸附到上证第一天; 10-13; 10-15; 10-16
  tabBtn('A').onclick();
  assert.deepEqual(R.G('D.evIdx.map(o => o.i)'), [1, 3, 5, 6]);      // 深证成指日期轴早 2 天
  assert.deepEqual(chart.opt.series.find(s => s.id === 'evIdx').data.map(d => d.value[0]), ['2026-10-13', '2026-10-15']);
  assert.deepEqual(chart.opt.series.find(s => s.id === 'evScore').data.map(d => d.value[0]), ['2026-10-15']);   // 10-13 还在起步期
  assert.ok($id('panel').innerHTML.includes('事件二') && $id('panel').innerHTML.includes('事件三'));
  const l2 = evtEls.find(e => e.value === '2');
  l2.checked = true;
  l2.fire('change');
  assert.equal(R.G('EV_ON[2]'), true);
  tabBtn('sh').onclick();
  assert.equal(R.G('D.evOn[2]'), true);                             // 各页签共用一份勾选状态
  assert.deepEqual(chart.opt.series.find(s => s.id === 'evIdx').data.map(d => d.value[0]),
                   ['2026-10-12', '2026-10-13', '2026-10-15', '2026-10-16']);
});

test('点当前页签不重绘; 点不存在的页签不出错', () => {
  const R = run({}, PAYLOAD);
  const before = chart.calls;
  R.G("activate('sh')");
  R.G("activate('zzz')");
  assert.equal(R.G('D.key'), 'sh');
  assert.equal(chart.calls, before);
});

console.log(bad ? `FAILED ${bad} of ${n}` : `ALL TESTS PASS passed ${n}`);
process.exit(bad ? 1 : 0);
