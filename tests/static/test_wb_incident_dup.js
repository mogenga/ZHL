// 回归：创建异常重复单缺陷（2026-10 反馈：填 1 个组件出现 2 条异常）。
// 覆盖四组行为：
//   A. loadIncidents 到达后即使已选中批次也重渲中栏（修 boot/推进竞态）
//   B. modal 校验失败不关窗（onOk 返回 false 保留输入）
//   C. 创建异常表单防呆：已有同触发原因未关闭异常 → 提示行 + 按钮变「仍要新建」
//   D. 异常卡「自动」标记：incident_auto 轨迹推导 + 受影响组件行
//   E. 创建成功 toast 带短 ID
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const read = (f) => fs.readFileSync(path.join(root, 'app', 'static', 'wb', f), 'utf8');
const ui = read('ui.js'), api = read('api.js'), main = read('main.js');
const intake = read('stages/intake.js');
const judge = read('stages/judge.js');
const incident = read('stages/incident.js');
const reinspection = read('stages/reinspection.js');
const release = read('stages/release.js');
const report = read('report.js');

// ---- browser-like DOM shim（比 test_wb_global 更进一步：记录子树 + 事件可触发）----
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
        // 浏览器行为：select 默认选中第一个 option
        if (e._tag === 'select' && c._tag === 'option' && !e.value) {
          e.value = c._attr_value != null ? c._attr_value : c.textContent;
        }
      }
    },
    replaceChildren(...kids) { e.children = []; e.append(...kids); },
    // 真实 DOM 方法：ui.js 的 extraButtons（归因三选一）用到；
    // shim 缺它会让该路径在测试里抛 TypeError，掩盖真实行为。
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
    // 仅支持测试用到的简单选择器：'.cls'、'tag'
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

for (const [name, src] of [['ui.js', ui], ['api.js', api], ['main.js', main], ['intake.js', intake],
  ['judge.js', judge], ['incident.js', incident], ['reinspection.js', reinspection],
  ['release.js', release], ['report.js', report]]) {
  vm.runInContext(src, ctx, { filename: name });
}
const WB = ctx.WB;

// ---- 通用夹具 ----
const BATCH_ID = '11111111-1111-1111-1111-111111111111';
const INC_ID = '22222222-2222-2222-2222-222222222222';
const batch = {
  batch_id: BATCH_ID, batch_no: 'B-5', status: 'isolated',
  inspections: [{ sn: 'SN-N-057', intake_status: 'confirmed' }],
};
const openInc = {
  incident_id: INC_ID, batch_id: BATCH_ID, status: 'new',
  trigger_type: 'rule_fail', severity: 'major',
};

function openCreateForm(list) {
  WB.state.selected = batch;
  WB.state.batchId = BATCH_ID;
  WB.state.incidents = list;           // 面板据此出现「＋ 再建一条异常」
  WB.state.traces = [];
  WB.api.listIncidents = () => Promise.resolve({ ok: true, data: list });
  WB.api.createIncident = () => Promise.resolve({ ok: true, data: { incident_id: '33333333-3333-3333-3333-333333333333' } });
  const panel = WB.stages.incident.render(batch, {});
  const btn = findText(panel, '＋ 再建一条异常') || findText(panel, '创建异常');
  if (!btn) throw new Error('未找到创建异常入口按钮');
  btn.fire('click');
  return flush().then(flush);
}

