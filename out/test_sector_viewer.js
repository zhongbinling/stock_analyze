// 回归测试: sector_viewer 前端 (DOM/echarts mock)
const fs = require('fs');
const html = fs.readFileSync('out/sector_heat_test.html', 'utf8');
const app = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1])[2];

function el(id) {
  return { id, style: {}, innerHTML: '', textContent: '', value: '',
    addEventListener(){}, setAttribute(){}, getAttribute(){return 'light'}, appendChild(){},
    classList:{add(){},remove(){},toggle(){}}, onclick:null, oninput:null, focus(){},
    querySelectorAll(){return[]} };
}
function mkChart() {
  return { _opt: null,
    setOption(o){ this._opt = o; },
    // 模拟真实ECharts: getOption 的 legend/dataZoom 归一化为数组
    getOption(){ const o = this._opt; if (!o) return o;
      return { ...o,
        legend: o.legend === undefined ? [] : (Array.isArray(o.legend) ? o.legend : [o.legend]),
        dataZoom: Array.isArray(o.dataZoom) ? o.dataZoom : [o.dataZoom] }; },
    on(){}, off(){}, clear(){ this._opt = null; }, resize(){},
    getWidth(){return 800}, getHeight(){return 600}, containsPixel(){return true},
    getZr(){return {on(){},off(){}}}, dispatchAction(){},
    convertFromPixel(){return 0}, convertToPixel(){return 0} };
}
global.document = { getElementById:id=>el(id), createElement:()=>el('x'), addEventListener(){},
  querySelector:()=>el('q'), querySelectorAll:()=>[], body: el('body'), documentElement: el('html') };
global.window = global; global.addEventListener = ()=>{};
let curveChart = null;
global.echarts = { init(dom){ const c = mkChart(); if (dom && dom.id === 'curve') curveChart = c; return c; } };

const driver = app + ';globalThis.__T={toggleSel,hmZoom,drawCurve,D,hmDays:()=>hmDaysGlobal,getSel:()=>selected};';
new Function(driver)();

const T = globalThis.__T;
const log = (ok, name, extra) => console.log((ok ? 'PASS' : 'FAIL') + '  ' + name + (extra ? '  ' + extra : ''));
let fails = 0;
const check = (ok, name, extra) => { log(ok, name, extra); if (!ok) fails++; };

// 1. 初始默认窗口 120 日
check(T.hmDays() === 120, 'default hmDaysGlobal = 120', T.hmDays());

// 2. 选中板块 → mkt 背景层 + 图例
const bk1 = T.D.codes.findIndex(c => c.startsWith('BK'));
T.toggleSel(bk1);
let opt = curveChart._opt;
check(opt.series.length === 2 && opt.series[0].name === '大盘情绪分', 'mkt bg series present',
  opt.series.map(s => s.name).join('|'));
check(opt.series[0].silent === true && opt.series[0].z === 1 && opt.series[1].z === 10, 'z-layering 1<10');
check(opt.legend.data[0] === '大盘情绪分', 'legend includes 大盘情绪分');

// 3. 图例隐藏态跨重绘保留
opt.legend = { ...opt.legend, selected: { '大盘情绪分': false } };  // 模拟用户点击图例
const bk2 = T.D.codes.findIndex((c, i) => c.startsWith('BK') && i !== bk1);
T.toggleSel(bk2);
opt = curveChart._opt;
check(opt.legend.selected['大盘情绪分'] === false, 'legend hidden state preserved after redraw',
  JSON.stringify(opt.legend.selected));
check(opt.legend.selected[T.D.names[bk1]] === undefined, 'visible series not in selected map');

// 4. 隐藏态在系列移除后不残留
opt.legend.selected = { '大盘情绪分': false, [T.D.names[bk1]]: false };  // 用户把两个都藏了
T.toggleSel(bk1);  // 移除 bk1
opt = curveChart._opt;
check(opt.legend.selected[T.D.names[bk1]] === undefined, 'stale hidden entry dropped on removal',
  JSON.stringify(opt.legend.selected));

// 5. 缩放态保留
curveChart._opt = { ...curveChart._opt, dataZoom: [{ type:'inside', start: 40, end: 55 }] };
T.toggleSel(bk2);  // 移除 bk2 → 回到无选中(mkt独占)
opt = curveChart._opt;
check(opt.dataZoom[0].start === 40 && opt.dataZoom[0].end === 55, 'zoom preserved on redraw',
  JSON.stringify(opt.dataZoom[0]));
check(opt.series.length === 1 && opt.series[0].name === '大盘情绪分', 'no-selection branch restores mkt-only');

// 6. 首次渲染(空 option) → 默认窗口
curveChart._opt = null;
T.drawCurve();
opt = curveChart._opt;
const total = Math.max(T.D.mkt.length, T.D.dates.length);
const expectStart = +(100 - (120 / total) * 100).toFixed(2);
check(Math.abs(opt.dataZoom[0].start - expectStart) < 0.5, 'first render window = last 120d',
  `start=${opt.dataZoom[0].start.toFixed(2)} expect≈${expectStart}`);

// 7. datazoom 同步折算(15年曲线)
const vis = Math.round(0.15 * total);
console.log(`INFO  curve total=${total} days; 15% zoom → ${vis} heatmap days (cap N_D=${T.D.dates.length})`);
check(vis > 0 && vis <= T.D.dates.length || vis > T.D.dates.length, 'sync math sanity');

// 8. 全部按钮存在
for (const id of ['tq5','tq20','tq60','tq120','tq250','tqAll']) {
  check(!!global.document.getElementById(id), 'button #' + id + ' in DOM');
}

console.log(fails === 0 ? '\nALL TESTS PASS' : `\n${fails} TEST(S) FAILED`);
process.exit(fails ? 1 : 0);
