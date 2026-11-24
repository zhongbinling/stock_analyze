// 历史回测页脚本的行为测试: 把 backtest.html 的内联脚本放进 vm 沙箱执行,
// 用迷你 DOM / ECharts / fetch 桩驱动真实的页面函数(compute/renderAll/loadStrategies…)。
// 每个用例各起一个全新沙箱。由 tests/test_behavior_review.py 调用; 断言失败退出码1。
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');
const {makeDom} = require('./page_dom.js');

const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/g)
  .map(x => x.replace(/<\/?script>/g, '')).sort((a, b) => b.length - a.length)[0];

const FACTORS = [
  {key: 'sentiment', name: '情绪热度', default_w: -30, desc: 'd1'},
  {key: 'pe_pct', name: '全A PE分位', default_w: -25, desc: 'd2'},
  {key: 'buffett', name: '巴菲特指标分位', default_w: -15, desc: 'd3'},
  {key: 'margin', name: '两融余额分位', default_w: 15, desc: 'd4'},
  {key: 'fflow', name: '主力资金流', default_w: 25, desc: 'd5'},
  {key: 'breadth', name: '板块温度宽度', default_w: 10, desc: 'd6'},
  {key: 'bb_ratio', name: '牛熊比(牛-熊)', default_w: -10, desc: 'd7'},
  {key: 'nt_flow', name: '国家队ETF净申购', default_w: 15, desc: 'd8'}];
const DATES = ['2026-11-20', '2026-11-21', '2026-11-24'];
// 行为指数的归一叠加从更早的日期起算(本图首日=115); 国家队图的叠加线应从该图自己的首日归一(=100)
function computeBody(last, o) {
  return Object.assign({
    ok: true, dates: DATES, composite: [1, 2, last],
    regime: {dates: DATES, closes: [3000, 3010, 3020], bull: [60, 55, 40], range: [10, 15, 25],
             bear: [30, 30, 35], bb: [30, 25, 5]},
    nt: {dates: DATES, ma5: [50, 80, null], cum: [2500, 2580, 2580], data_date: '2026-11-21'},
    nt_overlay: {dates: DATES, norm: [100, 101, 102]},
    overlay_index: {dates: DATES, norm: [115, 116, 117]},
    backtest: {dates: DATES, equity: [1, 1.01, 1.02], bench_equity: [1, 1, 1.01],
               pos: [0, 1, 1], bench_name: '510300ETF',
               metrics: {total_ret: 2, bench_ret: 1, cagr: 2.8, bench_cagr: 3.1, max_dd: -24.9,
                         win_rate: 85.7, n_trades: 7, sharpe: 0.27, n_days: 3}},
    factors_now: Object.fromEntries(FACTORS.map(f => [f.key, {
      name: f.name, weight: f.default_w, pct: 1.5, latest: '2026-11-24'}])),
    factor_joins: [{date: '2026-11-21', key: 'margin', name: '两融余额分位'}],
    entry: 30, exit: -30,
  }, o || {});
}
const STRATEGY = {id: 1, name: '稳健情绪', config: {factors: {sentiment: -40}, entry: 50, exit: -50},
                  updated_at: '2026-11-23 01:46'};

const tick = () => new Promise(r => setImmediate(r));

// 可选: node backtest_page_behavior.js <html> <echarts.min.js> [payload.json]
//   第2个参数给出时, 追加一个用真实 ECharts(SSR) 渲染的场景; 数据用 payload.json
//   (生产引擎的完整返回), 没给则用下面的小样本。桩ECharts只记录选项对象, 图表库
//   是否真的接受这些选项(标线写法、标签位置等)只有真库才能验证。
const REAL_LIB = process.argv[3] ? require(path.resolve(process.argv[3])) : null;
const REAL_PAYLOAD = REAL_LIB && process.argv[4]
  ? JSON.parse(fs.readFileSync(process.argv[4], 'utf8')) : null;

