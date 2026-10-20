// 回归测试: sector_viewer 前端 (DOM/echarts mock) — 含 S3-C 语义引擎用例
// 用法: node test_sector_viewer.js [html路径]  (默认 out/sector_heat_test.html; pytest 用夹具HTML)
const fs = require('fs');
const htmlPath = process.argv[2] || 'out/sector_heat_test.html';
const html = fs.readFileSync(htmlPath, 'utf8');
const app = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1])[2];

function el(id) {
  const attrs = {};
  return { id, style: {}, innerHTML: '', textContent: '', value: '',
    addEventListener(){}, setAttribute(k, v){ attrs[k] = v; }, getAttribute(k){ return attrs[k] || 'light'; },
    appendChild(){}, classList:{add(){},remove(){},toggle(){}}, onclick:null, oninput:null, focus(){},
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
// DOM 元素缓存(便于 innerHTML 断言)
const els = {};
global.document = { getElementById(id){ return els[id] || (els[id] = el(id)); },
  createElement:()=>el('x'), addEventListener(){}, querySelector:()=>el('q'),
  querySelectorAll:()=>[], body: el('body'),
  get documentElement(){ return this.getElementById('html'); } };
global.window = global; global.addEventListener = ()=>{};
// S3-D: getComputedStyle mock — 按当前 data-theme 返回CSS变量(与页内:root一致)
global.getComputedStyle = () => {
  const dark = global.document.documentElement.getAttribute('data-theme') === 'dark';
  const m = dark
    ? {'--i2':'#c3c2b7','--m':'#898781','--g':'#2c2c2a','--a':'#383835','--rd':'#e66767','--bl':'#3987e5','--i':'#f0efec'}
    : {'--i2':'#52514e','--m':'#898781','--g':'#e1e0d9','--a':'#c3c2b7','--rd':'#e34948','--bl':'#2a78d6','--i':'#0b0b0b'};
  return { getPropertyValue: n => m[n] || '' };
};
let curveChart = null, rankChart = null, slideChart = null;
global.echarts = { init(dom){ const c = mkChart();
  if (dom && dom.id === 'curve') curveChart = c;
  if (dom && dom.id === 'rank') rankChart = c;
  if (dom && dom.id === 'slider') slideChart = c;
  return c; } };

const driver = app + ';globalThis.__T={toggleSel,hmZoom,drawCurve,drawSideFor,drawSlider,drawLeft,renderCards,updateMkt,' +
  'semanticOf,semShort,divergenceOf,latestComps,deltaAt,scoreAt,matAt,D,hmDays:()=>hmDaysGlobal,' +
  'getSel:()=>selected,setCur:d=>curDay=d,setMode:m=>mode=m,getPAL:()=>PAL};';
new Function(driver)();

const T = globalThis.__T;
let fails = 0;
const check = (ok, name, extra) => {
  console.log((ok ? 'PASS' : 'FAIL') + '  ' + name + (extra ? '  ' + extra : ''));
  if (!ok) fails++;
};

// ── 既有回归 ──
check(T.hmDays() === 120, 'default hmDaysGlobal = 120', T.hmDays());

const bk1 = T.D.codes.findIndex(c => c.startsWith('BK'));
T.toggleSel(bk1);
let opt = curveChart._opt;
check(opt.series.length === 2 && opt.series[0].name === '大盘情绪分', 'mkt bg series present',
  opt.series.map(s => s.name).join('|'));
check(opt.series[0].silent === true && opt.series[0].z === 1 && opt.series[1].z === 10, 'z-layering 1<10');
check(opt.legend.data[0] === '大盘情绪分', 'legend includes 大盘情绪分');

opt.legend = { ...opt.legend, selected: { '大盘情绪分': false } };
const bk2 = T.D.codes.findIndex((c, i) => c.startsWith('BK') && i !== bk1);
T.toggleSel(bk2);
opt = curveChart._opt;
check(opt.legend.selected['大盘情绪分'] === false, 'legend hidden state preserved after redraw',
  JSON.stringify(opt.legend.selected));

curveChart._opt = { ...curveChart._opt, dataZoom: [{ type:'inside', start: 40, end: 55 }] };
T.toggleSel(bk2);   // 移除 bk2 (仍剩 bk1) → 重绘
opt = curveChart._opt;
check(opt.dataZoom[0].start === 40 && opt.dataZoom[0].end === 55, 'zoom preserved on redraw',
  JSON.stringify(opt.dataZoom[0]));
T.toggleSel(bk1);   // 移除最后一个 → 无选中分支
opt = curveChart._opt;
check(opt.series.length === 1 && opt.series[0].name === '大盘情绪分', 'no-selection branch restores mkt-only');

curveChart._opt = null;
T.drawCurve();
opt = curveChart._opt;
const total = Math.max(T.D.mkt.length, T.D.dates.length);
// 小数据集(夹具 total<120)时窗口钳到 0
const expectStart = Math.max(0, +(100 - (120 / total) * 100).toFixed(2));
check(Math.abs(opt.dataZoom[0].start - expectStart) < 0.5, 'first render window = last 120d',
  `start=${opt.dataZoom[0].start.toFixed(2)} expect≈${expectStart}`);

for (const id of ['tq5','tq20','tq60','tq120','tq250','tqAll']) {
  check(!!els[id], 'button #' + id + ' in DOM');
}

// ── S3-C: 语义引擎 ──
// 9. semanticOf 规则表
const semCases = [
  [70, -15, '高位回落 · 警惕退潮'], [70, 15, '加速冲顶 · 情绪亢奋'], [65, 0, '过热区 · 谨慎追高'],
  [45, 5, '强势升温 · 趋势延续'], [45, -1, '强势区 · 动能减弱'], [25, 8, '升温发酵 · 关注持续性'],
  [25, 0, '温热区 · 滞涨迹象'], [0, 8, '中性回暖'], [0, -8, '中性转弱'], [0, 0, '中性震荡'],
  [-70, -15, '恐慌探底 · 关注超跌反弹'], [-70, 0, '深度冰点 · 左侧观察区'],
  [-50, 8, '冰点修复 · 反弹尝试'], [-50, 0, '偏冷区 · 观望为主'], [-30, 8, '低位回暖'], [-30, 0, '持续偏冷'],
  [80, null, '过热区 · 谨慎追高'], [-5, null, '中性震荡'],
];
let semOk = true, semBad = '';
for (const [v, d5, want] of semCases) {
  const got = T.semanticOf(v, d5);
  if (got !== want) { semOk = false; semBad += `[${v},${d5}]${got}≠${want} `; }
}
check(semOk, 'semanticOf rule table (18 cases)', semBad || 'all match');
check(T.semShort('高位回落 · 警惕退潮') === '高位回落', 'semShort takes first segment');

// 10. divergenceOf 合成用例(阈值±50)
const divCases = [
  [{chg: 55, vol_dir: -55}, '⚠ 缩量上涨 · 量价背离'],
  [{chg: 55, vol_dir: 10, fflow: -55}, '⚠ 价涨资金流出 · 背离'],
  [{chg: -55, vol_dir: 55}, '放量下跌 · 恐慌宣泄'],
  [{chg: -55, vol_dir: 0, fflow: 55}, '价跌资金流入 · 有承接'],
  [{chg: 10, vol_dir: 10}, null],
  [{vol_dir: -55}, null], [null, null],
  [{chg: 49, vol_dir: -60}, null],   // 边界下不触发
  [{chg: 60, vol_dir: -49}, null],
];
let divOk = true, divBad = '';
for (const [comps, want] of divCases) {
  const got = T.divergenceOf(comps);
  if (got !== want) { divOk = false; divBad += `${JSON.stringify(comps)}→${got}≠${want} `; }
}
check(divOk, 'divergenceOf synthetic cases (8)', divBad || 'all match');

// 11. side 面板: 语义头 + 对比表(selected≥2)
T.toggleSel(bk1);   // 现在有 bk1+bk2 两个选中? 先确认状态
// 当前 selected: bk1 (bk2 被移除了); 再加回 bk2
if (!T.getSel().includes(bk2)) T.toggleSel(bk2);
T.drawSideFor(bk1);
const sideHtml = els['side'].innerHTML;
check(sideHtml.includes('class="sem"') && sideHtml.includes('分量明细（按贡献排序）'),
  'side has semantic header + comp table');
check(sideHtml.includes('class="cmp"') && sideHtml.includes('<th>对比</th>'),
  'side has comparison table when 2+ selected');
check(T.getSel().length === 2, 'two boards selected', T.getSel().length);

// 12. 贡献排序: 分量表按 |v| 降序
{
  const lc = T.latestComps(bk1);
  const comps = lc.comps;
  const rows = [...sideHtml.matchAll(/<tr><td>([^<]+)<\/td>/g)].map(m => m[1])
    .filter(n => n !== '分量' && !n.includes('对比'));
  // rows 包含 cmp 表的行名 — 分量表在 <div class="date"> 之后
  const compPart = sideHtml.split('<table><tr><th>分量</th>')[1] || '';
  const compRows = [...compPart.matchAll(/<tr><td>([^<]+)<\/td>/g)].map(m => m[1]);
  const vals = compRows.map(n => { for (const [k, lab] of Object.entries(T.D.clabels)) if (lab === n) return Math.abs(comps[k]); return -1; });
  const sorted = vals.every((v, i) => i === 0 || vals[i-1] >= v);
  check(compRows.length > 0 && sorted, 'components sorted by |contribution| desc',
    compRows.slice(0, 4).join(',') + ' | ' + vals.slice(0, 4).map(v => Math.round(v)).join(','));
}

// 13. hover 路径单选(无对比表)
T.toggleSel(bk1); T.toggleSel(bk2);   // 清空
T.drawSideFor(bk1);
check(!els['side'].innerHTML.includes('class="cmp"'), 'no cmp table when none selected');
check(els['side'].innerHTML.includes('class="sem"'), 'hover path still shows semantic header');

// 14. 卡片语义 + 大盘语义
T.renderCards();
check(els['hotCards'].innerHTML.includes('class="sem"'), 'cards include semantic label');
T.updateMkt();
check(typeof els['mktSem'].textContent === 'string' && els['mktSem'].textContent.length > 0,
  'mkt-box semantic phrase', els['mktSem'].textContent);

// 15. side 语义头与分量表同日对齐(不随日期滑块漂移)
{
  T.toggleSel(bk1); T.toggleSel(bk2);            // 2 板块选中
  T.drawSideFor(bk1);
  const semNow = (els['side'].innerHTML.match(/class="sem"[^>]*>([^<]+)</) || [])[1];
  // 期望值: 按分量表日期(lc.date)计算, 而非 curDay
  const lc = T.latestComps(bk1);
  const adi = T.D.dates.indexOf(lc.date);
  const v = adi >= 0 ? T.scoreAt(adi, bk1) : null;   // P5紧凑编码
  const d5 = T.deltaAt(adi, bk1, 5);
  const expect = v === null ? '' :
    (d5 !== null ? `5日${d5 > 0 ? '+' : ''}${d5} · ` : '') + T.semanticOf(v, d5);
  check(semNow === expect, 'side semantic computed at comp-table date',
    `got="${semNow}" expect="${expect}"`);
  T.setCur(0);                                    // 滑块拖到最早日期
  T.drawSideFor(bk1);
  const semOld = (els['side'].innerHTML.match(/class="sem"[^>]*>([^<]+)</) || [])[1];
  check(semOld === semNow, 'side semantic invariant to date slider', `"${semOld}" vs "${semNow}"`);
  T.setCur(T.D.dates.length - 1);                 // 复位
}

// ── S3-D: 主题令牌 / 切换重绘 / 降噪 / 栅格 ──
// 16. 令牌浅色值 + 降噪断言(热力图模式)
check(T.getPAL().pos === '#e34948' && T.getPAL().neg === '#2a78d6', 'PAL light pos/neg from CSS vars');
check(T.getPAL().splitA[0] === 'rgba(0,0,0,0.02)', 'PAL light splitArea');
T.setMode('heat'); T.drawLeft();
let hmOpt = rankChart._opt;
check(hmOpt.series[0].itemStyle.borderWidth === 0, 'heatmap cell border removed (denoise)');
check(hmOpt.visualMap.textStyle.fontSize === 10, 'visualMap legend fontSize 10');
check(hmOpt.yAxis.axisLabel.rich.g.color === '#0b0b0b', 'group label uses PAL.strong (light)');
check(hmOpt.xAxis.splitArea.areaStyle.color[0] === 'rgba(0,0,0,0.02)', 'heatmap splitArea from PAL');
check(hmOpt.title.textStyle.color === '#52514e', 'heatmap title uses PAL.txt (light)');

// 17. 主题切换 → 令牌更新 + 全量重绘为暗色值
els['themeToggle'].onclick();
check(global.document.documentElement.getAttribute('data-theme') === 'dark', 'theme toggled to dark');
check(T.getPAL().strong === '#f0efec' && T.getPAL().txt === '#c3c2b7', 'PAL re-read as dark',
  JSON.stringify({strong: T.getPAL().strong, txt: T.getPAL().txt}));
check(T.getPAL().splitA[0] === 'rgba(255,255,255,0.02)', 'PAL dark splitArea');
hmOpt = rankChart._opt;   // drawLeft 已被 themeToggle 触发(heat 模式)
check(hmOpt.yAxis.axisLabel.rich.g.color === '#f0efec', 'heatmap re-rendered with dark strong');
check(hmOpt.title.textStyle.color === '#c3c2b7', 'heatmap title dark txt');
check(hmOpt.tooltip.backgroundColor === 'rgba(38,38,37,0.96)', 'tooltip dark background');

// 19. S3D-review: 滑块位置跨重绘保留(主题切换不失同步)
slideChart._opt = { dataZoom: [{ type: 'slider', start: 33.3, end: 41.7 }] };
T.drawSlider();
check(slideChart._opt.dataZoom[0].start === 33.3 && slideChart._opt.dataZoom[0].end === 41.7,
  'slider position preserved on redraw', JSON.stringify(slideChart._opt.dataZoom[0]));
slideChart._opt = null;
T.drawSlider();
check(slideChart._opt.dataZoom[0].end === 100, 'first render slider defaults to last 31d');

// 20. S3D-review: 曲线图例滚动模式(flex-4宽度下6项不换行压图)
T.setMode('rank');
T.toggleSel(bk1);
check(curveChart._opt.legend.type === 'scroll', 'curve legend type scroll',
  JSON.stringify(curveChart._opt.legend.type));
check(hmOpt.xAxis.splitArea.areaStyle.color[0] === 'rgba(255,255,255,0.02)', 'heatmap dark splitArea');
check(slideChart._opt.dataZoom[0].borderColor === '#383835', 'slider re-rendered with dark border');

// 18. 栅格 8:4 与卡片内边距(CSS 断言)
check(/#rank\{flex:8/.test(html), 'CSS: #rank flex 8 (12-grid)');
check(/#curve\{flex:4/.test(html), 'CSS: #curve flex 4 (12-grid)');
check(/\.card\{[^}]*padding:8px 12px/.test(html), 'CSS: card padding 8px 12px');
check(/\.chart-row\{[^}]*gap:12px/.test(html), 'CSS: chart-row gap 12px');

console.log(fails === 0 ? '\nALL TESTS PASS' : `\n${fails} TEST(S) FAILED`);
process.exit(fails ? 1 : 0);
