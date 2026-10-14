// 回归：抽样结论永久阻断放行时，工作台没有可执行的出路（2026-10 反馈：
// 「复验关闭后，✗ 抽样结论通过：sample result is fail, expected pass，无法放行」）。
// 覆盖三组行为：
//   A. 抽样结论未通过 → 放行决策面板出现「重算复检结论」入口
//   B. 点击后调用 /recheck；闭环成立（written/已在）→ 重跑前置校验
//   C. 闭环没走完（result=fail）→ 如实提示缺哪只 SN，不刷新成"通过"
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const read = (f) => fs.readFileSync(path.join(root, 'app', 'static', 'wb', f), 'utf8');

function makeEl(tag) {
  const e = {
    _tag: tag, nodeType: 1, children: [], _ev: {}, parentNode: null,
    style: {}, dataset: {}, className: '', textContent: '', value: '',
    disabled: false, checked: false, innerHTML: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    get childNodes() { return e.children; },
    append(...kids) {
      for (const c of kids) {
        if (c == null) continue;
        if (Array.isArray(c)) { e.append(...c); continue; }
        e.children.push(c);
        try { Object.defineProperty(c, 'parentNode', { value: e, configurable: true }); } catch (_) {}
        if (e._tag === 'select' && c._tag === 'option' && !e.value) {
          e.value = c._attr_value != null ? c._attr_value : c.textContent;
        }
      }
    },
    replaceChildren(...kids) { e.children = []; e.append(...kids); },
    insertBefore(n, ref) {
      const i = e.children.indexOf(ref);
      if (i < 0) e.children.push(n); else e.children.splice(i, 0, n);
      try { Object.defineProperty(n, 'parentNode', { value: e, configurable: true }); } catch (_) {}
      return n;
    },
    remove() {
      const p = e.parentNode;
      if (p) { const i = p.children.indexOf(e); if (i >= 0) p.children.splice(i, 1); }
    },
    setAttribute(k, v) { e['_attr_' + k] = v; },
    addEventListener(type, fn) { (e._ev[type] = e._ev[type] || []).push(fn); },
    removeEventListener(type, fn) {
      const a = e._ev[type] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
    },
    fire(type) { (e._ev[type] || []).slice().forEach((fn) => fn({ target: e, type })); },
  };
  e.querySelector = (sel) => e.querySelectorAll(sel)[0] || null;
  e.querySelectorAll = (sel) => {
    const out = [];
    const hit = (n) => {
      if (sel[0] === '.') return (' ' + n.className + ' ').includes(sel.slice(1));
      return n._tag === sel;
    };
    const walk = (n) => (n.children || []).forEach((c) => { if (hit(c)) out.push(c); walk(c); });
    walk(e);
    return out;
  };
  return e;
}
function findText(node, text) {
  if (node.textContent === text) return node;
  for (const c of node.children || []) { const r = findText(c, text); if (r) return r; }
  return null;
}
function hasText(node, text) {
  if (typeof node.textContent === 'string' && node.textContent.indexOf(text) >= 0) return true;
  for (const c of node.children || []) { if (hasText(c, text)) return true; }
  return false;
}

const byId = {};
const documentStub = {
  readyState: 'complete',
  getElementById: (i) => (byId[i] = byId[i] || makeEl('div')),
  createElement: (t) => makeEl(t),
  createTextNode: (t) => ({ nodeType: 3, textContent: t, value: t }),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener() {},
  body: makeEl('body'),
};
const urlObj = new URL('http://x/static/workbench.html');
const ctx = vm.createContext({
  document: documentStub,
  window: { location: urlObj, origin: 'http://x' },
  location: urlObj,
  URL: (typeof URL !== 'undefined' ? URL : undefined),
  URLSearchParams,
  history: { replaceState() {} },
  setTimeout, clearTimeout, setInterval, clearInterval, console,
  fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('null') }),
  AbortController: function () {},
});

let failed = 0;
function assert(c, m) { if (!c) { console.error('FAIL:', m); failed++; } else console.log('ok:', m); }
const flush = () => new Promise((r) => setTimeout(r, 0));

for (const [name, src] of [['ui.js', read('ui.js')], ['api.js', read('api.js')],
  ['main.js', read('main.js')], ['release.js', read('stages/release.js')]]) {
  vm.runInContext(src, ctx, { filename: name });
}
const WB = ctx.WB;