function mk(o) {                               // 全新沙箱: 页面脚本从初始化开始跑
  const dom = makeDom();
  // 写死在HTML里的初始值: 三个"叠加上证"默认勾选, 阈值输入框 30 / -30
  ['ovReg', 'ovNt', 'ovComp'].forEach(id => { dom.$id(id).checked = true; });
  dom.$id('btEntry').value = '30';
  dom.$id('btExit').value = '-30';
  const log = [], pend = [], confirmed = [], alerts = [];
  const st = {confirm: false};
  function fetch(url, opts) {
    log.push({url, body: opts && opts.body ? JSON.parse(opts.body) : null});
    return new Promise((resolve, _rej) => pend.push({url, body: log[log.length - 1].body, resolve}));
  }
  const realCharts = {};
  const echarts = (o && o.real) ? {init(el) {                // 真实 ECharts, 服务端渲染
    const c = o.real.init(null, null, {renderer: 'svg', ssr: true, width: 1200, height: 300});
    realCharts[el.id] = c;
    return c;
  }} : dom.echarts;
  const ctx = vm.createContext({
    document: dom.document, echarts, fetch, console, JSON, Math, Date, Number,
    String, Array, Object, Promise, Error, isNaN, setInterval: () => 0, setTimeout: () => 0,
    clearTimeout: () => {}, window: {addEventListener() {}},
    confirm: m => { confirmed.push(m); return st.confirm; }, alert: m => { alerts.push(m); },
  });
  vm.runInContext(script, ctx);
  const E = {
    dom, charts: dom.charts, realCharts, document: dom.document, log, pend, confirmed, alerts, st,
    $: dom.$id,
    take: sub => {
      const i = pend.findIndex(p => p.url.includes(sub));
      return i < 0 ? null : pend.splice(i, 1)[0];
    },
    reply: (p, body, status) => {
      assert(p, '没有挂起的请求可应答');
      p.resolve({ok: (status || 200) < 400, status: status || 200, json: async () => body});
    },
    settle: async () => { for (let i = 0; i < 10; i++) await tick(); },
    G: expr => vm.runInContext(expr, ctx),
    click: id => { assert.ok(!dom.$id(id).disabled, id + ' 处于禁用态'); dom.$id(id).onclick(); },
    nReq: () => log.filter(l => l.url.includes('/api/backtest/')).length,
  };
  E.box = k => dom.document.querySelectorAll(`#facTable [data-fk="${k}"]`)[0];
  E.wgt = k => dom.document.querySelectorAll(`#facTable [data-fw="${k}"]`)[0];
  E.dirOf = k => dom.document.querySelectorAll(`#facTable [data-fdir="${k}"]`)[0];
  E.series = (chart, name) => dom.charts[chart].opt.series.find(s => s.name === name);
  return E;
}

async function boot(o) {                      // 首次加载: 取因子表 → 首算(pull=true) → 策略列表
  const E = mk(o);
  E.reply(E.take('/api/backtest/factors'), {factors: FACTORS, defaults: {entry: 30, exit: -30}});
  await E.settle();
  const c = E.take('/api/backtest/compute');
  assert(c, '应发起首算');
  E.reply(c, (o && o.payload) || computeBody(3));
  await E.settle();
  const ls = E.take('/api/backtest/strategies');
  if (ls) E.reply(ls, {strategies: (o && o.strategies) || [STRATEGY]});
  await E.settle();
  return E;
}
const eq = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);   // 沙箱数组跨realm

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ═══ 1. 首次加载 ═══
test('首次加载: 默认权重 + 份额按需拉取, 三态与各图渲染, 数据截至', async () => {
  const E = await boot(), {$} = E;
  const first = E.log.find(l => l.url.includes('/compute'));
  assert.strictEqual(first.body.pull, true, '首算应带 pull');
  assert.strictEqual(first.body.factors.sentiment, -30);
  assert.strictEqual(Object.keys(first.body.factors).length, 8, '默认8个因子全部勾选');
  assert.ok($('regNow').innerHTML.includes('牛 40%') && $('regNow').innerHTML.includes('震荡 25%'),
            $('regNow').innerHTML);
  assert.ok($('biNow').innerHTML.includes('>+3<'), $('biNow').innerHTML);
  assert.ok($('btMsg').textContent.includes('共3日 (2026-11-20 ~ 2026-11-24)'), $('btMsg').textContent);
  assert.ok($('dataThrough').innerHTML.includes('数据截至 <b>2026-11-24</b>'));
  assert.strictEqual(E.series('chartRegime', '牛%').data.length, 3);
  assert.strictEqual(E.series('chartBt', '510300ETF买入持有').data.length, 3);
  assert.ok($('btMetrics').innerHTML.includes('交易 7次'), $('btMetrics').innerHTML);
  assert.ok($('ntNow').innerHTML.includes('1个交易日的份额待源端发布'), $('ntNow').innerHTML);
  assert.ok($('ntNow').innerHTML.includes('+80') && $('ntNow').innerHTML.includes('2580'), $('ntNow').innerHTML);
});

