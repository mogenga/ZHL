/* WB.main — 视图状态 + 渲染调度 + URL 参数读写（T2：接真实数据，移除 mock）
   状态→界面映射由纯函数驱动（spec §2）；数据只来自 spec §3 事实表列出的端点。 */
'use strict';

WB.state = {
  view: 'batch',       // batch | rules | incidents | reports
  batchId: null,       // 当前选中批次（URL ?batch=<id>）
  batches: [],         // 批次列表（左列批次视图）
  batchesLoading: false,
  selected: null,      // 选中批次详情（getBatch 结果）
  detailLoading: false,
  ruleVersions: null,  // 规则版本列表（{items, available, reason}）
  incidents: [],       // 全量异常（左列异常视图）
  reports: [],         // 报告列表（左列报告视图，T9 接入）
  traces: [],          // 右栏轨迹（当前批次）
  viewStage: null,     // 流水线回看节点（null = 当前节点）
};

/* ---- URL 参数读写（?batch=<id>，刷新恢复选中批次）---- */
WB._url = function () { return new URL(location.href); };
WB.setBatch = function (id) {
  WB.state.batchId = id || null;
  const u = WB._url();
  if (id) u.searchParams.set('batch', id); else u.searchParams.delete('batch');
  history.replaceState(null, '', u.pathname + u.search);
  WB._onSelectedChanged();
};
WB._readUrl = function () {
  const u = WB._url();
  WB.state.batchId = u.searchParams.get('batch');
};

/* ---- 视图切换（左列四标签）---- */
WB.switchView = function (name) {
  WB.state.view = name;
  WB.renderLeft();
  if (name !== 'batch') WB.state._search = '';
  WB._renderCenterAndRight();
};
WB._bindLeftTabs = function () {
  document.querySelectorAll('[data-view]').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('[data-view]').forEach((x) => x.classList.toggle('on', x === b));
      WB.switchView(b.dataset.view);
    });
  });
  const nb = WB.el('newBatchBtn');
  if (nb) nb.addEventListener('click', WB._onNewBatch);
};

/* ---- 节点面板调度：WB.stages._byIndex[stageIndex] → 面板 DOM（T4+ 各阶段模块自注册）---- */
WB.stages = { _byIndex: {} };
WB._currentPanel = function (detail) {
  if (!detail) return null;
  const idx = WB.state.viewStage == null ? WB.stageOfStatus(detail.status) : WB.state.viewStage;
  const p = WB.stages._byIndex[idx];
  return p ? p.render(detail, WB._listItem()) : null;
};

/* ---- 中心：批次头 + 六节点流水线 + 当前节点面板 ---- */
WB.renderCenter = function (detail, listitem, panelDom) {
  const main = WB.el('mainPane');
  main.replaceChildren();
  if (WB.state.detailLoading) {
    main.append(WB.h('div', { class: 'crumb', text: '工作台 / 批次' }));
    main.append(WB.h('div', { class: 'card empty', text: '正在加载批次…' }));
    return;
  }
  if (!detail) {
    main.append(WB.h('div', { class: 'crumb', text: '工作台 / 批次' }));
    main.append(WB.h('div', { class: 'card empty', text: '从左侧选择批次开始工作' }));
    return;
  }
  main.append(WB.h('div', { class: 'crumb', text: '工作台 / 批次 / ' + detail.batch_no }));
  main.append(WB.batchHead(detail, listitem));
  main.append(panelDom || WB.h('div', { class: 'card empty', text: '该节点无可操作项' }));
};

/* ---- 批次头（批次号/状态/项目/型号/计划数量/规则版本名；不显示实际数量，spec §3.3）---- */
WB.batchHead = function (detail, listitem) {
  const row = (k, v) => WB.h('div', {},
    WB.h('div', { class: 'k', text: k }),
    WB.h('div', { class: 'v', text: v == null ? '—' : String(v) }));
  const ruleName = WB.ruleName(detail.rule_version_id);
  return WB.h('div', { class: 'card' },
    WB.h('div', { class: 'bhead' },
      WB.h('h2', { text: detail.batch_no }),
      WB.chip(detail.status),
      WB.h('span', { class: 'muted', text: (listitem ? listitem.project_id : '') + ' · ' + (listitem ? listitem.model : '') })),
    WB.h('div', { class: 'kv' },
      row('计划数量', detail.planned_quantity),
      row('规则版本', ruleName || '规则版本信息暂不可用'),
      row('流程位置', WB.STAGES[WB.stageOfStatus(detail.status)])),
    WB._pipeline(detail));
};