(async function main() {
  // ---- A. loadIncidents 到达后重渲中栏（选中批次时也重渲）----
  {
    WB.state.batchId = BATCH_ID;
    WB.state.detailLoading = false;
    let renders = 0;
    const orig = WB._renderCenterAndRight;
    WB._renderCenterAndRight = function () { renders++; };
    WB.api.listIncidents = () => Promise.resolve({ ok: true, data: [] });
    await WB.loadIncidents();
    assert(renders >= 1, 'A: loadIncidents 完成后重渲中栏（选中批次时）');
    WB._renderCenterAndRight = orig;
  }

  // ---- B. modal：onOk 返回 false 不关窗 ----
  {
    let closed = null;
    WB.openModal('t', [], function () { return false; });
    WB._modal.submitBtn.fire('click');
    closed = !!WB._modal;
    assert(closed === true, 'B1: onOk 返回 false → modal 保持打开');
    WB.closeModal();
    WB.openModal('t', [], function () {});
    WB._modal.submitBtn.fire('click');
    assert(WB._modal === null, 'B2: onOk 未返回 false → modal 照常关闭');
  }

  // ---- C. 创建表单防呆提示 + 「仍要新建」 ----
  {
    await openCreateForm([openInc]);
    assert(!!WB._modal, 'C1: 打开创建表单');
    const box = WB._modal.box;
    assert(hasText(box, '仍要新建'), 'C2: 同触发原因未关闭异常存在 → 按钮变「仍要新建」');
    assert(hasText(box, '未关闭'), 'C3: 表单内有未关闭异常提示行');
    // 换一个无冲突的触发原因 → 提示消失、按钮恢复
    WB._modal.inputs[0].value = 'sample_fail';
    WB._modal.inputs[0].fire('change');
    assert(!hasText(box, '仍要新建'), 'C4: 换触发原因后按钮恢复「创建异常」');
    assert(WB._modal.submitBtn.textContent === '创建异常', 'C5: 按钮文案复原');
    WB.closeModal();
  }

  // ---- C''. 空条码校验失败不关窗、不发请求 ----
  {
    let posted = 0;
    await openCreateForm([]);
    WB.api.createIncident = function () { posted++; return Promise.resolve({ ok: true, data: {} }); };
    WB._modal.inputs[2].value = '';
    WB._modal.submitBtn.fire('click');
    assert(!!WB._modal, 'C6: 空受影响条码 → modal 不关（输入保留）');
    assert(hasText(WB._modal.box, '请填写受影响组件条码'), 'C7: 行内错误提示可见');
    assert(posted === 0, 'C8: 校验失败不发起创建请求');
    WB.closeModal();
  }

  // ---- D. 「自动」标记 + 受影响组件行 ----
  {
    WB.state.selected = batch;
    WB.state.incidents = [openInc];
    WB.state.traces = [{
      skill_name: 'incident_auto', validation_result: 'pass',
      structured_output: { created: true, incident_id: INC_ID },
      params_summary: { mapped_sns: ['SN-N-057'] },
    }];
    const panel = WB.stages.incident.render(batch, {});
    assert(hasText(panel, '自动'), 'D1: 命中 incident_auto 轨迹 → 卡片带「自动」标记');
    assert(hasText(panel, '受影响组件') && hasText(panel, 'SN-N-057'), 'D2: 卡片展示受影响组件');
    // 无轨迹（人工创建）→ 不标记
    WB.state.traces = [];
    const panel2 = WB.stages.incident.render(batch, {});
    const card = panel2; // 简化：整面板无「自动」文本（提示行也只在有自动卡时出现）
    assert(!hasText(card, '自动'), 'D3: 人工异常不带「自动」标记');
  }

  // ---- E. 创建成功 toast 带短 ID ----
  {
    await openCreateForm([]);
    WB._modal.inputs[2].value = 'SN-N-057';
    WB._modal.submitBtn.fire('click');
    await flush();
    const toast = byId.toast ? byId.toast.textContent : '';
    assert(toast.indexOf('33333333') >= 0, 'E1: toast 含新异常短 ID（33333333）');
    assert(WB._modal === null, 'E2: 创建成功后 modal 关闭');
  }

  console.log(failed === 0 ? '\nALL PASS' : '\n' + failed + ' FAILED');
  // ---- F. AI 归因按钮：点击须有反馈且必须真的发出请求 ----
  // （2026-10-15 反馈「AI 归因点击无反应」：真模型要 40~90s，旧实现
  //   超时写死 15s 必被 abort 且全程无 loading；这里钉住"点得动、有反馈、
  //   发得出请求、失败有提示"四点。）
  {
    const isoInc = { incident_id: INC_ID, batch_id: BATCH_ID, status: 'isolated', trigger_type: 'rule_fail', severity: 'major' };
    WB.state.selected = batch; WB.state.batchId = BATCH_ID;
    WB.state.incidents = [isoInc]; WB.state.traces = [];
    WB.api.listIncidents = () => Promise.resolve({ ok: true, data: [isoInc] });
    let called = null;
    let resolveAttr = null;
    WB.api.attributeIncident = function (id, dec, cause) {
      called = { id: id, dec: dec, cause: cause };
      return new Promise(function (res) { resolveAttr = res; });
    };
    const panel = WB.stages.incident.render(batch, {});
    const btn = findText(panel, 'AI 归因');
    assert(!!btn, 'F1: isolated 态渲染出「AI 归因」按钮');
    btn.fire('click');
    await flush();
    assert(!!called, 'F2: 点击真的发出归因请求');
    assert(called.dec === null && called.cause === null, 'F3: 第一步不带 decision（生成+停驻）');
    assert(hasText(panel, 'AI 归因中'), 'F4: 等待期间有进行中反馈（不再是「点了没反应」）');

    resolveAttr({ ok: false, error: { key: 'timeout', msg: '请求超时', status: 0, detail: null } });
    await flush(); await flush();
    assert(hasText(panel, '请求超时'), 'F5: 超时/失败有可见提示');
    assert(!hasText(panel, 'AI 归因中'), 'F6: 失败后恢复，不卡在「归因中」');
  }

  process.exit(failed === 0 ? 0 : 1);
})().catch(function (e) { console.error('FATAL:', e); process.exit(1); });
