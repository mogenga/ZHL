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
  // 提交送检后的即时结果（intake.js 写入；渲染器画完当前节点面板后追加）。
  // 必须进状态：旧实现把卡片直接 append 到 mainPane，_loadDetail 的异步回调
  // 随后 replaceChildren 整个抹掉 —— 用户看到的就是「提交后无反馈」。
  intakeResult: null,
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
/* 回到工作台总览：清除选中批次与 URL 参数 */
WB.goHome = function () {
  WB.state.viewStage = null;
  WB.state.selected = null;
  WB.state.traces = [];
  WB.state.intakeResult = null;
  WB.setBatch(null);
};

/* ---- 视图切换（左列四标签）---- */
WB.switchView = function (name) {
  WB.state.view = name;
  // 标签选中态跟着视图状态走：程序性切换（_locateBatch / 报告抽屉等）不会经过
  // 标签的点击处理器，高亮若只在那里维护，就会出现「主区已回批次、上面还亮着
  // 异常」（2026-10-15 反馈）。选中态从此只有一个来源。
  WB._syncLeftTabs(name);
  // 报告视图：拉取全局报告列表（跨批次所有报告，默认展示全部）
  // 左侧导航 + 主区报告页共用 WB.state.reports
  if (name === 'reports') {
    WB.state.reports = [];
    WB.api.allReports().then(function (res) {
      WB.state.reports = (res.ok ? res.data : []) || [];
      if (WB.state.view === 'reports') {
        WB.renderLeft();
        WB._renderCenterAndRight();
      }
    });
  }
  WB.renderLeft();
  if (name !== 'batch') WB.state._search = '';
  WB._renderCenterAndRight();
};
WB._syncLeftTabs = function (name) {
  document.querySelectorAll('[data-view]').forEach((b) => {
    b.classList.toggle('on', b.dataset.view === name);
  });
};
WB._bindLeftTabs = function () {
  document.querySelectorAll('[data-view]').forEach((b) => {
    b.addEventListener('click', () => WB.switchView(b.dataset.view));
  });
  const nb = WB.el('newBatchBtn');
  if (nb) nb.addEventListener('click', WB._onNewBatch);
  // 点击左上角品牌 Logo 回到总览
  const brand = document.querySelector('.brand');
  if (brand) {
    brand.style.cursor = 'pointer';
    brand.addEventListener('click', WB.goHome);
  }
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
    main.append(WB.renderHome());
    return;
  }
  main.append(WB.h('div', { class: 'crumb' },
    WB.h('a', { href: '#', onclick: (e) => { e.preventDefault(); WB.goHome(); }, text: '工作台' }),
    ' / 批次 / ' + detail.batch_no));
  main.append(WB.batchHead(detail, listitem));
  if (WB.state.view === 'reports') {
    // 报告页在工作台主区展示（批次报告列表 + 查看详情 + 生成入口），不在左侧栏
    main.append(WB._reportPage(detail));
    return;
  }
  main.append(panelDom || WB.h('div', { class: 'card empty', text: '该节点无可操作项' }));
  // 送检即时结果：跟随渲染周期走，异步 detail 刷新不再把它抹掉。
  // 仅在当前节点确实是数据入场时展示——批次推进后信息已由节点面板承载。
  if (WB.state.intakeResult && detail.status === 'pending_intake' && WB.state.intakeResult.batch_id === detail.batch_id) {
    WB.stages.intake.appendResultCard(main, WB.state.intakeResult.data);
  }
};

/* ---- 报告页（工作台主区）：仅展示当前批次拥有的报告 + 生成入口 + 查看正文 ----
   全部报告列表放在左侧侧栏导航（跨批次），主区保持聚焦当前批次。
   主区数据从全局 WB.state.reports 按 batch_id 过滤，避免双张请求。 */