const BATCH_ID = '11111111-1111-1111-1111-111111111111';
const batch = { batch_id: BATCH_ID, batch_no: 'B-LIVEMODEL-1', status: 'pending_release' };

// 与用户截图同款的七项断言结果：只有 sample_passed 未过。
const CHECKS_FAIL = [
  { name: 'state', passed: true, reason: '' },
  { name: 'sample_passed', passed: false, reason: 'sample result is fail, expected pass' },
  { name: 'no_open_critical_incident', passed: true, reason: '' },
  { name: 'no_isolated_sn', passed: true, reason: '' },
  { name: 'all_evidence_present', passed: true, reason: '' },
  { name: 'all_capa_verified', passed: true, reason: '' },
  { name: 'reinspection_scope_covered', passed: true, reason: '' },
];
const CHECKS_PASS = CHECKS_FAIL.map((c) => ({ ...c, passed: true, reason: '' }));

function setup(checks) {
  let releaseCalls = 0;
  WB.api.releaseBatch = function () {
    releaseCalls++;
    return Promise.resolve({ ok: true, data: { passed: false, checks: checks } });
  };
  const panel = WB.stages.release.render(batch, {});
  return flush().then(flush).then(() => ({ panel: panel, calls: () => releaseCalls }));
}

(async function main() {
  // ---- A. 抽样结论未通过 → 出现重算入口 ----
  {
    const { panel } = await setup(CHECKS_FAIL);
    assert(!!findText(panel, '重算复检结论'),
      'A1: sample_passed 未通过 → 面板出现「重算复检结论」');
    assert(hasText(panel, 'sample result is fail, expected pass'),
      'A2: 未通过原因原样展示（不吞掉后台 reason）');
  }

  // ---- A2. 没有抽样阻断时不出现该入口 ----
  {
    let calls = 0;
    WB.api.releaseBatch = function () {
      calls++;
      return Promise.resolve({ ok: true, data: { passed: true, checks: CHECKS_PASS } });
    };
    const panel = WB.stages.release.render(batch, {});
    await flush(); await flush();
    assert(!findText(panel, '重算复检结论'),
      'A3: 抽样结论已通过 → 不出现重算入口（避免误触）');
  }

  // ---- B. 点击 → 调 /recheck；闭环成立 → 重跑前置校验 ----
  {
    const { panel, calls } = await setup(CHECKS_FAIL);
    let recheckIds = [];
    WB.api.recheckBatch = function (id) {
      recheckIds.push(id);
      return Promise.resolve({ ok: true, data: { result: 'pass', written: true, sample_round: 2 } });
    };
    const before = calls();
    findText(panel, '重算复检结论').fire('click');
    await flush(); await flush();
    assert(recheckIds.length === 1 && recheckIds[0] === BATCH_ID,
      'B1: 点击后以批次 id 调用重算接口');
    assert(calls() > before, 'B2: 补写成功后重跑前置校验（界面拿到最新七项）');
  }

  // ---- C. 闭环没走完 → 如实提示，不刷新成"通过" ----
  {
    const { panel, calls } = await setup(CHECKS_FAIL);
    WB.api.recheckBatch = function () {
      return Promise.resolve({
        ok: true,
        data: { result: 'fail', written: false, reason: 'sample round 2 not discharged — SN-B: no passing reinspection covering this SN' },
      });
    };
    const before = calls();
    findText(panel, '重算复检结论').fire('click');
    await flush(); await flush();
    assert(calls() === before, 'C1: 闭环未走完不重跑校验（不制造"已重算"的假象）');
  }

  // ---- D. 该批次根本没有抽样结论 → 说清楚，不套"复检未覆盖"的话术 ----
  {
    const { panel, calls } = await setup(CHECKS_FAIL);
    WB.api.recheckBatch = function () {
      return Promise.resolve({
        ok: true,
        data: { result: '', written: false, skipped: 'no_sample_conclusion' },
      });
    };
    const before = calls();
    findText(panel, '重算复检结论').fire('click');
    await flush(); await flush();
    const toastEl = ctx.document.getElementById('toast');
    assert(toastEl.textContent.indexOf('复检闭环未走完') < 0,
      'D1: 无抽样结论时不套用「复检未覆盖」话术');
    assert(toastEl.textContent.indexOf('抽样结论') >= 0,
      'D2: 明说该批次没有抽样结论');
    assert(calls() === before, 'D3: 不重跑校验');
  }

  console.log(failed ? '\nFAILED: ' + failed : '\nALL PASS');
  process.exit(failed ? 1 : 0);
})();