WB._pipeline = function (detail) {
  const cs = WB.stageOfStatus(detail.status);
  const view = WB.state.viewStage == null ? cs : WB.state.viewStage;
  const locked = view !== cs;
  const btns = WB.STAGES.map((s, i) => {
    const cls = 'stage' + (i < view ? ' done' : '') + (i === view ? ' cur' : '');
    const disabled = i > cs || detail.status === 'archived';
    return WB.h('button', {
      class: cls,
      ...(disabled ? { disabled: true } : {}),
      onclick: () => { WB.state.viewStage = i; WB._renderCenterAndRight(); },
    },
      WB.h('div', { class: 'dot', text: i < view ? '✓' : String(i + 1) }),
      WB.h('div', { class: 'lbl', text: s }));
  });
  const wrap = WB.h('div', { class: 'pipe' }, btns);
  if (locked) {
    wrap.append(WB.h('div', { class: 'actions' },
      WB.h('button', { onclick: () => { WB.state.viewStage = null; WB._renderCenterAndRight(); }, text: '返回当前节点' })));
  }
  return wrap;
};
WB._listItem = function () {
  return WB.state.batches.find((b) => b.batch_id === WB.state.batchId) || null;
};

/* ---- 右栏：轨迹 + 关键指标（T3 起接真实 traces；指标仅用真实字段，spec §3.3）---- */
WB.renderRight = function (detail) {
  const rp = WB.el('rightPane');
  rp.replaceChildren();
  rp.append(WB.h('div', { style: 'font-weight:700', text: '批次轨迹' }));
  rp.append(WB.h('div', { style: 'font-size:12px;color:var(--ink3)', text: '每一步操作留痕' }));
  rp.append(WB.h('ul', { class: 'tl' }, WB.renderTimeline(WB.state.traces || [])));
  rp.append(WB.h('div', { style: 'font-weight:700;margin-top:8px', text: '关键指标' }));
  const inspections = (detail && detail.inspections) || [];
  const passCnt = inspections.filter((i) => i.decision_status === 'pass').length;
  const failCnt = inspections.filter((i) => i.decision_status === 'fail').length;
  const kpiBox = (n, label) => WB.h('div', { class: 'box' },
    WB.h('div', { class: 'n', text: n }),
    WB.h('div', { class: 'l', text: label }));
  rp.append(WB.h('div', { class: 'mini-kpi' },
    kpiBox(String(passCnt), '判定合格'),
    kpiBox(String(failCnt), '判定不合格'),
    kpiBox(detail ? WB.ST_CN[detail.status] : '—', '当前状态'),
    kpiBox(detail ? String(detail.planned_quantity) : '—', '计划数量')));
};

/* ---- 轨迹时间线（右栏；skill_name 中文名 + 时间 + 结果着色，spec §1.3）---- */
WB.SKILL_CN = {
  intake: '入场',
  extract: '数据抽取',
  judge: '判定',
  sample: '抽样',
  incident_isolate: '异常处置',
  incident_attribute: '归因',
  incident_assign: '指派',
  incident_capa: '整改措施',
  reinspection: '复检',
  release: '放行',
  reject: '拒绝',
  report_api: '报告',
};
WB.renderTimeline = function (traces) {
  if (!traces.length) return [WB.h('li', {}, WB.h('div', { class: 't', text: '暂无轨迹' }))];
  const sorted = [...traces].sort((a, b) => new Date(b.started_at || 0) - new Date(a.started_at || 0));
  return sorted.map((t) => {
    const ok = t.validation_result === 'pass' ? 'tag-pass' : (t.validation_result === 'fail' ? 'tag-fail' : 'tag-warn');
    const res = t.validation_result === 'pass' ? '通过' : (t.validation_result === 'fail' ? '失败' : '部分');
    return WB.h('li', {},
      WB.h('div', { class: 't', text: WB.fmtTime(t.started_at) }),
      WB.h('div', { class: 'e' },
        WB.h('span', { text: (WB.SKILL_CN[t.skill_name] || t.skill_name || '环节') + ' · ' }),
        WB.h('span', { class: 'tag ' + ok, text: res })));
  });
};

