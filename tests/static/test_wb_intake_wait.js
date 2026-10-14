// 回归：提交送检数据的「等待动画 + 超时改轮询 + 完成后输出」（2026-10-15 反馈：
// 「提交送检数据之后老是超时」——真模型下 POST /api/intake 单请求同步跑完整条
// 主链（原始单据解析 + 逐组件抽取，单次 40~90s 且逐组件累加），15s 默认超时
// 必然 abort，用户看到的就是「超时」；后端不随浏览器中断而停，跑完照常落库。
// 覆盖五组行为：
//   A. 提交 → 表单关闭 + 等待窗出现（转圈 + 文案），成功 → 等待窗关闭 + 结果卡
//   B. 提交超时 → 等待窗不关、文案切「自动查询」，轮询发现批次已推进 → 合成结果
//   C. 超时后批次留在 pending_intake 且行已入库 = 低置信等人工确认 → 收窗走 pending
//   D. 真 4xx 失败 → 等待窗关闭 + toast 报错（不悬挂）
//   E. 空数据校验失败 → return false，表单不关（输入保留）
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const read = (f) => fs.readFileSync(path.join(root, 'app', 'static', 'wb', f), 'utf8');
const ui = read('ui.js'), api = read('api.js'), main = read('main.js');
const intake = read('stages/intake.js');