WB._reportPage = function (detail) {
  const card = WB.h('div', { class: 'card stage-panel report-view-page' });
  card.append(WB.h('h3', { text: '批次报告 · ' + detail.batch_no }));
  card.append(WB.h('div', { class: 'desc', text: '本批次已生成的报告；点击「查看正文」在工作台展开对应报告内容' }));
  const body = WB.h('div', {});
  card.append(body);
  body.append(WB.h('div', { class: 'hint-line', text: '正在加载报告…' }));
  // 先用全局列表过滤当前批次；若尚未加载（首屏）则等待 switchView 刚拉好的列表
  WB.api.allReports().then(function (res) {
    body.replaceChildren();
    if (!res.ok) {
      body.append(WB.h('div', { class: 'form-err', text: WB.errText(res.error) || '报告加载失败' }));
      return;
    }
    const all = res.data || [];
    WB.state.reports = all;
    const list = all.filter(function (r) { return r.batch_id === detail.batch_id; });
    if (!list.length) {
      body.append(WB.h('div', { class: 'hint-line muted', text: '本批次暂无报告' +
        (['released', 'rejected'].indexOf(detail.status) >= 0 ? '（可点击下方生成）' : '（放行/拒绝后可生成）') }));
    } else {
      body.append(WB.h('div', { class: 'hint-line', style: 'font-weight:600', text: '报告记录（' + list.length + '）' }));
      list.forEach(function (r) {
        body.append(WB.h('div', { class: 'hint-line' },
          WB.h('span', { class: 'tag', text: (WB.REP_CN || WB.RPT_CN || {})[r.report_type] || r.report_type || '报告' }),
          WB.h('span', { class: 'muted', text: ' · 版本 v' + (r.version || 1) + ' · ' + (WB.fmtTime(r.generated_at) || '') }),
          WB.h('button', { class: 'newbtn', style: 'font-size:12px;padding:1px 8px;margin-left:8px',
            onclick: function () { WB._expandReportInMain(r, detail); }, text: '查看正文' })));
      });
    }
  });
  // 生成入口：基于当前批次
  const canGen = ['released', 'rejected', 'archived'].indexOf(detail.status) >= 0;
  card.append(WB.h('div', { class: 'actions', style: 'margin-top:12px' },
    canGen
      ? WB.h('button', { class: 'primary', onclick: function () { WB.generateReportForBatch(detail); }, text: '生成报告' })
      : WB.h('span', { class: 'muted', text: '报告可在放行/拒绝后生成' })));
  return card;
};

/* ---- 工作台总览（未选中批次时的默认首页）---- */
WB.NEXT_LABEL = {
  pending_intake: '确认入场', pending_judge: '查看判定', isolated: '创建异常',
  disposing: '填报整改措施', pending_recheck: '录入复检', pending_release: '批准放行',
  released: '生成报告', rejected: '生成报告', archived: '—',
};
WB.renderHome = function () {
  const wrap = WB.h('div', {});
  wrap.append(WB.h('div', { class: 'crumb', text: '工作台 / 总览' }));
  const batches = WB.state.batches || [];
  // 终态批次：released 与 rejected 都会自动归档（D6a），与已归档同组统计，避免
  // 侧栏 KPI「在制」与批次列表「进行中」把已到放行/拒绝终态的批次误计为在制
  const isTerminal = (b) => b.status === 'archived' || b.status === 'released' || b.status === 'rejected';
  const inProgress = batches.filter((b) => !isTerminal(b));
  const cnt = (s) => batches.filter((b) => b.status === s).length;
  const openInc = (WB.state.incidents || []).filter(function (i) {
    return ['new', 'isolated', 'assigned', 'pending_recheck'].indexOf(i.status) >= 0;
  }).length;
  const kpiBox = (n, label) => WB.h('div', { class: 'box' },
    WB.h('div', { class: 'n', text: String(n) }),
    WB.h('div', { class: 'l', text: label }));
  wrap.append(WB.h('div', { class: 'card' },
    WB.h('div', { class: 'bhead' }, WB.h('h2', { text: '工作台总览' })),
    WB.h('div', { class: 'mini-kpi', style: 'margin-top:14px;display:grid;grid-template-columns:repeat(4,1fr);gap:10px' },
      kpiBox(inProgress.length, '在制批次'),
      kpiBox(cnt('pending_intake'), '待入场'),
      kpiBox(cnt('pending_release'), '待放行'),
      kpiBox(openInc, '未关闭异常'))));

  // 待办：进行中批次按下一步动作列出，点击直达
  const todoCard = WB.h('div', { class: 'card' },
    WB.h('h3', { text: '待办批次', style: 'font-size:15px;margin-bottom:4px' }),
    WB.h('div', { class: 'desc', style: 'color:var(--ink2);font-size:13px', text: '按当前进展排列，点击批次继续处理' }));
  const todo = inProgress
    .filter((b) => b.status !== 'draft')
    .sort((a, b) => WB.stageOfStatus(a.status) - WB.stageOfStatus(b.status));
  if (todo.length) {
    todo.forEach((b) => {
      todoCard.append(WB.h('button', { class: 'batch-item', onclick: () => WB.selectBatch(b.batch_id) },
        WB.h('div', { class: 'no' }, WB.h('span', { text: b.batch_no }), WB.chip(b.status)),
        WB.h('div', { class: 'meta' },
          WB.h('span', { text: b.project_id + ' · ' + b.model + ' · 计划 ' + b.planned_quantity + '　' }),
          WB.h('span', { style: 'color:var(--accent)', text: '下一步：' + (WB.NEXT_LABEL[b.status] || '—') }))));
    });
  } else {
    todoCard.append(WB.h('div', { class: 'empty', text: '暂无进行中的批次' }));
  }
  wrap.append(todoCard);

  wrap.append(WB.h('div', { class: 'card', style: 'display:flex;gap:10px;align-items:center' },
    WB.h('span', { class: 'muted', style: 'font-size:13px', text: '要开始一次新的交付检验：' }),
    WB.h('button', { class: 'newbtn', style: 'width:auto;padding:6px 18px;margin:0', onclick: WB._onNewBatch, text: '＋ 新建批次' })));
  return wrap;
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
    WB.h('div', { class: 'actions' },
      (['released', 'rejected', 'archived'].includes(detail.status)
        ? WB.h('button', { onclick: () => WB.generateReportForBatch(detail), text: '生成报告' })
        : WB.h('span', { class: 'muted', text: '报告可在放行后生成' }))),
    WB._pipeline(detail));
};

