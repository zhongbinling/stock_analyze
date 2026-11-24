// 页面脚本的迷你DOM(供 node 沙箱行为测试共用): innerHTML 里的元素按属性解析成桩节点,
// 支持 "[attr]" / "[attr=v]" / "tag" 三类子节点匹配, 文档级选择器可带 "#id " 前缀、逗号分隔。
'use strict';

function parseKids(src) {
  const out = [];
  const re = /<(\w+)([^>]*)>/g;
  let m;
  while ((m = re.exec(src))) {
    const el = {tag: m[1], dataset: {}, style: {}, value: '', textContent: '', innerHTML: '',
                checked: false, disabled: false, onclick: null, onchange: null, oninput: null,
                classList: [], id: ''};
    const attrs = m[2];
    const cls = /\bclass="([^"]*)"/.exec(attrs);
    if (cls) el.classList = cls[1].split(/\s+/).filter(Boolean);
    const idm = /\bid="([^"]*)"/.exec(attrs);
    if (idm) el.id = idm[1];
    const val = /\bvalue="([^"]*)"/.exec(attrs);
    if (val) el.value = val[1];
    if (/\schecked\b/.test(attrs)) el.checked = true;
    if (/\sdisabled\b/.test(attrs)) el.disabled = true;
    let d;
    const dre = /\bdata-([\w-]+)="([^"]*)"/g;
    while ((d = dre.exec(attrs))) el.dataset[d[1]] = d[2];
    if (el.tag === 'select') {                     // 取 selected 的 option 作当前值
      const body = src.slice(m.index, src.indexOf('</select>', m.index));
      const opts = [...body.matchAll(/<option value="([^"]*)"( selected)?/g)];
      const sel = opts.find(o => o[2]) || opts[0];
      el.value = sel ? sel[1] : '';
    }
    out.push(el);
  }
  return out;
}

function kidsMatching(kids, sel) {
  const strip = a => a.replace(/^data-/, '');          // 选择器里的 [data-fk] ↔ dataset.fk
  return sel.split(',').flatMap(s => {
    s = s.trim();
    let m = /^\[([\w-]+)="([^"]*)"\]$/.exec(s);        // [attr="v"]
    if (m) return kids.filter(k => k.dataset[strip(m[1])] === m[2]);
    m = /^\[([\w-]+)\]$/.exec(s);                      // [attr]
    if (m) return kids.filter(k => strip(m[1]) in k.dataset);
    m = /^(\w+)$/.exec(s);                             // tag
    if (m) return kids.filter(k => k.tag === m[1]);
    return [];
  });
}

function makeDom() {
  const els = {};
  const $id = id => {
    for (const e of Object.values(els)) {          // innerHTML 里带id的节点
      const k = e._kids.find(x => x.id === id);
      if (k) return k;
    }
    return els[id] || (els[id] = new El(id));
  };
  const listeners = {};
  class El {
    constructor(id) { this.id = id; this._html = ''; this.textContent = ''; this.style = {};
                      this.disabled = false; this.value = ''; this.dataset = {}; this._kids = [];
                      this.onclick = null; this.onchange = null; this.oninput = null; this.checked = false; }
    get innerHTML() { return this._html; }
    set innerHTML(v) { this._html = String(v); this._kids = parseKids(this._html); }
    querySelectorAll(sel) { return kidsMatching(this._kids, sel); }   // 元素自身的查询
    addEventListener() {}
  }
  const document = {
    querySelector(sel) {
      if (sel.includes('csrf-token')) return {content: 'CSRF'};
      const simple = /^#([\w-]+)$/.exec(sel.trim());               // "#id"
      if (simple) return $id(simple[1]);
      return this.querySelectorAll(sel)[0] || null;
    },
    getElementById: $id,
    querySelectorAll(sel) {                           // "#id 子选择器" → 该元素的子节点
      return sel.split(',').flatMap(s => {
        const m = /^#(\w+)\s+(.+)$/.exec(s.trim());
        return m ? kidsMatching($id(m[1])._kids, m[2]) : [];
      });
    },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    fire(ev) { (listeners[ev] || []).forEach(fn => fn()); },
    hidden: false,
  };
  const charts = {};
  const echarts = {init(el) {
    const c = {id: el.id, opt: null, n: 0,
               setOption(o) { this.opt = o; this.n++; },
               clear() { this.opt = null; }, on() {}, getOption() { return this.opt; }, resize() {}};
    charts[el.id] = c;
    return c;
  }};
  return {document, charts, echarts, els, $id};
}

module.exports = {makeDom, parseKids};
