// 避雷针页脚本的行为测试: 把 stock.html 的内联脚本放进 vm 沙箱执行,
// 用最小的 DOM / ECharts / fetch 桩驱动真实的页面函数(load/renderXxx)。
// 由 tests/test_stock_d35.py 调用; 断言失败退出码1。
'use strict';
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/g)
  .map(x => x.replace(/<\/?script>/g, '')).sort((a, b) => b.length - a.length)[0];

// ── 最小DOM ──
function parseKids(src) {            // innerHTML里的 <a data-code> / <span class> / <option value>
  const out = [];
  const re = /<(\w+)([^>]*)>/g;
  let m;
  while ((m = re.exec(src))) {
    const el = {tag: m[1], dataset: {}, style: {}, value: '', textContent: '', innerHTML: '',
                onclick: null, onchange: null, classList: [], disabled: false};
    const cls = /\bclass="([^"]*)"/.exec(m[2]);
    if (cls) el.classList = cls[1].split(/\s+/);
    const idm = /\bid="([^"]*)"/.exec(m[2]);
    if (idm) el.id = idm[1];
    const val = /\bvalue="([^"]*)"/.exec(m[2]);
    if (val) el.value = val[1];
    let d;
    const dre = /\bdata-([\w-]+)="([^"]*)"/g;
    while ((d = dre.exec(m[2]))) el.dataset[d[1]] = d[2];
    el.text = src.slice(m.index + m[0].length, src.indexOf('</' + m[1] + '>', m.index));
    out.push(el);
  }
  return out;
}
class El {
  constructor(id) { this.id = id; this._html = ''; this.textContent = ''; this.style = {};
                    this.disabled = false; this.value = ''; this.dataset = {}; this._kids = [];
                    this.onclick = null; this.onchange = null; this.checked = false; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = v; this._kids = parseKids(v); }
  addEventListener() {}
  querySelectorAll() { return []; }
}
const els = {};
const $id = id => {
  for (const e of Object.values(els)) {              // innerHTML里带id的节点(如#poolToggle)
    const k = e._kids.find(x => x.id === id);
    if (k) return k;
  }
  return els[id] || (els[id] = new El(id));
};
const radios = [{value: 'single', checked: true, onchange: null},
                {value: 'cum', checked: false, onchange: null}];
const document = {
  querySelector(sel) {
    if (sel.includes('csrf-token')) return {content: 'CSRF'};
    if (sel.includes('pfMode')) return radios.find(r => r.checked);
    return null;
  },
  getElementById: $id,
  querySelectorAll(sel) {
    if (sel.includes('pfMode')) return radios;
    const mm = /^#(\w+)\s+\[([\w-]+)\]$/.exec(sel);   // "#poolChips [data-code]"
    if (mm) return $id(mm[1])._kids.filter(k => mm[2] in k.dataset);
    return [];
  },
  addEventListener() {}, hidden: false,
};
const charts = {};
const echarts = {init(el) {
  const c = {id: el.id, opt: null, n: 0,
             setOption(o) { this.opt = o; this.n++; },
             clear() { this.opt = null; }, on() {}, getOption() { return this.opt; }, resize() {}};
  charts[el.id] = c;
  return c;
}};

// ── fetch桩: 请求挂起, 由测试按URL取出并应答 ──
const log = [], pend = [];
function fetch(url, opts) {
  log.push(url);
  return new Promise(resolve => pend.push({url, resolve}));
}
function take(sub) {
  const i = pend.findIndex(p => p.url.includes(sub));
  return i < 0 ? null : pend.splice(i, 1)[0];
}
function reply(p, body, status) {
  p.resolve({ok: (status || 200) < 400, status: status || 200, json: async () => body});
}
const confirmed = [], alerts = [];
const sandbox = {
  document, echarts, fetch, window: {addEventListener() {}, isSecureContext: false},
  localStorage: {_m: {}, getItem(k) { return this._m[k] || null; }, setItem(k, v) { this._m[k] = v; }},
  confirm: () => { confirmed.push(1); return false; },
  alert: m => { alerts.push(m); },
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  console, JSON, Math, Date, Number, String, Array, Object, Promise, Error, encodeURIComponent,
  isFinite, Infinity, Set, Map, parseInt, parseFloat,
};
const ctx = vm.createContext(sandbox);
vm.runInContext(script, ctx);
const tick = () => new Promise(r => setImmediate(r));
const settle = async () => { for (let i = 0; i < 8; i++) await tick(); };
const G = expr => vm.runInContext(expr, ctx);
const click = id => {                                 // 浏览器里禁用态按钮点不动
  const el = $id(id);
  assert.ok(!el.disabled, id + ' 处于禁用态, 点击不会触发');
  el.onclick();
};