// ═══ 2. 并发计算 ═══
test('连续两次计算: 先发后到的旧结果不覆盖后发的新结果', async () => {
  const E = await boot();
  const before = E.nReq();
  E.click('btnCompute');                                 // 请求A(旧参数)
  const a = E.take('/api/backtest/compute');
  E.wgt('sentiment').value = '60';                       // 期间改了权重
  E.click('btnCompute');                                 // 请求B(新参数)
  const b = E.take('/api/backtest/compute');
  assert(a && b && a.body.factors.sentiment === -30 && b.body.factors.sentiment === -60);
  E.reply(b, computeBody(22)); await E.settle();         // B先返回
  E.reply(a, computeBody(11)); await E.settle();         // A后返回: 已过期
  assert.ok(E.$('biNow').innerHTML.includes('>+22<'),
            '旧请求的结果覆盖了新结果: ' + E.$('biNow').innerHTML);
  assert.strictEqual(E.series('chartComp', '行为指数').data[2], 22);
  assert.strictEqual(E.nReq(), before + 2);
});

// ═══ 3. 阈值校验 ═══
test('买入阈值不大于卖出阈值: 不发请求, 给出原因', async () => {
  const E = await boot(), {$} = E;
  const before = E.nReq();
  $('btEntry').value = '-30'; $('btExit').value = '30';
  E.click('btnCompute');
  assert.ok($('btMsg').textContent.includes('买入阈值须大于卖出阈值'), $('btMsg').textContent);
  $('btEntry').value = '0'; $('btExit').value = '0';
  E.click('btnCompute');
  assert.ok($('btMsg').textContent.includes('买入阈值须大于卖出阈值'), $('btMsg').textContent);
  $('btEntry').value = '';                               // 空/非数字
  E.click('btnCompute');
  assert.ok($('btMsg').textContent.includes('阈值'), $('btMsg').textContent);
  assert.strictEqual(E.nReq(), before, '参数非法时不应发起计算');
});

// ═══ 4. 权重 ═══
test('勾选的因子权重全为0或不勾任何因子: 不发请求, 给出原因', async () => {
  const E = await boot(), {$} = E;
  const before = E.nReq();
  E.document.querySelectorAll('#facTable [data-fk]').forEach(cb => { cb.checked = false; });
  E.click('btnCompute');
  assert.ok($('btMsg').textContent.includes('至少勾选一个因子'), $('btMsg').textContent);
  E.box('sentiment').checked = true; E.wgt('sentiment').value = '0';
  E.click('btnCompute');
  assert.ok($('btMsg').textContent.includes('权重大于0'), $('btMsg').textContent);
  assert.strictEqual(E.nReq(), before, '没有有效因子时不应发起计算');
});

// ═══ 5. 保存策略 ═══
test('用已有的策略名保存: 先确认是否覆盖; 取消则不发请求', async () => {
  const E = await boot(), {$} = E;
  const before = E.nReq();
  $('stName').value = '稳健情绪';
  E.click('btnSave');
  assert.strictEqual(E.confirmed.length, 1, '同名策略被静默覆盖, 没有确认');
  assert.strictEqual(E.nReq(), before, '用户取消后不应保存');
  E.st.confirm = true;
  E.click('btnSave');
  const p = E.take('/api/backtest/strategies');
  assert(p && p.body.name === '稳健情绪', '确认后应保存');
  E.reply(p, {ok: true, msg: '策略[稳健情绪]已更新', id: 1});
  await E.settle();
  const ls = E.take('/api/backtest/strategies');
  if (ls) E.reply(ls, {strategies: [STRATEGY]});
  await E.settle();
  $('stName').value = '   ';
  E.click('btnSave');
  assert.ok($('btMsg').textContent.includes('请先填策略名'));
  // 全新名字: 不弹确认
  E.confirmed.length = 0;
  $('stName').value = '新策略';
  E.click('btnSave');
  assert.strictEqual(E.confirmed.length, 0, '新名字不应弹覆盖确认');
  E.reply(E.take('/api/backtest/strategies'), {ok: true, msg: 'ok', id: 2});
  await E.settle();
  const ls2 = E.take('/api/backtest/strategies');
  if (ls2) E.reply(ls2, {strategies: []});
  await E.settle();
});