WB._pipeline = function (detail) {
  const cs = WB.stageOfStatus(detail.status);
  const view = WB.state.viewStage == null ? cs : WB.state.viewStage;
  const locked = view !== cs;
  const btns = WB.STAGES.map((s, i) => {
    /* 已完成以实际进度 cs 判定，与回看位置解耦；
       当前高亮（.cur / .review）跟随点击的 view，不随回看清零前面的 ✓ */
    let cls = 'stage' + (i <= cs ? ' done' : '');
    if (i === view) cls += locked ? ' review' : ' cur';
    const disabled = i > cs || detail.status === 'archived';
    return WB.h('button', {
      class: cls,
      ...(disabled ? { disabled: true } : {}),
      onclick: () => { WB.state.viewStage = i; WB._renderCenterAndRight(); },
    },
      WB.h('div', { class: 'dot', text: i <= cs ? '✓' : String(i + 1) }),
      WB.h('div', { class: 'lbl', text: s }));
  });
  /* 节点线自身在回看模式下必须逐像素不变：.pipe 是 flex 容器、.stage{flex:1}，
     任何追加进去的元素都会参与均分，把六个节点挤窄——这正是「切个节点整条线
     都变了」的成因（2026-10-15 反馈）。返回按钮因此放在 .pipe 之外，单独一行；
     回看也不再加画线（旧的绿线是为"回看会清掉前面的 ✓"补的说明，而 ✓ 现在由
     实际进度 cs 决定、与回看位置解耦，那条线既不必要又会改线形）。 */
  const wrap = WB.h('div', { class: 'pipe-wrap' }, WB.h('div', { class: 'pipe' }, btns));
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
  if (!detail) {
    rp.append(WB.h('div', { style: 'font-weight:700', text: '工作台总览' }));
    rp.append(WB.h('div', { style: 'font-size:12px;color:var(--ink3)', text: '选择批次后查看其轨迹与指标' }));
    rp.append(WB.h('ul', { class: 'tl' },
      WB.h('li', {},
        WB.h('div', { class: 't', text: ' ' }),
        WB.h('div', { class: 'e', text: '左侧「待办批次」可直接进入' }))));
    return;
  }
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
  // 未关闭异常数：本批异常中 status 不在 closed/rejected/scrapped 的计数（spec §3.2）
  const openInc = detail ? WB.state.incidents.filter(function (i) {
    return i.batch_id === detail.batch_id && ['new','isolated','assigned','pending_recheck'].indexOf(i.status) >= 0;
  }).length : 0;
  const sample = detail ? detail.sample_result : null;
  const sampleTxt = sample ? (WB.ST_CN[sample] || sample) : '—';
  rp.append(WB.h('div', { class: 'mini-kpi' },
    kpiBox(String(passCnt), '判定合格'),
    kpiBox(String(failCnt), '判定不合格'),
    kpiBox(String(openInc), '未关闭异常'),
    kpiBox(sampleTxt, '样本结论'),
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
    const isTerminal = (b) => b.status === 'archived' || b.status === 'released' || b.status === 'rejected';
    const active = list.filter((b) => match(b) && !isTerminal(b));
    const done = list.filter((b) => match(b) && isTerminal(b));
    const item = (b) => WB.h('button', {
      class: 'batch-item' + (b.batch_id === WB.state.batchId ? ' on' : ''),
      onclick: () => WB.selectBatch(b.batch_id),
    },
      WB.h('div', { class: 'no' }, WB.h('span', { text: b.batch_no }), WB.chip(b.status)),
      WB.h('div', { class: 'meta', text: b.project_id + ' · ' + b.model + ' · 计划 ' + b.planned_quantity }));
    pane.append(WB.h('div', { class: 'group-title', text: '进行中（' + active.length + '）' }));
    active.forEach((b) => pane.append(item(b)));
    pane.append(WB.h('div', { class: 'group-title', text: '已结束（' + done.length + '）' }));
    done.forEach((b) => pane.append(item(b)));
    if (!list.length) pane.append(WB.h('div', { class: 'empty', text: '暂无批次' }));
  } else if (WB.state.view === 'rules') {
    const rv = WB.state.ruleVersions;
    if (!rv) { pane.append(WB.h('div', { class: 'empty', text: '加载中…' })); return; }
    if (!rv.available) {
      pane.append(WB.h('div', { class: 'empty', text: rv.reason === 'network' ? '规则版本加载失败（网络异常）' : '规则版本数据暂不可用（请先初始化规则库）' }));
      return;
    }
    pane.append(WB.h('div', { class: 'group-title' },
      WB.h('span', { text: '规则版本' }),
      WB.h('button', { class: 'newbtn', style: 'float:right;font-size:12px;padding:2px 8px;margin-left:8px', onclick: WB._onNewRuleVersion, text: '＋ 新建' })));
    (rv.items || []).forEach((r) => pane.append(WB.h('div', { class: 'rule-card' },
      WB.h('button', { class: 'batch-item', style: 'width:100%;text-align:left;margin:0', onclick: () => WB._ruleDrawer(r) },
        WB.h('div', { class: 'no' },
          WB.h('span', { text: r.name + ' v' + r.version_no }),
          WB.h('span', { class: 'chip st-' + (r.status === 'published' ? 'released' : 'draft'), text: WB.RULE_ST_CN[r.status] || r.status })),
        WB.h('div', { class: 'meta', text: r.project_id + ' · ' + r.model + ' · 生效 ' + WB.fmtTime(r.effective_at) })),
      WB._ruleActions(r))));
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
    // 左侧栏报告 tab：列出全局报告（跨批次），点击切到工作台主区展开报告正文。
    // 列表数据由 switchView('reports') 拉取全局 allReports 到 WB.state.reports。
    pane.append(WB.h('div', { class: 'group-title', text: '报告列表（全部）' }));
    (WB.state.reports || []).forEach((r) => pane.append(WB.h('button', { class: 'batch-item',
      onclick: () => { WB._gotoReportInMain(r); } },
      WB.h('div', { class: 'no' },
        WB.h('span', { text: (r.report_id || '').slice(0, 8) }),
        WB.h('span', { class: 'chip st-' + (r.report_type === 'incident' ? 'rejected' : 'released'), text: (WB.REP_CN || WB.RPT_CN || {})[r.report_type] || r.report_type })),
      WB.h('div', { class: 'meta', text: (WB.state.batches ? (WB.state.batches.find(function (b) { return b.batch_id === r.batch_id; }) || {}).batch_no || (r.batch_id || '').slice(0, 8) : (r.batch_id || '').slice(0, 8)) + ' \u00b7 v' + r.version + ' \u00b7 ' + WB.fmtTime(r.generated_at) }))));
    if (!(WB.state.reports || []).length) {
      pane.append(WB.h('div', { class: 'empty', text: '\u6682\u65e0\u62a5\u544a\uff08\u653e\u884c/\u62d2\u7edd\u6279\u6b21\u53ef\u5728\u5de5\u4f5c\u53f0\u4e3b\u533a\u751f\u6210\uff09' }));
    }
  }
};

/* 左侧报告项 -> 工作台主区报告页展开正文（不用右侧抽屉） */
WB._gotoReportInMain = function (r) {
  if (r.batch_id && WB.state.batchId && WB.state.batchId !== r.batch_id) {
    WB.selectBatch(r.batch_id);
  }
  if (WB.state.view !== 'reports') WB.switchView('reports');
  WB._expandReportInMain(r, WB.state.selected);
};
/* spec §5 术语对照表 */
WB.TRIG_CN = { rule_fail: '规则失败', sample_fail: '抽样失败', appearance_manual: '外观人工触发', data_consistency: '数据一致性', rule_missing: '规则缺失' };
WB.SEV_CN = { critical: '严重', major: '较严重', minor: '轻微' };
/* FR-3.6 / §11.3 人工三选一（+ 保留的「搁置」）中文标签 */
WB.DECISION_CN = { adopt: '采纳', reject: '驳回', modify: '修改', defer: '搁置' };
WB.REP_CN = WB.REP_CN || { release: '放行报告', incident: '异常处置报告', summary: '批次汇总' };
WB._onSearch = function (e) {
  WB.state._search = e.target.value;
  if (WB.state.view === 'batch') WB.renderLeft();
};
WB._ruleDrawer = function (r) {
  WB.openDrawer(r.name + ' v' + r.version_no,
    '项目 ' + r.project_id + ' · 型号 ' + r.model + '\n状态 ' + (WB.RULE_ST_CN[r.status] || r.status) + ' · 版本号 ' + r.version_no + ' · 生效 ' + WB.fmtTime(r.effective_at));
};

WB._onNewRuleVersion = function () {
  WB.newRuleForm();
};
WB.newRuleForm = function () {
  // 预填：尽量复用最近一条规则的项目/型号，降低输入成本；版本自动建议（已存在则 +1）
  const rv = WB.state.ruleVersions || { items: [] };
  const last = (rv.items || [])[0] || null;
  const nextVer = (() => {
    const items = (rv.items || []).filter((r) => (!last || (r.project_id === last.project_id && r.model === last.model)));
    const maxV = items.reduce((m, r) => Math.max(m, r.version_no || 0), 0);
    return maxV + 1;
  })();
  const today = new Date().toISOString().slice(0, 10);
  WB.openModal('新建规则版本（草稿）', [
    { label: '项目', type: 'text', value: last ? last.project_id : '', placeholder: '项目标识', required: true },
    { label: '型号', type: 'text', value: last ? last.model : '', placeholder: '如 JH-550W', required: true },
    { label: '名称', type: 'text', value: last ? last.name : '', placeholder: '如 JH-550W 检验规则', required: true },
    { label: '铭牌功率 (W)', type: 'text', value: '550', placeholder: '≥1', required: true },
    { label: '功率容差', type: 'text', value: '0.03', placeholder: '如 0.03', required: true },
    { label: '版本号', type: 'text', value: String(nextVer), placeholder: '≥1', required: true, min: 1 },
    { label: '生效时间', type: 'datetime-local', value: new Date().toISOString().slice(0, 16) },
  ], function (vals, inputs) {
    const project_id = (vals[0] || '').trim();
    const model = (vals[1] || '').trim();
    const name = (vals[2] || '').trim();
    const nameplate_power = parseFloat((vals[3] || '').trim());
    const power_tolerance = parseFloat((vals[4] || '').trim());
    const version_no = parseInt((vals[5] || '').trim(), 10);
    const effRaw = (vals[6] || '').trim();
    if (!project_id || !model || !name || !version_no || Number.isNaN(nameplate_power) || Number.isNaN(power_tolerance)) {
      WB.toast('请填写全部必填项（功率/容差须为数字，版本号 ≥1）');
      return;
    }
    const effective_at = effRaw ? new Date(effRaw).toISOString() : new Date().toISOString();
    WB.api.createRuleVersion({
      project_id: project_id, model: model, name: name,
      nameplate_power: nameplate_power, power_tolerance: power_tolerance,
      parameter_standards: {}, current_bands: {}, sample_plan: {},
      effective_at: effective_at, version_no: version_no,
      source: 'manual', recommendation_ref: null,
    }).then(function (res) {
      if (!res.ok) { WB.toast(WB.errText(res.error)); return; }
      const dup = res.data && res.data.duplicate;
      WB.toast(dup ? '规则版本已存在（同 项目+型号+版本号），返回既存记录' : '草稿已创建，请发布后生效');
      WB.loadRuleVersions().then(function () { if (WB.state.view === 'rules') WB.renderLeft(); });
    });
  }, { submitLabel: '创建草稿' });
};

/* 单条规则卡片下的操作按钮：发布（draft→published）/ 停用（published→disabled）/ 审计 */
WB._ruleActions = function (r) {
  const row = WB.h('div', { class: 'rule-actions' });
  if (r.status === 'draft') {
    row.append(WB.h('button', { class: 'mini-btn', onclick: function () { WB._changeRuleStatus(r, 'publish'); }, text: '发布' }));
  } else if (r.status === 'published') {
    row.append(WB.h('button', { class: 'mini-btn warn', onclick: function () { WB._changeRuleStatus(r, 'disable'); }, text: '停用' }));
  }
  // 停用为终态，无操作
  row.append(WB.h('button', { class: 'mini-btn ghost', onclick: function () { WB._ruleEventsDrawer(r); }, text: '审计' }));
  return row;
};

WB._changeRuleStatus = function (r, action) {
  const isDisable = action === 'disable';
  const fields = [{ label: '操作人（operator）*', type: 'text', value: 'manual', placeholder: '填写操作人', required: true }];
  if (isDisable) fields.push({ label: '停用原因（可选）', type: 'text', placeholder: '如：版本口径变更 / 抽检不合格率上升', required: false });
  const title = isDisable ? ('停用规则版本 ' + r.name + ' v' + r.version_no) : ('发布规则版本 ' + r.name + ' v' + r.version_no);
  WB.openModal(title, fields, function (vals) {
    const operator = (vals[0] || '').trim() || 'manual';
    const reason = isDisable ? ((vals[1] || '').trim() || null) : null;
    if (!operator) { WB.toast('请填写操作人'); return; }
    const body = { operator: operator, source: 'manual', reason: reason };
    const call = isDisable ? WB.api.disableRuleVersion(r.rule_version_id, body) : WB.api.publishRuleVersion(r.rule_version_id, body);
    call.then(function (res) {
      if (!res.ok) { WB.toast(WB.errText(res.error)); return; }
      WB.toast(isDisable ? '规则版本已停用' : '规则版本已发布');
      WB.loadRuleVersions().then(function () { if (WB.state.view === 'rules') WB.renderLeft(); });
    });
  }, { submitLabel: isDisable ? '确认停用' : '确认发布' });
};

WB._ruleEventsDrawer = function (r) {
  WB.openDrawer(r.name + ' v' + r.version_no + ' · 审计事件', WB.h('div', { class: 'empty', text: '加载中…' }));
  WB.api.listRuleVersionEvents(r.rule_version_id).then(function (res) {
    const bd = WB.el('drawerBody');
    bd.replaceChildren();
    if (!res.ok) { bd.append(WB.h('div', { class: 'empty', text: WB.errText(res.error) })); return; }
    const evs = res.data || [];
    if (!evs.length) { bd.append(WB.h('div', { class: 'empty', text: '暂无审计事件' })); return; }
    const ACTION_CN = { created: '创建', published: '发布', disabled: '停用' };
    evs.forEach(function (e) {
      bd.append(WB.h('div', { class: 'ev-item' },
        WB.h('div', { class: 'ev-head' },
          WB.h('span', { class: 'chip st-' + (e.action === 'published' ? 'released' : 'draft'), text: ACTION_CN[e.action] || e.action }),
          WB.h('span', { class: 'ev-op', text: '操作人 ' + (e.operator || '—') + ' · ' + WB.fmtTime(e.created_at) })),
        (e.detail && e.detail.reason) ? WB.h('div', { class: 'ev-reason', text: '原因：' + e.detail.reason }) : null,
        WB.h('div', { class: 'ev-detail', text: 'v' + (e.detail && e.detail.version_no != null ? e.detail.version_no : '—') + ' · 来源 ' + ((e.detail && e.detail.source) || 'manual') })));
    });
  });
};

WB._locateBatch = function (i) {
  WB.switchView('batch');
  if (i.batch_id) WB.selectBatch(i.batch_id);
};
WB._reportDrawer = function (r) {
  if (WB.report && WB.report.open) { WB.report.open(r); return; }
  WB.openDrawer((WB.REP_CN || {})[r.report_type] || '报告',
    '报告号 ' + r.report_id + ' · 版本 v' + r.version + ' · 生成 ' + WB.fmtTime(r.generated_at));
};

/* ---- 选中批次：接真实 getBatch + traces（spec §1.4 刷新恢复）---- */
WB.selectBatch = function (id) {
  WB.state.viewStage = null;
  // 换批次清掉上一批的送检结果（同批次重选保留，刷新恢复场景不清）
  if (WB.state.batchId !== id) WB.state.intakeResult = null;
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

/* ---- 新建批次（T3：模态表单，spec §4.1）---- */
WB._onNewBatch = function () {
  WB.newBatchForm();
};
WB.newBatchForm = function () {
  const rv = WB.state.ruleVersions;
  // 规则版本下拉：仅 status=published 且已生效；available:false / 无可用项时禁用提交
  let published = [];
  if (rv && rv.available) {
    published = (rv.items || []).filter(function (r) { return r.status === 'published'; });
  }
  const options = published.map(function (r) {
    return { value: r.rule_version_id, label: r.name + ' v' + r.version_no + '（' + r.model + '）' };
  });
  const submitDisabled = !rv || !rv.available || published.length === 0;

  const hintEl = submitDisabled
    ? WB.h('div', { class: 'form-note', text: rv && rv.available ? '暂无已发布的规则版本，无法创建批次' : '规则版本未初始化，请先初始化规则库' })
    : null;

  WB.openModal('新建批次', [
    { label: '批次号', type: 'text', placeholder: '如 B-20261013-001', required: true },
    { label: '项目', type: 'text', placeholder: '项目标识', required: true },
    { label: '型号', type: 'text', placeholder: '如 JH-550W', required: true },
    { label: '计划数量', type: 'text', placeholder: '≥1', required: true, min: 1 },
    {
      label: '规则版本', type: 'select', options: options, required: true,
      disabled: submitDisabled,
      hint: submitDisabled ? '' : '仅列已发布且已生效版本',
    },
  ], function (vals) {
    const batch_no = (vals[0] || '').trim();
    const project_id = (vals[1] || '').trim();
    const model = (vals[2] || '').trim();
    const planned = parseInt((vals[3] || '').trim(), 10);
    const rule_version_id = vals[4];
    if (!batch_no || !project_id || !model || !rule_version_id || !planned || planned < 1) {
      WB.toast('请填写全部必填项（计划数量 ≥1）');
      return;
    }
    WB.api.createBatch({
      batch_no: batch_no, project_id: project_id, model: model,
      rule_version_id: rule_version_id, planned_quantity: planned,
    }).then(function (res) {
      if (!res.ok) { WB.toast(WB.errText(res.error)); return; }
      WB.toast('批次已创建，进入数据入场');
      // 成功：回读列表一次再选中新批次（spec §4.1）
      WB.loadBatches().then(function () {
        WB.selectBatch(res.data.batch_id);
      });
    });
  }, { submitLabel: '创建批次', inline: hintEl });
};

/* ---- 主渲染入口 ---- */
WB._renderCenterAndRight = function () {
  WB.renderCenter(WB.state.selected, WB._listItem(), WB._currentPanel(WB.state.selected));
  WB.renderRight(WB.state.selected);
};
WB._onSelectedChanged = function () {
  // 批次 id 变化时先刷新左栏（让蓝色 .on 边框跟随当前选中项），再处理中右栏
  WB.renderLeft();
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
    // 总览首页的 KPI 依赖列表数据：数据到达后若未选中批次则重渲染首页
    if (!WB.state.batchId) WB._renderCenterAndRight();
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
    // 无论是否已选中批次都重渲中栏：详情先到、异常后到的竞态下，
    // 面板不能停留在「尚无异常记录」（曾致创建表单误显→重复建单）
    if (!WB.state.detailLoading) WB._renderCenterAndRight();
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