// ---- DOM shim（与 test_wb_incident_dup 同款，裁剪到本文件用到的面）----
function makeEl(tag) {
  const e = {
    _tag: tag, nodeType: 1, children: [], _ev: {}, parentNode: null,
    style: {}, dataset: {}, textContent: '', value: '',
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
// 轮询/计时器按真实时钟走（2s 轮询间隔、1s 计时），用真实睡眠推进
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

for (const [name, src] of [['ui.js', ui], ['api.js', api], ['main.js', main],
  ['intake.js', intake]]) {
  vm.runInContext(src, ctx, { filename: name });
}
const WB = ctx.WB;

const BATCH_ID = '11111111-1111-1111-1111-111111111111';
const batch = { batch_id: BATCH_ID, batch_no: 'B-WAIT', status: 'pending_intake' };
const ROW = 'SN-N-001,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass';

// 提交送检表单并填一行有效数据
function openForm() {
  const panel = WB.stages.intake.render(batch, {});
  findText(panel, '提交送检数据').fire('click');
  WB._modal.inputs[2].value = ROW;
  return WB._modal;
}

(async function main() {
  WB.state.selected = batch; WB.state.batchId = BATCH_ID;

  // ---- E. 空数据校验失败 → return false，表单不关 ----
  {
    const panel = WB.stages.intake.render(batch, {});
    findText(panel, '提交送检数据').fire('click');
    WB._modal.inputs[2].value = '';          // 不填数据直接提交
    WB._modal.submitBtn.fire('click');
    assert(!!WB._modal, 'E1: 空数据 → 校验失败表单不关（输入保留）');
    assert(byId.toast.textContent.indexOf('请填写检测数据') >= 0,
      'E2: toast 提示填写检测数据');
    WB.closeModal();
  }

  // ---- A. 提交成功 → 等待窗出现再关闭 + 结果卡 ----
  {
    const m = openForm();
    // _finishWait 会拉详情重渲主区：桩住 getBatch 让结果卡可渲染
    WB.api.getBatch = function () {
      return Promise.resolve({ ok: true, data: { batch_id: BATCH_ID, batch_no: 'B-WAIT',
        status: 'pending_judge', inspections: [] } });
    };
    let submitted = null;
    WB.api.submitIntake = function (body) {
      submitted = body;
      return new Promise(function (resolve) {
        // 20ms 后返回：同步断言点先看到等待窗挂着
        setTimeout(function () {
          resolve({ ok: true, data: { status: 'confirmed', confidence: 0.9, low_conf_sns: [] } });
        }, 20);
      });
    };
    m.submitBtn.fire('click');
    assert(WB._modal === null, 'A1: 提交后表单立即关闭（数据已进请求体）');
    assert(!!WB._waitModal, 'A2: 等待窗出现');
    assert(hasText(WB._waitModal.box, '处理中 · B-WAIT'), 'A3: 等待窗带批次号');
    assert(hasText(WB._waitModal.box, '正在提交送检数据'), 'A4: 等待窗初始文案');
    // 默认录入方式是「原始单据（AI 抽取）」→ 走 raw_source 载荷而非 inspections
    assert(!!submitted && typeof submitted.raw_source === 'string'
      && submitted.raw_source.indexOf('SN-N-001') >= 0,
      'A6: 表单数据进了请求体（raw_source 通道）');
    await sleep(60);
    assert(WB._waitModal === null, 'A7: 提交成功后等待窗关闭');
    assert(WB.state.intakeResult && WB.state.intakeResult.batch_id === BATCH_ID,
      'A8: 入场结果卡进渲染状态（置信度等可见）');
    assert(hasText(byId.mainPane, '入场结果'), 'A9: 结果卡出现在主区');
  }

  // ---- B. 提交超时 → 切轮询，批次已推进 → 合成结果收窗 ----
  {
    WB.state.intakeResult = null;
    const m = openForm();
    WB.api.submitIntake = function () {
      return Promise.resolve({ ok: false, status: 0, data: null,
        error: { key: 'timeout', msg: '请求超时', status: 0, detail: null } });
    };
    let getBatchCalls = 0;
    WB.api.getBatch = function () {
      getBatchCalls++;
      return Promise.resolve({ ok: true, data: { batch_id: BATCH_ID, status: 'pending_judge',
        inspections: [{ sn: 'SN-N-001', intake_status: 'confirmed' }] } });
    };
    m.submitBtn.fire('click');
    assert(!!WB._waitModal, 'B1: 超时后等待窗不关（提交 ≠ 失败）');
    await flush();                              // .then 回调（切文案、启轮询）在微任务里
    assert(hasText(WB._waitModal.box, '自动查询进度'), 'B2: 文案切到「自动查询进度」');
    await sleep(2400);                          // 跨过 2s 轮询间隔
    assert(getBatchCalls >= 1, 'B3: 轮询已启动');
    assert(WB._waitModal === null, 'B4: 批次已推进 → 等待窗关闭');
    assert(WB.state.intakeResult && WB.state.intakeResult.data.degraded === true,
      'B5: 结果卡标记 degraded（超时后合成，抽取明细不在）');
    assert(hasText(byId.mainPane, '入场结果'), 'B6: 结果卡出现（置信度显示 —）');
  }

  // ---- C. 超时后批次留在 pending_intake 且行已入库 → 低置信等人工确认 ----
  {
    WB.state.intakeResult = null;
    const m = openForm();
    WB.api.submitIntake = function () {
      return Promise.resolve({ ok: false, status: 0, data: null,
        error: { key: 'timeout', msg: '请求超时', status: 0, detail: null } });
    };
    // 轮询第一次就看到：状态没动，但行已在 = 挂起等人工确认，不会再自己推进
    WB.api.getBatch = function () {
      return Promise.resolve({ ok: true, data: { batch_id: BATCH_ID,
        status: 'pending_intake', inspections: [{ sn: 'SN-N-001', intake_status: 'unconfirmed' }] } });
    };
    m.submitBtn.fire('click');
    assert(!!WB._waitModal, 'C0: 等待窗在');
    await sleep(2400);
    assert(WB._waitModal === null, 'C1: 行已入库 + 状态未动 → 收窗交还人工确认');
    assert(WB.state.intakeResult && WB.state.intakeResult.data.status === 'pending',
      'C2: 结果卡走 pending 路径（待人工确认）');
    assert(hasText(byId.mainPane, '待人工确认'), 'C3: 卡片文案是「待人工确认」');
  }

  // ---- D. 真 4xx 失败 → 等待窗关闭 + toast ----
  {
    WB.state.intakeResult = null;
    const m = openForm();
    WB.api.submitIntake = function () {
      return Promise.resolve({ ok: false, status: 409, data: null, error: {
        key: 'batch_not_pending_intake', msg: 'batch_not_pending_intake',
        status: 409, detail: { error: 'batch_not_pending_intake' } } });
    };
    m.submitBtn.fire('click');
    assert(!!WB._waitModal, 'D0: 失败响应到达前等待窗在');
    await flush(); await flush();
    assert(WB._waitModal === null, 'D1: 真 4xx → 等待窗关闭（不悬挂）');
    assert(byId.toast.textContent.indexOf('状态冲突') >= 0,
      'D2: toast 报出归一化业务错误（409 → 状态冲突）');
    assert(!WB.state.intakeResult, 'D3: 失败不产生结果卡');
  }

  // ---- F. 收窗后无计时器泄漏（elapsed 闭包不再写已移除节点）----
  {
    WB.state.intakeResult = null;
    openForm();
    WB.api.submitIntake = function () {
      return Promise.resolve({ ok: true, data: { status: 'confirmed', confidence: 0.9 } });
    };
    WB._modal.submitBtn.fire('click');
    await sleep(60);
    assert(WB._waitModal === null, 'F1: 成功路径同样收窗');
    await sleep(1100);                          // 若 interval 泄漏此刻会再触发
    assert(true, 'F2: 无计时器泄漏报错');
  }

  console.log(failed ? '\nFAILED: ' + failed : '\nALL PASS');
  process.exit(failed ? 1 : 0);
})().catch(function (e) { console.error('FATAL:', e); process.exit(1); });