// ═══ 6. 策略列表 ═══
test('策略列表: 能直接看到每个策略的因子与阈值', async () => {
  const E = await boot();
  const h = E.$('stList').innerHTML;
  assert.ok(h.includes('情绪热度 40'), '列表里看不到策略的因子构成: ' + h);
  assert.ok(h.includes('入50/出-50'), '列表里看不到阈值: ' + h);
});

test('加载策略: 还原因子(方向+权重)与阈值后重算(不触发份额拉取)', async () => {
  const E = await boot(), {$} = E;
  E.document.querySelectorAll('#stList [data-load]')[0].onclick();
  assert.strictEqual(E.box('sentiment').checked, true);
  assert.strictEqual(E.dirOf('sentiment').value, '-1');
  assert.strictEqual(E.wgt('sentiment').value, '40');
  assert.strictEqual(E.box('pe_pct').checked, false, '策略未保存的因子不应被勾选');
  assert.strictEqual($('btEntry').value, 50);
  const p = E.take('/api/backtest/compute');
  assert(p && !p.body.pull, '加载策略只读库计算');
  eq(Object.keys(p.body.factors), ['sentiment']);
  E.reply(p, computeBody(5)); await E.settle();
});

// ═══ 7. 国家队图 ═══
test('国家队图的上证叠加线从该图自己的首个交易日起归一(=100)', async () => {
  const E = await boot();
  const s = E.series('chartNt', '上证(归一)');
  assert.strictEqual(s.data[0][1], 100,
                    '叠加线起点不是100(按行为指数的首日归一): ' + s.data[0][1]);
  assert.strictEqual(E.series('chartComp', '上证(归一)').data[0], 115, '行为指数图的叠加不受影响');
});

// ═══ 8. 因子加入竖线 ═══
test('行为指数图: 标出各因子开始有数据的日期', async () => {
  const E = await boot();
  const ml = E.series('chartComp', '行为指数').markLine.data;
  const join = ml.filter(x => x.xAxis);
  assert.strictEqual(join.length, 1);
  assert.strictEqual(join[0].xAxis, '2026-11-21');
  assert.ok(JSON.stringify(join[0].label).includes('两融余额分位'), '竖线应标出是哪个因子');
  assert.strictEqual(ml.filter(x => x.yAxis != null).length, 2, '买卖阈值横线保留');
});

// ═══ 8b. 回测区间内的因子构成说明 ═══
test('回测卡注明区间内参与的因子数与中途加入的因子, 取自计算结果而非面板当前勾选', async () => {
  const E = await boot(), {$} = E;
  assert.ok($('btBench').textContent.includes('起点参与因子 7 个'), $('btBench').textContent);
  assert.ok($('btBench').textContent.includes('2026-11 起加入两融余额分位'), $('btBench').textContent);
  E.box('pe_pct').checked = false;                      // 改了勾选但还没点计算
  $('ovComp').onchange();                               // 切叠加只重绘, 不重算
  assert.ok($('btBench').textContent.includes('起点参与因子 7 个'),
            '说明应跟随上次计算结果: ' + $('btBench').textContent);
  E.click('btnCompute');
  E.reply(E.take('/api/backtest/compute'), computeBody(3, {factor_joins: []}));
  await E.settle();
  // 这次计算结果里8个因子全程参与(面板里取消勾选的那次改动并没有被提交) → 跟结果走
  assert.ok($('btBench').textContent.includes('全程 8 个因子'), $('btBench').textContent);
});