/* ---- 左列：四视图 ---- */
WB.renderLeft = function () {
  const pane = WB.el('leftView');
  pane.replaceChildren();
  if (WB.state.view === 'batch') {
    if (WB.state.batchesLoading) { pane.append(WB.h('div', { class: 'empty', text: '加载中…' })); return; }
    if (WB.state.batchesError) {
      pane.append(WB.h('div', { class: 'empty', text: WB.errText(WB.state.batchesError) + '（点击重试）' },
        WB.h('button', { class: 'newbtn', style: 'margin-top:8px', onclick: WB.loadBatches, text: '重试' })));
      return;
    }
    pane.append(WB.h('input', { class: 'search', placeholder: '搜索批次号 / 项目 / 型号', oninput: WB._onSearch, value: WB.state._search || '' }));
    const list = WB.state.batches;
    const q = (WB.state._search || '').trim().toLowerCase();
    const match = (b) => !q || (b.batch_no + b.project_id + b.model).toLowerCase().includes(q);
    const active = list.filter((b) => match(b) && b.status !== 'archived');
    const done = list.filter((b) => match(b) && b.status === 'archived');
    const item = (b) => WB.h('button', {
      class: 'batch-item' + (b.batch_id === WB.state.batchId ? ' on' : ''),
      onclick: () => WB.selectBatch(b.batch_id),
    },
      WB.h('div', { class: 'no' }, WB.h('span', { text: b.batch_no }), WB.chip(b.status)),
      WB.h('div', { class: 'meta', text: b.project_id + ' · ' + b.model + ' · 计划 ' + b.planned_quantity }));
    pane.append(WB.h('div', { class: 'group-title', text: '进行中（' + active.length + '）' }));
    active.forEach((b) => pane.append(item(b)));
    pane.append(WB.h('div', { class: 'group-title', text: '已归档（' + done.length + '）' }));
    done.forEach((b) => pane.append(item(b)));
    if (!list.length) pane.append(WB.h('div', { class: 'empty', text: '暂无批次' }));
  } else if (WB.state.view === 'rules') {
    const rv = WB.state.ruleVersions;
    if (!rv) { pane.append(WB.h('div', { class: 'empty', text: '加载中…' })); return; }
    if (!rv.available) {
      pane.append(WB.h('div', { class: 'empty', text: rv.reason === 'network' ? '规则版本加载失败（网络异常）' : '规则版本数据暂不可用（请先初始化规则库）' }));
      return;
    }
    pane.append(WB.h('div', { class: 'group-title', text: '规则版本' }));
    (rv.items || []).forEach((r) => pane.append(WB.h('button', { class: 'batch-item', onclick: () => WB._ruleDrawer(r) },
      WB.h('div', { class: 'no' },
        WB.h('span', { text: r.name + ' v' + r.version_no }),
        WB.h('span', { class: 'chip st-' + (r.status === 'published' ? 'released' : 'draft'), text: WB.RULE_ST_CN[r.status] || r.status })),
      WB.h('div', { class: 'meta', text: r.project_id + ' · ' + r.model + ' · 生效 ' + WB.fmtTime(r.effective_at) }))));
    if (!(rv.items || []).length) pane.append(WB.h('div', { class: 'empty', text: '暂无规则版本' }));
  } else if (WB.state.view === 'incidents') {
    pane.append(WB.h('div', { class: 'group-title', text: '异常列表' }));
    (WB.state.incidents || []).forEach((i) => pane.append(WB.h('button', { class: 'batch-item', onclick: () => WB._locateBatch(i) },
      WB.h('div', { class: 'no' },
        WB.h('span', { text: i.incident_id ? i.incident_id.slice(0, 8) : i.incident_id }),
        WB.h('span', { class: 'chip inc-' + i.status, text: (WB.ST_CN[i.status] || i.status) })),
      WB.h('div', { class: 'meta', text: (i.batch_id ? i.batch_id.slice(0, 8) : '') + ' · ' + (WB.TRIG_CN[i.trigger_type] || i.trigger_type) + ' · ' + (WB.SEV_CN[i.severity] || i.severity) }))));
    if (!(WB.state.incidents || []).length) pane.append(WB.h('div', { class: 'empty', text: '暂无异常' }));
  } else {
    pane.append(WB.h('div', { class: 'group-title', text: '报告列表' }));
    (WB.state.reports || []).forEach((r) => pane.append(WB.h('button', { class: 'batch-item', onclick: () => WB._reportDrawer(r) },
      WB.h('div', { class: 'no' },
        WB.h('span', { text: (r.report_id || '').slice(0, 8) }),
        WB.h('span', { class: 'chip st-' + (r.report_type === 'incident' ? 'rejected' : 'released'), text: (WB.REP_CN || {})[r.report_type] || r.report_type })),
      WB.h('div', { class: 'meta', text: (r.batch_no || (r.batch_id || '').slice(0, 8)) + ' · v' + r.version + ' · ' + WB.fmtTime(r.generated_at) }))));
    if (!(WB.state.reports || []).length) pane.append(WB.h('div', { class: 'empty', text: '暂无报告' }));
  }
};
/* spec §5 术语对照表 */
WB.TRIG_CN = { rule_fail: '规则失败', sample_fail: '抽样失败', appearance_manual: '外观人工触发', data_consistency: '数据一致性', rule_missing: '规则缺失' };
WB.SEV_CN = { critical: '严重', major: '较严重', minor: '轻微' };
WB.REP_CN = { release: '放行报告', incident: '异常处置报告' };
WB._onSearch = function (e) {
  WB.state._search = e.target.value;
  if (WB.state.view === 'batch') WB.renderLeft();
};
WB._ruleDrawer = function (r) {
  WB.openDrawer(r.name + ' v' + r.version_no,
    '项目 ' + r.project_id + ' · 型号 ' + r.model + '\n状态 ' + (WB.RULE_ST_CN[r.status] || r.status) + ' · 版本号 ' + r.version_no + ' · 生效 ' + WB.fmtTime(r.effective_at));
};
WB._locateBatch = function (i) {
  WB.switchView('batch');
  if (i.batch_id) WB.selectBatch(i.batch_id);
};
WB._reportDrawer = function (r) {
  WB.openDrawer((WB.REP_CN || {})[r.report_type] || '报告',
    '报告号 ' + r.report_id + ' · 版本 v' + r.version + ' · 生成 ' + WB.fmtTime(r.generated_at) + '\n内容渲染见 T9。');
};

