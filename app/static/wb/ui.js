/* WB.ui — DOM 构建 / toast / modal / drawer（spec §6.2 原生 JS，全局命名空间 WB，沿用 ZHL 模式） */
'use strict';

/* 首个脚本创建全局 WB。用 window 上的对象属性（而非顶层 const/let）跨脚本共享：
   后续 api.js / main.js 直接引用 WB。多文件同页下若各写 `const WB` 会在共享
   全局词法环境里「重复声明」直接 SyntaxError，故命名空间挂在 window.WB 上。 */
var WB = (typeof window !== 'undefined' ? (window.WB = window.WB || {}) : (globalThis.WB = globalThis.WB || {}));

/* ---- h()：轻量 DOM 构建（attrs 支持 class/text/事件回调/其他属性，kids 支持数组嵌套） */
WB.h = function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      /* innerHTML 不是反射属性：走 setAttribute('innerHTML',…) 只会挂一个惰性
         innerhtml 特性、元素内容仍为空，必须直接赋 IDL 属性才解析 HTML */
      else if (k === 'innerHTML') e.innerHTML = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(e.dataset, v);
      else if (k === 'disabled' || k === 'checked' || k === 'multiple') e[k] = !!v;
      else e.setAttribute(k, v);
    }
  }
  const add = (c) => {
    if (c == null) return;
    if (Array.isArray(c)) { c.forEach(add); return; }
    e.append(c.nodeType ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  return e;
};
WB.el = (id) => document.getElementById(id);

/* ---- toast（禁用原生 alert，一律 toast）---- */
WB.toast = function (msg) {
  const t = WB.el('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('on'), 2600);
};

/* ---- modal（fields: {label, type:'text'|'select'|'textarea'|'checkbox', options?, value?, placeholder?}）
   onOk(values) 由调用方决定异步写操作；values 与 fields 顺序对齐。 */
WB._modal = null;
WB.openModal = function (title, fields, onOk, opts = {}) {
  WB.closeModal();
  const ov = WB.h('div', { class: 'mask on' }, null);
  const box = WB.h('div', { class: 'modal on' });
  box.append(WB.h('h3', { text: title }));
  const inputs = [];
  for (const f of fields) {
    let inp;
    if (f.type === 'select') {
      inp = WB.h('select');
      (f.options || []).forEach((o) => {
        if (typeof o === 'object' && o !== null) {
          inp.append(WB.h('option', { value: o.value, text: o.label, ...(o.disabled ? { disabled: true } : {}) }));
        } else {
          inp.append(WB.h('option', { value: o, text: o }));
        }
      });
      if (f.value != null) inp.value = f.value;
    } else if (f.type === 'textarea') {
      inp = WB.h('textarea', { rows: f.rows || 3, placeholder: f.placeholder || '' });
      inp.value = f.value || '';
    } else if (f.type === 'checkbox') {
      inp = WB.h('input', { type: 'checkbox', ...(f.value ? { checked: true } : {}) });
    } else if (f.type === 'datetime-local') {
      inp = WB.h('input', { type: 'datetime-local' });
      inp.value = f.value || '';
    } else {
      inp = WB.h('input', { type: f.type || 'text', placeholder: f.placeholder || '', ...(f.min != null ? { min: f.min } : {}) });
      inp.value = f.value || '';
    }
    if (f.disabled) inp.disabled = true;
    inputs.push(inp);
    const row = WB.h('div', { class: 'form-row' },
      WB.h('label', { text: f.label + (f.required ? ' *' : '') }), inp);
    if (f.hint) row.append(WB.h('div', { class: 'hint-line', text: f.hint }));
    box.append(row);
  }
  if (opts.inline) {
    if (typeof opts.inline === 'string') opts.inline = document.querySelector(opts.inline);
    if (opts.inline) box.append(opts.inline);
  }
  const okBtn = WB.h('button', { class: 'primary', onclick: () => {
    const vals = inputs.map((i) => i.type === 'checkbox' ? i.checked : i.value);
    // onOk 返回 false = 行内校验未过：保留输入不关窗（其余视为放行，兼容既有回调）
    if (onOk(vals, inputs) === false) return;
    WB.closeModal();
  }, text: opts.submitLabel || '确定' });
  // extraButtons：三选一等场景的额外动作（如归因的「修改后采纳 / 驳回」）。
  // 默认点击后关窗；回调返回 false 表示"别关"——要么校验没过要保留输入，
  // 要么它已经自己打开了下一个模态框（如「修改后采纳」→ 修改表单），此时
  // 再 closeModal 会把刚开的那个关掉。
  const foot = WB.h('div', { class: 'foot' },
    WB.h('button', { onclick: () => WB.closeModal(), text: '取消' }),
    okBtn,
  );
  if (Array.isArray(opts.extraButtons)) {
    opts.extraButtons.forEach((b) => {
      foot.insertBefore(
        WB.h('button', { class: b.cls || '', text: b.label, onclick: () => {
          if (b.onclick() === false) return;
          WB.closeModal();
        } }),
        okBtn,
      );
    });
  }
  box.append(foot);
  document.body.append(ov, box);
  ov.addEventListener('click', (ev) => { if (ev.target === ov) WB.closeModal(); });
  WB._modal = { ov, box, inputs, submitBtn: okBtn };
  return WB._modal;
};
WB.closeModal = function () {
  if (WB._modal) {
    WB._modal.ov.remove();
    WB._modal.box.remove();
    WB._modal = null;
  }
};

/* ---- drawer（T9 报告查看复用；body 接受 HTML 节点或字符串）---- */
WB.openDrawer = function (title, body) {
  WB.el('drawerTitle').textContent = title;
  const bd = WB.el('drawerBody');
  bd.replaceChildren();
  if (body && body.nodeType) bd.append(body);
  else if (typeof body === 'string') bd.append(WB.h('div', { text: body }));
  WB.el('drawer').classList.add('open');
  WB.el('mask').classList.add('on');
};
WB.closeDrawer = function () {
  WB.el('drawer').classList.remove('open');
  WB.el('mask').classList.remove('on');
};

/* ---- 状态中文标签（spec §2.1 / §5 术语表；批次 10 态 + 异常 7 态 + 其他常用枚举）---- */
WB.ST_CN = {
  // 批次 10 态
  'draft': '草稿',
  'pending_intake': '待入场',
  'pending_judge': '待判定',
  'pending_release': '待放行',
  'isolated': '已隔离',
  'disposing': '处置中',
  'pending_recheck': '待复检',
  'rejected': '已拒绝',
  'released': '已放行',
  'archived': '已归档',
  // 异常 7 态
  'new': '新建',
  'assigned': '已指派',
  'closed': '已关闭',
  'scrapped': '已报废',
  // 抽样/判定结论
  'pass': '合格',
  'fail': '不合格',
  'rule_missing': '规则缺失',
  'pending': '待处理',
};
WB.chip = function (status, extraClass) {
  const c = WB.h('span', { class: 'chip st-' + status + (extraClass ? ' ' + extraClass : '') });
  c.textContent = WB.ST_CN[status] || status;
  return c;
};
WB.ruleName = function (ruleVersionId) {
  const items = (WB.state.ruleVersions && WB.state.ruleVersions.items) || [];
  const r = items.find((x) => x.rule_version_id === ruleVersionId);
  return r ? (r.name + ' v' + r.version_no + ' [' + (WB.RULE_ST_CN[r.status] || r.status) + ']') : null;
};
WB.RULE_ST_CN = { published: '已发布', draft: '草稿', disabled: '已停用' };

/* ---- 流水线节点名（spec §1.2 六节点）---- */
WB.STAGES = ['数据入场', '判定', '异常处置', '复检', '放行', '报告'];
WB.stageOfStatus = function (status) {
  return {
    'pending_intake': 0,
    'pending_judge': 1,
    'isolated': 2,
    'disposing': 2,
    'pending_recheck': 3,
    'pending_release': 4,
    'released': 5,
    'rejected': 5,
    'archived': 5,
    'draft': 0,
  }[status] ?? 0;
};

/* ---- 通用时间格式化 ---- */
WB.fmtTime = function (iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const p = (n) => String(n).padStart(2, '0');
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  } catch (_) {
    return String(iso);
  }
};

WB._init = function () {
  WB.el('drawerClose').addEventListener('click', WB.closeDrawer);
  WB.el('mask').addEventListener('click', (e) => {
    if (e.target === WB.el('mask')) {
      if (WB._modal) WB.closeModal();
      else if (WB.el('drawer').classList.contains('open')) WB.closeDrawer();
    }
  });
};

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', WB._init);
  else WB._init();
}