// ═══ 9. 刷新与容错 ═══
test('回到页签超过5分钟: 自动带 pull 重算; 点[刷新数据]同理', async () => {
  const E = await boot();
  E.G('lastComputeAt = 1');
  E.document.fire('visibilitychange');
  let p = E.take('/api/backtest/compute');
  assert(p && p.body.pull === true, '回到页签应带 pull 重算');
  E.reply(p, computeBody(3)); await E.settle();
  E.click('btnRefresh');
  p = E.take('/api/backtest/compute');
  assert(p && p.body.pull === true);
  E.reply(p, computeBody(3)); await E.settle();
});

test('接口返回空数据/出错: 给出提示, 页面不留 undefined', async () => {
  const E = await boot(), {$} = E;
  E.click('btnCompute');
  E.reply(E.take('/api/backtest/compute'), computeBody(3, {dates: [], composite: []}));
  await E.settle();
  assert.ok($('btMsg').textContent.includes('✗'), $('btMsg').textContent);
  assert.ok(!$('biNow').innerHTML.includes('undefined'), $('biNow').innerHTML);
  E.click('btnCompute');
  E.reply(E.take('/api/backtest/compute'), {msg: '份额源繁忙'}, 500);
  await E.settle();
  assert.ok($('btMsg').textContent.includes('份额源繁忙'), $('btMsg').textContent);
});

// ═══ 9b. 独立评审指出的情形 ═══
test('参数校验失败后, 之前在途的那次计算回来不得冲掉错误提示', async () => {
  const E = await boot(), {$} = E;
  const before = E.nReq();
  E.click('btnCompute');                                 // 请求A在途
  const a = E.take('/api/backtest/compute');
  $('btEntry').value = '-30'; $('btExit').value = '30';  // 期间把阈值填反, 再点
  E.click('btnCompute');
  assert.ok($('btMsg').textContent.includes('买入阈值须大于卖出阈值'));
  E.reply(a, computeBody(11)); await E.settle();         // A这会儿才回来
  assert.ok($('btMsg').textContent.includes('买入阈值须大于卖出阈值'),
            '错误提示被在途请求的返回冲掉了: ' + $('btMsg').textContent);
  assert.ok($('biNow').innerHTML.includes('>+3<'), '面板是无效参数, 不该拿别的参数的结果重绘');
  assert.strictEqual(E.nReq(), before + 1);
});

test('策略列表没加载出来时: 同名保存由服务端拦下(409), 确认后才覆盖', async () => {
  const E = mk(), {$} = E;
  E.reply(E.take('/api/backtest/factors'), {factors: FACTORS, defaults: {entry: 30, exit: -30}});
  await E.settle();
  E.reply(E.take('/api/backtest/compute'), computeBody(3));
  await E.settle();
  E.reply(E.take('/api/backtest/strategies'), {msg: '数据库忙'}, 500);   // 列表加载失败
  await E.settle();
  assert.ok($('stList').textContent.includes('数据库忙'));
  $('stName').value = '稳健情绪';                        // 其实已有同名策略, 页面不知道
  E.click('btnSave');
  const first = E.take('/api/backtest/strategies');
  assert(first && first.body.overwrite === false, '第一次保存不应带 overwrite');
  E.reply(first, {ok: false, exists: true, msg: '策略名[稳健情绪]已存在'}, 409);
  await E.settle();
  assert.strictEqual(E.confirmed.length, 1, '服务端报告重名后应让用户确认');
  assert.strictEqual(E.nReq(), 4, '用户取消后不应再发请求');       // factors+compute+strategies+首次保存
  E.st.confirm = true;
  E.click('btnSave');                                   // 再点: 列表仍不知道有同名 → 服务端再拦 → 确认 → 覆盖
  const second = E.take('/api/backtest/strategies');
  assert(second && second.body.overwrite === false);
  E.reply(second, {ok: false, exists: true, msg: '策略名[稳健情绪]已存在'}, 409);
  await E.settle();
  const third = E.take('/api/backtest/strategies');
  assert(third && third.body.overwrite === true && third.body.name === '稳健情绪',
         '确认后应带 overwrite 重发');
  E.reply(third, {ok: true, msg: '策略[稳健情绪]已更新', id: 1});
  await E.settle();
  assert.ok($('btMsg').textContent.includes('已更新'));
});