const DATES = ['2026-01-05', '2026-01-06', '2026-01-07', '2026-01-08', '2026-01-09'];
function mkK(code) {
  return {ok: true, code, name: 'N' + code, adj: 'qfq', n: 5, dates: DATES,
          k: DATES.map(() => [10, 11, 12, 9]), closes: DATES.map(() => 11),
          mas: {}, vol: DATES.map(() => 100), amount: DATES.map(() => 1.5),
          last: {date: DATES[4], close: 11}};
}
const EMPTY = {
  kline: u => mkK(decodeURIComponent(/[?&]code=([^&]*)/.exec(u)[1])),   // 只取code参数(不含&adj=)
  events: () => ({n: 1, events: [{date: '2026-09-30', risk: 'mid', type: 't', title: 'x'}]}),
  risk: () => ({items: []}),
  float: () => ({n: 1, soon: {n: 0}, data: [{date: '2026-09-30', share_yi: 1, ratio: 1, holder: 'h'}]}),
  freshness: () => ({datasets: {'日线': {fresh: true, last: DATES[4]}}}),
  finance_ai: () => ({grade: 'B', score: 0, rules: [], ai: null}),
  holders: () => ({periods: [], data: []}),
  margin: () => ({n: 1, latest: {date: '2026-09-30', rzye: 1, ratio: 1},
                  dates: ['2026-09-30'], rzye: [1], rqye: [1]}),
  profit: () => ({dates: ['2026-09-30'], cum: [1], single: [1], cum_yoy: [1], single_yoy: [1]}),
  val: () => ({dates: ['2026-09-30'], pe: [10], pb: [1], pe_p3: [null], pe_p5: [null],
               pb_p3: [null], pb_p5: [null]}),
  full: () => ({dates: ['2026-09-30'], pledge_ratio: null,
    profit: {revenue: [1], n_income: [1], dedt: [1], or_yoy: [1], net_yoy: [1],
             gpm: [1], npm: [1], roe: [1], dv_ttm: 1},
    cashflow: {ocf: [1], ar: [1], ap: [1], inv: [1], cash: [1], inv_turn: [1], ar_turn: [1]},
    balance: {assets: [1], liab: [1], dta: [1], goodwill: [null], gw_eq: [null], liab_pct: [1]},
    inst: {dates: ['2026-09-30'], ratio: [1], holder_num: [1]}}),
  pool: () => ({pool: []}),
};
function bodyFor(url) {
  const key = Object.keys(EMPTY).find(k => /api\/stock\/(\w+)\??/.exec(url)[1] === k);
  assert(key, '未知的接口: ' + url);
  return EMPTY[key](url);
}
const eq = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);   // 沙箱数组跨realm
async function loadStock(code) {                 // 加载一只股票并只应答K线(其余请求留挂起)
  G(`load(${JSON.stringify(code)})`);
  const k = take('/api/stock/kline?code=' + code);
  assert(k, '应发起K线请求 ' + code);
  reply(k, mkK(code));
  await settle();
}
async function drainPending() {                  // 清空挂起请求(各模块的空数据响应), 含应答后新派生的
  for (let round = 0; pend.length; round++) {
    assert.ok(round < 20, '请求派生不收敛');
    while (pend.length) { const p = pend.shift(); reply(p, bodyFor(p.url)); }
    await settle();
  }
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ═══ 1. 切股竞态 ═══
test('快速切股: 先发后到的旧股票响应被丢弃, 页面停在后选的股票', async () => {
  G("load('A')");
  G("load('B')");
  const kb = take('/api/stock/kline?code=B'); reply(kb, mkK('B')); await settle();
  const before = log.length;
  const ka = take('/api/stock/kline?code=A'); reply(ka, mkK('A')); await settle();
  assert.strictEqual(G('curCode'), 'B', 'curCode被旧响应改回A');
  assert.strictEqual(log.length, before, '旧股票A的响应又触发了子模块请求');
  assert.ok($id('stSummary').innerHTML.includes('NB'), 'K线摘要被旧股票覆盖');
  await drainPending();
});

test('切股后, 上一只股票尚未返回的模块数据不上图', async () => {
  await loadStock('B');                          // B的股东请求挂起中
  const n0 = charts.chartHnum.n;
  await loadStock('C');
  reply(take('/api/stock/holders?code=B'), {ok: true, periods: ['2026-09-30'], data: [
    {end_date: '2026-09-30', holder_num: 99999, holder_num_asof: null, holders: []}]});
  await settle();
  assert.strictEqual(charts.chartHnum.n, n0, '上一只股票的股东图被画了出来');
  assert.ok(!$id('hdState').innerHTML.includes('9.9999'), '上一只股票的户数状态被写入');
  await drainPending();
});

test('切换累计/单季后立刻切股: 旧股票的利润数据不渲染', async () => {
  await loadStock('A');
  radios[1].checked = true; radios[0].checked = false;
  radios[1].onchange();                          // 发起A的profit请求(挂起)
  await loadStock('B');                          // B的holders还挂着 → B自己的profit请求尚未发出
  const n0 = charts.chartProfit.n;
  const p = take('/api/stock/profit?code=A');    // A的慢响应在切股后才到
  assert(p, '挂起的应是A的profit请求');
  reply(p, {dates: ['2025-12-31'], cum: [42], single: [42], cum_yoy: [1], single_yoy: [1]});
  await settle();
  assert.strictEqual(charts.chartProfit.n, n0, '旧股票A的利润图被画到了B的页面上');
  radios[0].checked = true; radios[1].checked = false;
  await drainPending();
});

test('切股即清上一只的扫雷报告与风险横幅', async () => {
  $id('aiFinBody').innerHTML = '<b>上一只的评级D</b>';
  $id('riskBanner').style.display = '';
  G("load('Z')");
  assert.strictEqual($id('aiFinBody').innerHTML, '--');
  assert.strictEqual($id('riskBanner').style.display, 'none');
  await drainPending();
});

const PANEL_CHARTS = ['chartHnum', 'chartMargin', 'chartProfit', 'chartVal', 'chartFloat',
                      'chartG1', 'chartG1b', 'chartG2', 'chartG2b', 'chartG3', 'chartG3b', 'chartG4'];
async function loadFull(code, holdersNum) {      // 完整加载一只股票: 所有模块都渲染过
  await loadStock(code);
  reply(take('/api/stock/holders?code=' + code), {ok: true, periods: ['2026-09-30'], data: [
    {end_date: '2026-09-30', holder_num: holdersNum, holder_num_asof: null, holders: []}]});
  await drainPending();
  assert.ok(PANEL_CHARTS.every(id => charts[id].opt), code + ' 的各面板应已渲染');
}

test('切股: 股东表/事件表/徽标与下面各图一并清空, 不与上一只混排', async () => {
  await loadFull('A', 111111);
  assert.ok($id('hdState').innerHTML.includes('11.11万'));
  $id('evTable').innerHTML = '<table>A的事件</table>';
  $id('freshRow').innerHTML = '<span>A的徽标</span>';
  G("load('B')");                                // K线还没回来, 页面就该是新股票的空面板
  assert.strictEqual($id('hdTable').innerHTML, '');
  assert.strictEqual($id('hdState').textContent, '');
  assert.strictEqual($id('evTable').innerHTML, '', '上一只的事件表还留着');
  assert.strictEqual($id('freshRow').innerHTML, '', '上一只的数据集徽标还留着');
  assert.ok(PANEL_CHARTS.every(id => charts[id].opt === null),
            '上一只的图未清: ' + PANEL_CHARTS.filter(id => charts[id].opt).join(','));
  reply(take('/api/stock/kline?code=B'), mkK('B')); await settle();
  reply(take('/api/stock/holders?code=B'), {ok: true, periods: ['2026-09-30'], data: [
    {end_date: '2026-09-30', holder_num: 222222, holder_num_asof: null, holders: []}]});
  await drainPending();
  assert.ok($id('hdState').innerHTML.includes('22.22万'), '新股票的数据应正常渲染');
});

test('新股票加载失败: 不留"旧K线配空面板"的半只旧股票', async () => {
  await loadFull('A', 111111);
  G("load('X')");
  reply(take('/api/stock/kline?code=X'), {msg: '不存在的代码'}, 404); await settle();
  assert.strictEqual(G('curCode'), null, '当前股票仍是上一只');
  assert.strictEqual(charts.chartK.opt, null, '上一只的K线还留着');
  assert.strictEqual($id('stSummary').innerHTML, '');
  assert.ok($id('stState').textContent.includes('不存在的代码'), $id('stState').textContent);
  assert.strictEqual(take('/api/stock/events'), null, '失败后不应再拉各模块');
  const n0 = log.length;
  click('btnAiFin');                             // 无当前股票: 提示而不发请求
  click('btnSkUpd');
  assert.strictEqual(log.length, n0, '无当前股票时不应发起扫雷/更新请求');
  assert.deepStrictEqual(alerts.slice(-2), ['请先选择股票', '请先选择股票']);
  await drainPending();
});

test('同一只股票重载(切复权): 不清面板', async () => {
  await loadFull('B', 222222);
  G("load('B')");                                // 切复权/数据更新后就是这样重载
  assert.ok(PANEL_CHARTS.every(id => charts[id].opt), '重载同一只不应清空各图(闪烁)');
  assert.ok($id('hdTable').innerHTML.includes('table'), '重载同一只不应清空股东表');
  await drainPending();
});

test('同一只股票重载失败: 保留原内容, 状态行给出原因', async () => {
  await loadFull('B', 222222);
  G("load('B')");
  reply(take('/api/stock/kline?code=B'), {msg: '服务暂不可用'}, 500); await settle();
  assert.strictEqual(G('curCode'), 'B');
  assert.ok(charts.chartK.opt, '同一只重载失败不应清空K线');
  assert.ok($id('stState').textContent.includes('服务暂不可用'), $id('stState').textContent);
  await drainPending();
});

test('读扫雷缓存失败: 状态给出服务端原因, 报告区保持空', async () => {
  await loadStock('B');
  const p = take('/api/stock/finance_ai?code=B');
  reply(p, {msg: '尚无当前口径的扫雷结果 — 点[一键扫雷]生成'}, 404);
  await settle();
  assert.ok($id('aiFinState').textContent.includes('未扫雷'), $id('aiFinState').textContent);
  assert.ok($id('aiFinState').textContent.includes('一键扫雷'), '未带出服务端原因');
  assert.strictEqual($id('aiFinBody').innerHTML, '--');
  await drainPending();
});

// ═══ 2. 筹码结构图单位 ═══
test('筹码结构图: 股东户数按"万"绘制(轴标万户)', () => {
  G(`renderFull({dates: ['2026-06-30', '2026-09-30'],
    profit: {revenue: [1, 1], n_income: [1, 1], dedt: [1, 1], or_yoy: [1, 1], net_yoy: [1, 1],
             gpm: [50, 50], npm: [10, 10], roe: [5, 5], dv_ttm: 2},
    cashflow: {ocf: [1, 1], ar: [1, 1], ap: [1, 1], inv: [1, 1], cash: [1, 1],
               inv_turn: [1, 1], ar_turn: [1, 1]},
    balance: {assets: [1, 1], liab: [1, 1], dta: [20, 20], goodwill: [null, null],
              gw_eq: [null, null], liab_pct: [20, 20]},
    inst: {dates: ['2026-06-30', '2026-09-30'], ratio: [5.1, 5.76],
           holder_num: [296404, 278364]}, pledge_ratio: null})`);
  const s = charts.chartG4.opt.series;
  assert.strictEqual(s[1].name, '股东户数(万)');
  eq(s[1].data, [29.64, 27.84], '画的是原始户数(大了1万倍)');
  eq(s[0].data, [5.1, 5.76]);
});

// ═══ 3. 股东表户数 ═══
test('股东表: 未披露户数的期间标"沿用", 不算环比(无假预警); 有披露才算', () => {
  G(`renderHolders({periods: ['2025-12-31', '2025-11-19', '2025-11-05', '2025-09-30'],
    data: [
      {end_date: '2025-12-31', holder_num: 255892, holder_num_asof: null, holders: []},
      {end_date: '2025-11-19', holder_num: null, holder_num_asof: 238512, holders: []},
      {end_date: '2025-11-05', holder_num: null, holder_num_asof: 238512, holders: []},
      {end_date: '2025-09-30', holder_num: 238512, holder_num_asof: null, holders: []}]})`);
  $id('hdPeriod').value = '2025-11-19'; $id('hdPeriod').onchange();
  let t = $id('hdState').innerHTML;
  assert.ok(t.includes('23.85万') && t.includes('沿用最近一期'), t);
  assert.ok(!t.includes('环比') && !t.includes('筹码分散'), '沿用值被拿去算环比: ' + t);
  $id('hdPeriod').value = '2025-12-31'; $id('hdPeriod').onchange();
  t = $id('hdState').innerHTML;
  assert.ok(t.includes('+7.3%') && t.includes('筹码分散'), '有披露期间应算环比: ' + t);
  assert.strictEqual(charts.chartHnum.opt.series[0].data.length, 2, '趋势图只画有披露的期间');
});

// ═══ 4. K线事件标记 ═══
test('K线: 晚于最新K线的未来事件不堆到最后一根K线上', () => {
  G(`curEvents = [
    {date: '2028-06-28', risk: 'high', type: '解禁', title: '未来解禁5%'},
    {date: '2026-01-07', risk: 'high', type: '业绩预告', title: '首亏'},
    {date: '2026-01-08', risk: 'low', type: '分红', title: '分红'}]`);
  G(`renderK(${JSON.stringify(mkK('B'))})`);
  const ev = charts.chartK.opt.series.find(s => s.name === '风险事件');
  eq(ev.data.map(x => x.value[0]), [2], '未来事件/低风险事件不应上图');
  assert.strictEqual($id('evCount').textContent, 1);
});

test('K线反复渲染不累积resize监听', () => {
  let n = 0;
  sandbox.window.addEventListener = () => { n++; };
  G(`renderK(${JSON.stringify(mkK('B'))})`);
  G(`renderK(${JSON.stringify(mkK('B'))})`);
  G(`renderK(${JSON.stringify(mkK('B'))})`);
  assert.strictEqual(n, 0, '每次渲染新增一个resize监听');
});

// ═══ 5. 自选池chips ═══
test('自选池超过12只: 可展开全部', async () => {
  await loadStock('B');
  const p = take('/api/stock/pool');
  reply(p, {pool: Array.from({length: 15}, (_, i) => ({code: 'C' + i, name: '股' + i}))});
  await settle();
  const chips = () => $id('poolChips')._kids.filter(k => 'code' in k.dataset);
  assert.strictEqual(chips().length, 12, '默认只列前12');
  assert.strictEqual($id('poolN').textContent, 15);
  const tog = $id('poolChips')._kids.find(k => k.id === 'poolToggle');
  assert(tog && tog.text.includes('展开全部'), '缺展开入口(其余股票点不到)');
  $id('poolToggle').onclick();
  assert.strictEqual(chips().length, 15);
  assert.ok($id('poolToggle').text.includes('收起'), '展开后应可收起');
  $id('poolToggle').onclick();
  assert.strictEqual(chips().length, 12);
  await drainPending();
});

// ═══ 6. 扫雷的AI定性没跑成 ═══
test('扫雷的AI定性未成功: 规则评级照常显示, 状态行提示可重试', async () => {
  await loadStock('D');
  const p = take('/api/stock/finance_ai?code=D');
  reply(p, {ok: true, cached: true, degraded: true, grade: 'C', score: -10,
            msg: '上次AI定性分析未成功(规则评级有效) — 点[一键扫雷]重试',
            rules: [{level: '高', rule: '扣非净利同比转负(5.00%→-30.00%)', score: -10}],
            ai: {summary: 'AI定性分析失败(仅规则引擎结果): ollama down', points: [], advice: ''}});
  await settle();
  const st = $id('aiFinState').textContent;
  assert.ok(st.includes('规则评级有效') && st.includes('AI定性未成功') && st.includes('一键扫雷'),
            '状态行: ' + st);
  assert.ok($id('aiFinBody').innerHTML.includes('>C<'), '规则评级应照常显示');
  await drainPending();
});

// ═══ 7. 解禁图的数量文案 ═══
test('解禁图: 小额解禁显示"10.64万股", 不再是"0.0亿股"', () => {
  G(`renderFloat({n: 2, soon: {n: 1, total_yi: 0.01, total_txt: '10.64万股', max_ratio: 0.0038},
    data: [{date: '2027-04-26', share_yi: 0.01064, share_txt: '10.64万股', ratio: 0.0038,
            holder: '张绍卓', future: true, big: false},
           {date: '2028-06-28', share_yi: 1.01, share_txt: '1.01亿股', ratio: 5.49,
            holder: '袁永刚', future: true, big: true}]})`);
  assert.ok($id('flState').textContent.includes('共10.64万股'), $id('flState').textContent);
  const tip = charts.chartFloat.opt.tooltip.formatter([{dataIndex: 0}]);
  assert.ok(tip.includes('解禁10.64万股') && !tip.includes('0.0亿股'), tip);
  assert.ok(charts.chartFloat.opt.tooltip.formatter([{dataIndex: 1}]).includes('解禁1.01亿股'));
  // 兼容没有 share_txt 的旧接口: 退回亿股数值
  G(`renderFloat({n: 1, soon: {n: 0}, data: [{date: '2027-04-26', share_yi: 1.5, ratio: 1,
    holder: 'x', future: true, big: false}]})`);
  assert.ok(charts.chartFloat.opt.tooltip.formatter([{dataIndex: 0}]).includes('解禁1.5亿股'));
});

// ═══ 8. "刚更新免确认"按股票记 ═══
test('数据更新免确认只对更新过的那只股票生效', async () => {
  await loadStock('A');
  click('btnSkUpd');                             // 触发后才发起update_all
  const u = take('/api/stock/update_all');
  assert(u && u.url.includes('/api/stock/update_all'), '应发起数据更新请求');
  reply(u, {datasets: {}}); await settle();
  await drainPending();                          // 更新后的重新加载
  // A刚更新: 点扫雷不弹确认, 直接发起扫雷请求
  click('btnAiFin');
  const fa = take('/api/stock/freshness?code=A');
  reply(fa, {datasets: {'日线': {fresh: false}}});   // 仍不新鲜
  await settle();
  assert.strictEqual(confirmed.length, 0, '刚更新过的股票不应再弹确认');
  const scan = take('/api/stock/scan');
  assert(scan, '应直接发起扫雷');
  reply(scan, {grade: 'B', score: 0, rules: [], ai: null}); await settle();
  assert.ok(!$id('btnAiFin').disabled, '扫雷结束后按钮应恢复可用');
  // 切到B(未更新): 同样不新鲜 → 要弹确认; 用户取消 → 不发起扫雷
  await loadFull('B', 222222);
  confirmed.length = 0;
  click('btnAiFin');
  const fb = take('/api/stock/freshness?code=B');
  reply(fb, {datasets: {'日线': {fresh: false}}}); await settle();
  assert.strictEqual(confirmed.length, 1, '别的股票不应借用A的免确认');
  assert.strictEqual(take('/api/stock/scan'), null, '用户取消后不应发起扫雷');
  await drainPending();
});

test('数据更新期间切股: 新股票的界面不被刷新, 但被更新那只仍记"刚更新"', async () => {
  await loadStock('A');
  click('btnSkUpd');
  const u = take('/api/stock/update_all');
  await loadStock('B');                          // 更新还没返回就切到B
  const n0 = log.length;
  reply(u, {datasets: {'日线': {fresh: true}}}); await settle();
  assert.strictEqual(log.length, n0, '更新A的结果触发了对B界面的刷新');
  assert.ok(!$id('freshRow').innerHTML.includes('日线'), 'A的更新结果覆盖了B的徽标行');
  assert.strictEqual(G('curCode'), 'B');
  // 切回A: 3分钟内扫雷免确认
  await drainPending();
  await loadFull('A', 111111);
  confirmed.length = 0;
  click('btnAiFin');
  const fa = take('/api/stock/freshness?code=A');
  reply(fa, {datasets: {'日线': {fresh: false}}}); await settle();
  assert.strictEqual(confirmed.length, 0, 'A确实更新过, 切回来不应再弹确认');
  const scan = take('/api/stock/scan');
  assert(scan, '应直接发起扫雷');
  reply(scan, {grade: 'B', score: 0, rules: [], ai: null}); await settle();
  await drainPending();
});

// ═══ 12b. 更新成功后的确认 ═══
// (沙箱与localStorage是各用例共用的: 前面的用例给A记过"刚更新"、忽略过数据集 — 先复位)
const resetScanState = () => {
  G('Object.keys(skUpdated).forEach(k => delete skUpdated[k])');
  G('skMuted.clear()');
};

test('数据更新成功: 页面重载扫雷结果后, 状态行仍标着"数据已更新"(切到别的股票不带)', async () => {
  resetScanState();
  await loadStock('A');
  await drainPending();
  assert.strictEqual($id('aiFinState').textContent, '● 上次扫雷结果');
  click('btnSkUpd');
  reply(take('/api/stock/update_all'), {datasets: {'日线': {fresh: true, last: DATES[4]}}});
  await settle();
  const k = take('/api/stock/kline?code=A');
  assert.ok(k, '更新后应重新加载该股票');
  reply(k, mkK('A'));
  await settle();
  await drainPending();                         // 含扫雷结果的读回 — 会顶掉"● 数据已更新"
  assert.strictEqual($id('aiFinState').textContent, '● 数据已更新 · 上次扫雷结果');
  assert.ok($id('freshRow').innerHTML.includes('日线✓'), $id('freshRow').innerHTML);
  await loadFull('B', 222222);                   // 没更新过的股票: 不带这句
  assert.strictEqual($id('aiFinState').textContent, '● 上次扫雷结果');
});

test('数据更新成功但该股还没扫过雷: "数据已更新"与未扫雷的说明并存', async () => {
  resetScanState();
  await loadStock('A');
  await drainPending();
  click('btnSkUpd');
  reply(take('/api/stock/update_all'), {datasets: {}});
  await settle();
  reply(take('/api/stock/kline?code=A'), mkK('A'));
  await settle();
  const fa = take('/api/stock/finance_ai?code=A');
  assert.ok(fa, '重载时应读回扫雷结果');
  reply(fa, {msg: '尚无当前口径的扫雷结果'}, 404);
  await drainPending();
  assert.strictEqual($id('aiFinState').textContent, '● 数据已更新 · (未扫雷) 尚无当前口径的扫雷结果');
});

// ═══ 13. 更新被服务端拦下 ═══
test('数据更新被安全校验拦下(400): 显示服务端的说明, 不报"数据已更新", 也不记"刚更新"', async () => {
  resetScanState();
  await loadStock('A');
  await drainPending();                         // 加载时的各模块请求(含新鲜度)都应答完
  click('btnSkUpd');
  const u = take('/api/stock/update_all');
  const msg = '登录状态已变化, 请刷新页面后重试';
  reply(u, {ok: false, csrf: true, msg}, 400); await settle();
  assert.strictEqual($id('aiFinState').textContent, '✗ ' + msg);   // 原先服务端回HTML, 这里只有 "✗ HTTP 400"
  assert.ok(!$id('btnSkUpd').disabled, '失败后按钮应恢复可用');
  // 更新没成功: A不算"刚更新", 点扫雷照常先查新鲜度
  confirmed.length = 0;
  click('btnAiFin');
  const fa = take('/api/stock/freshness?code=A');
  assert(fa, '更新失败的股票不应跳过新鲜度检查');
  reply(fa, {datasets: {'日线': {fresh: false}}}); await settle();
  assert.strictEqual(confirmed.length, 1, '更新失败的股票不应免确认');
  await drainPending();
});

test('数据更新遇到登录失效(401): 显示说明, 不把登录页当成空结果报"数据已更新"', async () => {
  resetScanState();
  await loadStock('A');
  await drainPending();
  click('btnSkUpd');
  reply(take('/api/stock/update_all'), {ok: false, msg: '未登录或登录已失效, 请刷新页面重新登录'}, 401);
  await settle();
  assert.strictEqual($id('aiFinState').textContent, '✗ 未登录或登录已失效, 请刷新页面重新登录');
  assert.strictEqual(G('skUpdated')['A'], undefined, '失败的更新不应记为"刚更新"');
  assert.strictEqual(pend.length, 0, '更新失败后不应再发任何请求: ' + pend.map(p => p.url).join(', '));
});

(async () => {
  let fail = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log('  ok   ' + name); }
    catch (e) { fail++; console.log('  FAIL ' + name + '\n       ' + (e && e.message)); }
  }
  console.log(fail ? `FAILED ${fail}/${tests.length}` : `passed ${tests.length}`);
  process.exit(fail ? 1 : 0);
})();