/* ---- 选中批次：接真实 getBatch + traces（spec §1.4 刷新恢复）---- */
WB.selectBatch = function (id) {
  WB.state.viewStage = null;
  WB.setBatch(id);
  WB._loadDetail(id);
};
WB._loadDetail = function (id) {
  if (!id) { WB.state.selected = null; WB.state.traces = []; WB._renderCenterAndRight(); return; }
  WB.state.detailLoading = true;
  WB._renderCenterAndRight();
  WB.api.getBatch(id).then(function (res) {
    if (!res.ok || !WB.state.detailLoading) return;
    if (!res.ok) { WB.toast(WB.errText(res.error)); }
    else { WB.state.selected = res.data; }
    WB.state.detailLoading = false;
    // 轨迹异步加载（失败不阻断，降级为空）
    WB.api.listTraces(id).then(function (t) {
      if (!WB.state.batchId || WB.state.batchId !== id) return;
      WB.state.traces = t.ok ? t.data : [];
      if (WB.state.view === 'batch') WB._renderCenterAndRight();
    });
    WB._renderCenterAndRight();
  });
};

/* ---- 新建批次（T3 实现表单；T2 占位）---- */
WB._onNewBatch = function () {
  if (WB.newBatchForm) WB.newBatchForm();
  else WB.toast('新建批次表单见 T3');
};

/* ---- 主渲染入口 ---- */
WB._renderCenterAndRight = function () {
  WB.renderCenter(WB.state.selected, WB._listItem(), WB._currentPanel(WB.state.selected));
  WB.renderRight(WB.state.selected);
};
WB._onSelectedChanged = function () {
  // 点击列表项（setBatch 触发）：若选中未变仅重渲染；已变则异步加载
  if (WB.state.selected && WB.state.selected.batch_id === WB.state.batchId) {
    WB._renderCenterAndRight();
  } else if (WB.state.batchId) {
    WB._loadDetail(WB.state.batchId);
  } else {
    WB.state.selected = null;
    WB.state.traces = [];
    WB._renderCenterAndRight();
  }
};

/* ---- 数据加载（T2：批次列表 / 规则库 / 异常 / 轨迹）---- */
WB.loadBatches = function () {
  WB.state.batchesLoading = true;
  WB.renderLeft();
  return WB.api.listBatches().then(function (res) {
    WB.state.batchesLoading = false;
    if (res.ok) { WB.state.batches = res.data; }
    else { WB.state.batches = []; /* 行内错误由 renderLeft 读取 */ }
    WB.state.batchesError = res.ok ? null : res.error;
    WB.renderLeft();
    return res;
  });
};
WB.loadRuleVersions = function () {
  return WB.api.listRuleVersions().then(function (res) {
    if (res.ok) WB.state.ruleVersions = res.data;
    else WB.state.ruleVersions = { items: [], available: false, reason: 'network' };
    if (WB.state.view === 'rules') WB.renderLeft();
    return res;
  });
};
WB.loadIncidents = function () {
  return WB.api.listIncidents().then(function (res) {
    WB.state.incidents = res.ok ? res.data : [];
    if (WB.state.view === 'incidents') WB.renderLeft();
    return res;
  });
};

/* ---- 启动：读 URL → 加载真实数据 → 绑定 → 恢复选中批次 ---- */
WB.boot = function () {
  WB._readUrl();
  WB._bindLeftTabs();
  // 并行加载三类数据
  WB.loadBatches();
  WB.loadRuleVersions();
  WB.loadIncidents();
  // 恢复 URL 选中批次
  if (WB.state.batchId) WB._onSelectedChanged();
  else WB._renderCenterAndRight();
  WB.renderLeft();
};
WB.boot();