test('最新读数缺因子时标为暂定值, 并给出最近一个完整读数', async () => {
  const E = mk(), {$} = E;
  E.reply(E.take('/api/backtest/factors'), {factors: FACTORS, defaults: {entry: 30, exit: -30}});
  await E.settle();
  const pv = {missing: ['两融余额分位', '国家队ETF净申购'], n_factors: 8,
              complete_date: '2026-11-21', complete_value: 22.4};
  E.reply(E.take('/api/backtest/compute'), computeBody(9, {provisional: pv}));
  await E.settle();
  const h = $('biNow').innerHTML;
  assert.ok(h.includes('最近完整读数(11-21): <b style="color:#e34448">+22.4</b>'), h);
  assert.ok(h.includes('11-24 暂定值 <b style="color:#e34448">+9</b>'), h);
  assert.ok(h.includes('两融余额分位、国家队ETF净申购') && h.includes('按 6/8 个因子归一'), h);
  assert.ok(!h.includes('当前:'), '不完整的读数不应写作"当前"');
  const ml = E.series('chartComp', '行为指数').markLine.data;
  const prov = ml.filter(x => x.xAxis === '2026-11-24');
  assert.strictEqual(prov.length, 1, '图上应标出暂定区间的起点');
  assert.ok(JSON.stringify(prov[0].label).includes('暂定'));
});

test('权重框里敲负数不翻转方向; 没有指数数据时状态卡给出提示', async () => {
  const E = await boot(), {$} = E;
  E.document.querySelectorAll('#facTable [data-fk]').forEach(cb => { cb.checked = false; });
  E.box('sentiment').checked = true;
  E.dirOf('sentiment').value = '1';                      // 顺势
  E.wgt('sentiment').value = '-20';
  E.click('btnCompute');
  const p = E.take('/api/backtest/compute');
  eq(p.body.factors, {sentiment: 20}, '负的幅度把方向翻转了');
  E.reply(p, computeBody(3, {regime: {dates: [], closes: [], bull: [], range: [], bear: [], bb: []}}));
  await E.settle();
  assert.strictEqual($('regNow').innerHTML, '库内没有上证指数数据');
});

// ═══ 10. 真实图表库(可选) ═══
if (REAL_LIB) {
  test('真实 ECharts: 引擎返回的数据能被图表库接受并渲染出四张图', async () => {
    const body = REAL_PAYLOAD || computeBody(3);
    const E = await boot({real: REAL_LIB, payload: body});
    assert.deepStrictEqual(Object.keys(E.realCharts).sort(),
                           ['chartBt', 'chartComp', 'chartNt', 'chartRegime']);
    let total = 0;
    for (const [id, chart] of Object.entries(E.realCharts)) {
      const svg = chart.renderToSVGString();          // 选项不被接受时会在这里抛错或画出空图
      total += svg.length;
      assert.ok(svg.length > 1500, id + ' 渲染内容异常短: ' + svg.length);
    }
    assert.ok(total > 12000, '四张图合计渲染内容过短: ' + total);
    const ml = E.realCharts.chartComp.getOption().series[0].markLine.data;
    const nProv = body.provisional && body.provisional.complete_date ? 1 : 0;
    assert.strictEqual(ml.length, 2 + ((body.factor_joins) || []).length + nProv,
                      '标线数量(2条阈值线+因子加入线+暂定值起点线)');
    assert.strictEqual(ml.filter(x => x.xAxis).length,
                      ((body.factor_joins) || []).length + nProv);
    if (process.env.BT_SVG_DIR) {                   // 调试用: 把渲染结果落盘供人工查看
      for (const [id, chart] of Object.entries(E.realCharts))
        fs.writeFileSync(path.join(process.env.BT_SVG_DIR, id + '.svg'), chart.renderToSVGString());
    }
  });
}

(async () => {
  let fail = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log('  ok   ' + name); }
    catch (e) { fail++; console.log('  FAIL ' + name + '\n       ' + (e && e.message)); }
  }
  console.log(fail ? `FAILED ${fail}/${tests.length}` : `passed ${tests.length}`);
  process.exit(fail ? 1 : 0);
})();
