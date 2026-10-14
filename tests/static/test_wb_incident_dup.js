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
  const cls = new Set();
  const e = {
    _tag: tag, nodeType: 1, children: [], _ev: {}, parentNode: null,
    style: {}, dataset: {}, textContent: '', value: '',
    disabled: false, checked: false, innerHTML: '',
    // classList 必须真记住 class：左列标签的选中态（.on）就是这么表达的，
    // 用 no-op 桩会让「视图切了、高亮没跟着切」这类缺陷在测试里不可见
    //（2026-10-15 反馈：异常列表点批次 → 主区回批次，标签仍高亮「异常」）。
    get className() { return Array.from(cls).join(' '); },
    set className(v) {
      cls.clear();
      String(v == null ? '' : v).split(/\s+/).forEach(function (c) { if (c) cls.add(c); });
    },
    classList: {
      add(c) { cls.add(c); },
      remove(c) { cls.delete(c); },
      toggle(c, force) {
        const on = force === undefined ? !cls.has(c) : !!force;
        if (on) cls.add(c); else cls.delete(c);
        return on;
      },
      contains(c) { return cls.has(c); },
    },
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

// 左列四个视图标签：workbench.html 的静态标记，shim 不解析 HTML，在此手工立起
//（标签选中态由 .on 表达，见 makeEl 的 classList 实现）
const leftTabs = ['batch', 'rules', 'incidents', 'reports'].map(function (v) {
  const b = makeEl('button');
  b.dataset.view = v;
  b.className = v === 'batch' ? 'on' : '';
  return b;
});

const byId = {};
const documentStub = {
  readyState: 'complete',
  getElementById: (i) => (byId[i] = byId[i] || makeEl('div')),
  createElement: (t) => makeEl(t),
  createTextNode: (t) => ({ nodeType: 3, textContent: t, value: t }),
  querySelectorAll: (sel) => (sel === '[data-view]' ? leftTabs : []),
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

  // ---- G. FR-3.6 三选一：采纳 / 驳回 / 修改 三个选项都在，且结果可区分 ----
  // （2026-10-15 反馈：FR-3.6 规定「采纳、驳回、修改」，实现却是「搁置」，
  //   且旧版根因是自由文本框、三种情况全记 adopt，库里分不清照抄/改过/手填。）
  {
    const isoInc = { incident_id: INC_ID, batch_id: BATCH_ID, status: 'isolated', trigger_type: 'rule_fail', severity: 'major' };
    WB.state.selected = batch; WB.state.batchId = BATCH_ID;
    WB.state.incidents = [isoInc]; WB.state.traces = [];
    WB.api.listIncidents = () => Promise.resolve({ ok: true, data: [isoInc] });
    const sent = [];
    WB.api.attributeIncident = () => Promise.resolve({
      ok: true,
      data: { status: 'awaiting_human', hypotheses: [
        { cause: '电池片隐裂', confidence: 0.82, evidence_ids: ['e1', 'e2'] },
        { cause: '焊接虚焊', confidence: 0.5, evidence_ids: ['e3'] },
      ] },
    });
    WB.api.adoptAttribution = (id, dec, cause, opts) => {
      sent.push({ dec: dec, cause: cause, opts: opts || {} });
      return Promise.resolve({ ok: true, data: { decision: dec } });
    };
    const panel = WB.stages.incident.render(batch, {});
    findText(panel, 'AI 归因').fire('click');
    await flush(); await flush();
    const box = WB._modal && WB._modal.box;
    assert(!!box, 'G1: 归因结果弹窗打开');
    assert(hasText(box, '电池片隐裂') && hasText(box, '焊接虚焊'), 'G2: 两条假设都渲染');
    assert(hasText(box, '置信度 82%'), 'G3: 展示置信度');
    assert(hasText(box, '证据 2 条'), 'G4: 展示证据条数');
    // 三选一的三个动作
    assert(!!findText(box, '采纳选中假设'), 'G5: 选项一「采纳」在位');
    assert(!!findText(box, '驳回'), 'G6: 选项二「驳回」在位');
    assert(!!findText(box, '修改后采纳'), 'G7: 选项三「修改」在位（FR-3.6 的第三个，非「搁置」）');
    assert(!hasText(box, '搁置'), 'G8: 不再把「搁置」当作三选之一');
    // 默认选中第一条 → 采纳必须带 hypothesis_index，让后端按原文取值
    // （前端传的文本改不了它——这就是"采纳"与"手工填写"在数据上的分界）。
    WB._modal.submitBtn.fire('click');
    await flush(); await flush();
    const adopt = sent[sent.length - 1];
    assert(adopt.dec === 'adopt', 'G9: 主按钮提交 adopt');
    assert(adopt.opts.hypothesisIndex === 0, 'G10: 采纳带 hypothesis_index（命中率统计 FR §11.4）');
    // 切换单选项 → 下标跟着走（radio 的 change 回调）
    const radios = box.querySelectorAll('input');
    assert(radios.length >= 2, 'G11: 假设渲染为可选项（单选）');
    radios[1].checked = true;
    radios[1].fire('change');
    WB.api.attributeIncident = () => Promise.resolve({
      ok: true,
      data: { status: 'awaiting_human', hypotheses: [
        { cause: '电池片隐裂', confidence: 0.82, evidence_ids: ['e1'] },
        { cause: '焊接虚焊', confidence: 0.5, evidence_ids: ['e3'] },
      ] },
    });
    const panel2 = WB.stages.incident.render(batch, {});
    findText(panel2, 'AI 归因').fire('click');
    await flush(); await flush();
    const r2 = WB._modal.box.querySelectorAll('input');
    r2[1].checked = true;
    r2[1].fire('change');
    WB._modal.submitBtn.fire('click');
    await flush(); await flush();
    const adopt2 = sent[sent.length - 1];
    assert(adopt2.opts.hypothesisIndex === 1, 'G12: 选中第 2 条时 index=1（不是写死 0）');
  }

  // ---- H. 「修改后采纳」提交 modified_cause + 原假设下标 ----
  {
    const isoInc = { incident_id: INC_ID, batch_id: BATCH_ID, status: 'isolated', trigger_type: 'rule_fail', severity: 'major' };
    WB.state.selected = batch; WB.state.batchId = BATCH_ID;
    WB.state.incidents = [isoInc]; WB.state.traces = [];
    WB.api.listIncidents = () => Promise.resolve({ ok: true, data: [isoInc] });
    WB.api.attributeIncident = () => Promise.resolve({
      ok: true,
      data: { status: 'awaiting_human', hypotheses: [{ cause: '电池片隐裂', confidence: 0.8, evidence_ids: ['e1'] }] },
    });
    const sent = [];
    WB.api.adoptAttribution = (id, dec, cause, opts) => {
      sent.push({ dec: dec, cause: cause, opts: opts || {} });
      return Promise.resolve({ ok: true, data: { decision: dec } });
    };
    const panel = WB.stages.incident.render(batch, {});
    findText(panel, 'AI 归因').fire('click');
    await flush(); await flush();
    findText(WB._modal.box, '修改后采纳').fire('click');
    await flush();
    assert(hasText(WB._modal.box, '修订后的根因'), 'H1: 修改表单打开');
    // 表单默认带出原假设作为底稿
    assert(WB._modal.inputs[0].value === '电池片隐裂', 'H2: 以原假设为底稿（可改）');
    WB._modal.inputs[0].value = '焊接虚焊（现场确认）';
    WB._modal.submitBtn.fire('click');
    await flush(); await flush();
    const mod = sent[sent.length - 1];
    assert(mod.dec === 'modify', 'H3: 提交 decision=modify');
    assert(mod.opts.modifiedCause === '焊接虚焊（现场确认）', 'H4: 提交修订后文本');
    assert(mod.opts.hypothesisIndex === 0, 'H5: 记录被修改的原假设下标');
  }

  // ---- I. 待复检异常：复检通过后不再要求倒回本节点补勾（2026-10-15 反馈）----
  // 后端在「复检通过」时自动置 capa_verified（L3 门禁本来就要求 verified=true
  // 必须先有通过的复检记录，该标记不是独立判断），界面因此删掉「补勾整改措施
  // 已验证」按钮，直接展示已验证状态，并保留「确认关闭异常」这一签署动作。
  {
    const recheckInc = {
      incident_id: INC_ID, batch_id: BATCH_ID, status: 'pending_recheck',
      trigger_type: 'rule_fail', severity: 'major', owner: 'ZHL-QA',
      confirmed_root_cause: '电池片隐裂', corrective_action: '更换电池片',
      containment_action: '停线隔离该批',
      capa_verified: true, verification_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    };
    WB.state.selected = batch; WB.state.batchId = BATCH_ID;
    WB.state.incidents = [recheckInc]; WB.state.traces = [];
    WB.api.listIncidents = () => Promise.resolve({ ok: true, data: [recheckInc] });
    const panel = WB.stages.incident.render(batch, {});
    const kv = (p) => p.querySelectorAll('.k').map(function (el) { return el.textContent; });
    assert(!findText(panel, '补勾整改措施已验证'), 'I1: 不再出现「补勾整改措施已验证」按钮');
    // 关闭动作前移到复检节点（2026-10-15 反馈：链路只往后推，不倒回上游）
    assert(!findText(panel, '确认关闭异常'), 'I2: 关闭动作不再留在异常处置节点');
    // 查 kv 行而不是整页文本：按钮文案里也含「已验证」，整页查找会假过
    assert(kv(panel).indexOf('整改措施已验证') >= 0, 'I3: 复检通过后卡片直接显示已验证状态');
    // 临时措施是关闭条件之一，卡片要能看见它有没有填——否则用户只看到一句
    // 「未填报临时措施」却不知道这项该长什么样、有没有值
    assert(kv(panel).indexOf('临时措施') >= 0, 'I3b: 卡片展示临时措施（关闭条件可见）');

    // 尚未复检（无复检记录、未验证）→ 仍指向复检节点，且不显示已勾状态
    const todoInc = {
      incident_id: INC_ID, batch_id: BATCH_ID, status: 'pending_recheck',
      trigger_type: 'rule_fail', severity: 'major', owner: 'ZHL-QA',
      capa_verified: false, verification_id: null,
    };
    WB.state.incidents = [todoInc];
    WB.api.listIncidents = () => Promise.resolve({ ok: true, data: [todoInc] });
    const panel2 = WB.stages.incident.render(batch, {});
    assert(!findText(panel2, '补勾整改措施已验证'), 'I4: 未复检也不给补勾入口');
    assert(hasText(panel2, '复检节点'), 'I5: 未复检时提示去复检节点录入结果');
    assert(kv(panel2).indexOf('整改措施已验证') < 0, 'I6: 未复检不显示已验证状态');
  }

  // ---- J. 关闭动作前移到复检节点（2026-10-15 反馈：链路只往后推）----
  // 「确认关闭异常」从异常处置节点搬到复检节点：操作员刚录完复检就在原地关闭，
  // 不必倒回上一步。可达性前提：异常进入 pending_recheck 的唯一路径
  // （POST /capa）同事务把批次也置 pending_recheck，因此复检面板必然渲染该卡片。
  {
    const rb = { batch_id: BATCH_ID, batch_no: 'B-5', status: 'pending_recheck',
      inspections: [{ sn: 'SN-N-057', inspection_id: INC_ID }] };
    const noRe = {
      incident_id: INC_ID, batch_id: BATCH_ID, status: 'pending_recheck',
      trigger_type: 'rule_fail', severity: 'major', owner: 'ZHL-QA',
      capa_verified: false, verification_id: null,
    };
    WB.state.selected = rb; WB.state.batchId = BATCH_ID;
    WB.state.incidents = [noRe]; WB.state.traces = [];
    WB.api.listIncidents = () => Promise.resolve({ ok: true, data: [noRe] });
    const p1 = WB.stages.reinspection.render(rb, {});
    assert(!!findText(p1, '录入复检'), 'J1: 复检节点保留「录入复检」');
    assert(!findText(p1, '确认关闭异常'), 'J2: 未录复检时不提前给关闭入口');

    // 已录复检（verification_id 回填）→ 原地出现「确认关闭异常」
    const withRe = Object.assign({}, noRe, {
      capa_verified: true, verification_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    });
    WB.state.incidents = [withRe];
    WB.api.listIncidents = () => Promise.resolve({ ok: true, data: [withRe] });
    const p2 = WB.stages.reinspection.render(rb, {});
    const closeBtn = findText(p2, '确认关闭异常');
    assert(!!closeBtn, 'J3: 已录复检 → 复检节点原地出现「确认关闭异常」');
    assert(hasText(p2, '整改措施已验证'), 'J3b: 签署处同屏可见「整改措施已验证」状态');

    // 点击即真的发起关闭请求（点得动、发得出）
    let closedId = null;
    WB.api.closeIncident = function (id) {
      closedId = id;
      return Promise.resolve({ ok: true, data: { batch_status: 'pending_release' } });
    };
    closeBtn.fire('click');
    await flush(); await flush();
    assert(closedId === INC_ID, 'J4: 点击后以该异常 id 调关闭接口');

    // 关闭条件不满足 → 逐条列出缺失项（沿用既有 409 映射，不被搬动弄丢）
    const miss = Object.assign({}, withRe);
    WB.state.incidents = [miss];
    WB.api.listIncidents = () => Promise.resolve({ ok: true, data: [miss] });
    const p3 = WB.stages.reinspection.render(rb, {});
    WB.api.closeIncident = function () {
      return Promise.resolve({ ok: false, error: { key: 'close_conditions_unmet',
        detail: { missing: ['owner', 'capa_fields'] } } });
    };
    findText(p3, '确认关闭异常').fire('click');
    await flush(); await flush();
    assert(hasText(p3, '关闭条件未满足'), 'J5: 409 缺失条件在复检卡片内逐条呈现');
  }

  // ---- K. 复检节点补填整改措施（2026-10-15 反馈：临时措施漏填后关不掉）----
  // 指派端点只接受 isolated，close 却硬要求 containment_action 非空 —— 指派时
  // 漏填就没有任何入口，异常永远关不掉。补填入口放在当前节点（复检卡片），
  // 且不得把自动置位的 verified 洗掉。
  {
    const rb2 = { batch_id: BATCH_ID, batch_no: 'B-5', status: 'pending_recheck',
      inspections: [{ sn: 'SN-N-057', inspection_id: INC_ID }] };
    const inc = {
      incident_id: INC_ID, batch_id: BATCH_ID, status: 'pending_recheck',
      trigger_type: 'rule_fail', severity: 'major', owner: 'ZHL-QA',
      confirmed_root_cause: '电池片隐裂', corrective_action: '更换电池片',
      containment_action: null,               // ← 指派时漏填（用户截图现场）
      capa_verified: true, verification_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    };
    WB.state.selected = rb2; WB.state.batchId = BATCH_ID;
    WB.state.incidents = [inc]; WB.state.traces = [];
    WB.api.listIncidents = () => Promise.resolve({ ok: true, data: [inc] });
    const panel = WB.stages.reinspection.render(rb2, {});
    const fillBtn = findText(panel, '补填整改措施');
    assert(!!fillBtn, 'K1: 复检卡片提供「补填整改措施」入口');

    let sent = null;
    WB.api.capaIncident = function (id, body) {
      sent = { id: id, body: body };
      return Promise.resolve({ ok: true, data: {} });
    };
    fillBtn.fire('click');
    assert(!!WB._modal, 'K2: 点击打开措施表单');
    assert(hasText(WB._modal.box, '临时措施'), 'K3: 表单含「临时措施」字段');
    // 三个必填：确认根因 / 纠正措施 / 临时措施
    WB._modal.inputs[0].value = '电池片隐裂';
    WB._modal.inputs[1].value = '更换电池片';
    WB._modal.inputs[2].value = '停线隔离该批';
    WB._modal.submitBtn.fire('click');
    await flush(); await flush();
    assert(sent && sent.id === INC_ID, 'K4: 提交到该异常的整改措施接口');
    assert(sent.body.containment_action === '停线隔离该批', 'K5: 提交临时措施');
    assert(!('verified' in sent.body), 'K6: 不带 verified → 后台保持已自动置位的标记');
    assert(WB._modal === null, 'K7: 提交成功后关闭表单');

    // 已在待复检态：不显示「已验证」勾选框（该标记由复检通过自动置位，
    // 摆一个勾在这里等于把刚删掉的手续又搬回来）
    const panel2 = WB.stages.reinspection.render(rb2, {});
    findText(panel2, '补填整改措施').fire('click');
    assert(!hasText(WB._modal.box, '已验证'), 'K8: 待复检态表单不再出现「已验证」勾选');
    WB.closeModal();
  }

  // ---- L. 视图切换与左列标签高亮同步（2026-10-15 反馈）----
  // 异常列表里点一条 → 主区回到批次视图，但顶部标签仍高亮「异常」。原因：高亮
  // 只由标签自己的点击处理器维护，任何**程序性**切换（_locateBatch 等）都不碰它。
  // 高亮因此归 switchView 统一维护（视图状态只有一个来源）。
  {
    const onTab = function () {
      return leftTabs.filter(function (b) { return b.classList.contains('on'); })
        .map(function (b) { return b.dataset.view; });
    };
    const tabOf = function (v) {
      return leftTabs.filter(function (b) { return b.dataset.view === v; })[0];
    };
    assert(leftTabs.length === 4, 'L1: 左列四个视图标签就位');

    // 用户路径复现：先点「异常」标签（真点击，高亮此时是对的）…
    WB.state.selected = null;
    tabOf('incidents').fire('click');
    assert(onTab().join() === 'incidents', 'L2: 点「异常」标签 → 高亮在异常');

    // …再从异常列表点一条 → 主区回批次，高亮必须跟着回（缺陷点：仍停在「异常」）
    WB.selectBatch = function () {};   // 只验视图/高亮同步，详情加载不在本用例范围
    WB._locateBatch({ batch_id: BATCH_ID });
    assert(WB.state.view === 'batch', 'L3: 点异常条目 → 视图切回批次');
    assert(onTab().join() === 'batch', 'L4: 标签高亮跟着切（原先停在「异常」）');

    // 程序性切换不止 _locateBatch 一处：switchView 自身就要同步高亮
    WB.switchView('rules');
    assert(onTab().join() === 'rules', 'L5: 任意程序性切换都同步高亮');
    assert(WB.state.view === 'rules', 'L6: 视图状态与高亮同源');
  }

  // ---- M. 回看节点不得改变节点线本身（2026-10-15 反馈：切节点整条线都变）----
  // 成因：返回按钮与两条 .line 被追加进 .pipe（display:flex；.stage{flex:1}），
  // 多出来的 flex 子项把六个节点挤窄。回看与当前态，线的几何必须一致——回看
  // 只能改变「被查看节点的标记」与「多一个返回入口」。
  {
    // 返回节点自身可能就是 .pipe（旧实现直接返回 pipe），也可能是包裹层——
    // 断言只锁不变量，不锁包裹层是否存在
    const pipeOf = function (node) {
      if (node.classList && node.classList.contains('pipe')) return node;
      return node.querySelectorAll('.pipe')[0];
    };
    const d = { status: 'released' };           // 当前节点 = 报告（index 5）
    WB.state.viewStage = null;
    const w1 = WB._pipeline(d);
    const p1 = pipeOf(w1);
    assert(!!p1, 'M0: 渲染出节点线容器');
    assert(p1.querySelectorAll('.stage').length === 6, 'M1: 六个节点');
    assert(p1.children.length === 6, 'M2: .pipe 内只有六个节点（无额外 flex 子项）');
    assert(w1.querySelectorAll('.line').length === 0, 'M3: 不再注入会改变线形的 .line');

    WB.state.viewStage = 3;                     // 回看「复检」
    const w2 = WB._pipeline(d);
    const p2 = pipeOf(w2);
    assert(p2.children.length === 6, 'M4: 回看时 .pipe 结构不变（仍六个节点）');
    assert(w2.querySelectorAll('.line').length === 0,
      'M4b: 回看不注入 .line（线形与当前态一致，不再"整条线变了"）');
    assert(p2.querySelectorAll('.stage')[3].classList.contains('review'), 'M5: 被回看节点标记 review');
    assert(p2.querySelectorAll('.stage')[0].classList.contains('done'),
      'M6: 回看不清除实际完成的 ✓（与回看位置解耦）');
    const ret = findText(w2, '返回当前节点');
    assert(!!ret, 'M7: 回看时提供「返回当前节点」');
    assert(!p2.children.some(function (c) { return c.classList.contains('actions'); }),
      'M8: 返回按钮所在行不在 .pipe 内（不参与节点均分）');
    ret.fire('click');
    assert(WB.state.viewStage === null, 'M9: 点击返回 → 回到当前节点');
  }

  console.log(failed === 0 ? '\nALL PASS' : '\n' + failed + ' FAILED');
  process.exit(failed === 0 ? 0 : 1);
})().catch(function (e) { console.error('FATAL:', e); process.exit(1); });
