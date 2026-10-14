/* WB.stages.incident — 异常处置节点（stageIndex 2）：隔离→归因→指派→整改措施
   面板列出本批全部异常，每卡按异常状态只渲染一个主操作（spec §2.2 顺序硬约束）：
     new → 隔离异常；isolated → AI 归因 + 指派责任人；assigned → 填报整改措施；
     pending_recheck → 提示转复检。
   isolated 批次且无异常时 → 创建异常表单（spec §4 步骤 2）。
   409/400 错误行内呈现（spec §4.9）。 */
'use strict';

WB.stages.incident = (function () {
  const stageIndex = 2;
  const OPEN = ['new', 'isolated', 'assigned', 'pending_recheck'];

  WB.incidentByBatch = function (batchId) {
    return (WB.state.incidents || []).filter(function (i) { return i.batch_id === batchId; });
  };

  function render(batch, listitem) {
    const card = WB.h('div', { class: 'card stage-panel' });
    card.append(WB.h('h3', { text: '异常处置' }));
    const incs = WB.incidentByBatch(batch.batch_id);
    const body = WB.h('div', {});
    card.append(body);

    // 批次非 isolated 阶段（如待判定/待放行回看）：只读展示
    if (batch.status === 'isolated' && !incs.length) {
      body.append(WB.h('div', { class: 'hint-line', text: '本批已隔离，尚无异常记录。' }));
      body.append(WB.h('button', { class: 'primary', onclick: function () { createIncidentForm(batch, body); }, text: '创建异常' }));
      return card;
    }

    if (!incs.length) {
      body.append(WB.h('div', { class: 'empty', text: '本批暂无异常' }));
      return card;
    }

    const autoMap = WB.incidentAutoInfo();
    const hasAuto = incs.some(function (i) { return !!autoMap[String(i.incident_id)]; });
    if (hasAuto) {
      body.append(WB.h('div', { class: 'hint-line', text: '「自动」= 批次隔离时系统自动创建（判定/抽样失败触发），无需重复创建' }));
    }
    incs.forEach(function (i) { body.append(incidentCard(i, batch)); });
    // 已有异常时也可再创建
    body.append(WB.h('div', { class: 'actions' },
      WB.h('button', { onclick: function () { createIncidentForm(batch, body); }, text: '＋ 再建一条异常' })));
    return card;
  }

  /* ---- 自动异常推导（重复单缺陷 2026-10）：ensure_incident 同事务写
     incident_auto 轨迹（structured_output.incident_id），人工端点不写 →
     命中即「系统自动创建」。返回 {incident_id: mapped_sns[]}。 ---- */
  WB.incidentAutoInfo = function () {
    const map = {};
    (WB.state.traces || []).forEach(function (t) {
      if (t.skill_name !== 'incident_auto' || t.validation_result !== 'pass') return;
      const out = t.structured_output || {};
      if (!out.created || !out.incident_id) return;
      map[String(out.incident_id)] = (t.params_summary && t.params_summary.mapped_sns) || [];
    });
    return map;
  };

  /* ---- 单卡：按状态渲染唯一主操作（spec §2.2）---- */
  function incidentCard(i, batch) {
    const autoSns = WB.incidentAutoInfo()[String(i.incident_id)];
    const wrap = WB.h('div', { class: 'card', style: 'margin-top:12px' });
    wrap.append(WB.h('div', { class: 'bhead' },
      WB.h('span', { class: 'mono', text: String(i.incident_id).slice(0, 8) }),
      WB.chip(i.status, 'inc-' + i.status),
      WB.h('span', { class: 'muted', text: (WB.TRIG_CN[i.trigger_type] || i.trigger_type) + ' · ' + (WB.SEV_CN[i.severity] || i.severity) }),
      autoSns ? WB.h('span', { class: 'chip chip-auto', text: '自动' }) : null));
    const meta = WB.h('div', { class: 'kv' });
    if (autoSns && autoSns.length) meta.append(krow('受影响组件', autoSns.join('、')));
    if (i.owner) meta.append(krow('责任人', i.owner));
    if (i.due_at) meta.append(krow('处置期限', WB.fmtTime(i.due_at)));
    if (i.confirmed_root_cause) meta.append(krow('确认根因', i.confirmed_root_cause));
    if (i.corrective_action) meta.append(krow('整改措施', i.corrective_action));
    if (meta.childNodes.length) wrap.append(meta);
    const actions = WB.h('div', { class: 'actions' });
    wrap.append(actions);
    _primaryAction(i, batch, actions);
    return wrap;
  }
  function krow(k, v) { return WB.h('div', {}, WB.h('div', { class: 'k', text: k }), WB.h('div', { class: 'v', text: String(v) })); }

  function _primaryAction(i, batch, actions) {
    const slot = WB.h('div', {});
    actions.append(slot);
    const fail = function (msg) { slot.replaceChildren(WB.h('div', { class: 'form-err', text: msg })); };
    const done = function (msg) {
      slot.replaceChildren(WB.h('div', { class: 'hint-line', text: msg }));
      // 刷新异常列表与批次（状态推进驱动面板重渲染）
      WB.loadIncidents().then(function () { WB._loadDetail(batch.batch_id); WB.loadBatches(); });
    };
    switch (i.status) {
      case 'new':
        slot.append(WB.h('button', { class: 'primary', onclick: function () {
          WB.api.isolateIncident(i.incident_id).then(function (r) {
            r.ok ? done('已隔离该异常') : fail(WB.errText(r.error));
          });
        }, text: '隔离异常' }));
        break;
      case 'isolated':
        slot.append(
          WB.h('button', { class: 'primary', onclick: function () { attributeModal(i, batch, slot, done, fail); }, text: 'AI 归因' }),
          WB.h('button', { onclick: function () { assignModal(i, batch, slot, done, fail); }, text: '指派责任人' }));
        break;
      case 'assigned':
        slot.append(WB.h('button', { class: 'primary', onclick: function () { capaModal(i, batch, slot, done, fail); }, text: '填报整改措施' }));
        break;
      case 'pending_recheck':
        slot.append(WB.h('span', { class: 'hint-line', text: '已转待复检，请在复检节点录入结果' }));
        // 复检通过后的闭环：补勾整改措施已验证 → 确认关闭（P0 缺口 #1）
        slot.append(WB.h('button', { onclick: function () { capaModal(i, batch, slot, done, fail); }, text: '补勾整改措施已验证' }));
        slot.append(WB.h('button', { class: 'primary', onclick: function () { closeIncidentFlow(i, batch, slot, done, fail); }, text: '确认关闭异常' }));
        break;
      default:
        slot.append(WB.h('span', { class: 'hint-line', text: '当前状态 ' + (WB.ST_CN[i.status] || i.status) }));
    }
  }

  /* ---- AI 归因（spec §2.2 / §4.9）：生成假设（本次请求）→ 人工三选一（另一次请求）---- */
  function attributeModal(i, batch, slot, done, fail) {
    // 第一步：不带 decision → 后端生成假设并把运行停在 human_adopt 中断点。
    WB.api.attributeIncident(i.incident_id, null, null).then(function (res) {
      if (!res.ok) { fail(WB.errText(res.error)); return; }
      const hyp = res.data.hypotheses || [];
      const hypBox = WB.h('div', {});
      if (hyp.length) {
        hyp.forEach(function (h, idx) {
          const label = (typeof h === 'string') ? h : (h.cause || h.hypothesis || JSON.stringify(h));
          const meta = (h && typeof h === 'object')
            ? '（置信度 ' + Math.round((h.confidence || 0) * 100) + '% · 证据 ' + ((h.evidence_ids || []).length) + ' 条）'
            : '';
          hypBox.append(WB.h('div', { class: 'hint-line' }, WB.h('b', { text: '#' + (idx + 1) + ' ' }), WB.h('span', { text: label + ' ' + meta })));
        });
      } else {
        hypBox.append(WB.h('div', { class: 'hint-line', text: res.data.hint || '暂无可用假设，请人工填写根因' }));
      }
      const err = WB.h('div', {});
      const fields = [
        { label: '根因（采纳时必填；可直接抄上方假设）', type: 'textarea', rows: 3, value: res.data.confirmed_root_cause || '' },
      ];
      WB.openModal('AI 归因 · ' + String(i.incident_id).slice(0, 8), fields, function (vals) {
        const cause = (vals[0] || '').trim();
        // 第二步：另一次请求把人工决定 resume 回停住的运行。
        _resumeAttr(i, batch, 'adopt', cause || null, function () {
          slot.replaceChildren(WB.h('div', { class: 'hint-line', text: '已采纳根因：' + cause }));
          WB.loadIncidents().then(function () { WB._loadDetail(batch.batch_id); });
        }, fail);
      }, { submitLabel: '采纳', inline: hypBox, extraButtons: [
        { label: '驳回', cls: 'danger', onclick: function () {
          _resumeAttr(i, batch, 'reject', null, function () {
            slot.replaceChildren(WB.h('div', { class: 'hint-line', text: '已驳回全部假设，请人工填写根因' }));
            WB.loadIncidents().then(function () { WB._loadDetail(batch.batch_id); });
          }, fail);
        } },
        { label: '搁置', cls: '', onclick: function () {
          _resumeAttr(i, batch, 'defer', null, function () {
            slot.replaceChildren(WB.h('div', { class: 'hint-line', text: '已搁置，稍后可再次发起归因' }));
          }, fail);
        } },
      ] });
    });
  }
  function _resumeAttr(i, batch, decision, cause, ok, fail) {
    WB.api.adoptAttribution(i.incident_id, decision, cause).then(function (r) {
      if (r.ok) { WB.toast('已记录归因决定：' + decision); ok(r.data); }
      else fail(WB.errText(r.error));
    });
  }

  // 指派责任人表单（spec §4 步骤 4）：责任人 / 处置期限 / 临时措施
  function assignModal(i, batch, slot, done, fail) {
    WB.openModal('指派责任人 · ' + String(i.incident_id).slice(0, 8), [
      { label: '责任人', type: 'text', required: true },
      { label: '处置期限', type: 'datetime-local', required: true },
      { label: '临时措施', type: 'text' },
    ], function (vals) {
      const owner = (vals[0] || '').trim();
      const dueRaw = vals[1];
      if (!owner || !dueRaw) { WB.toast('请填写责任人与处置期限'); return false; }
      const due_at = new Date(dueRaw).toISOString();
      WB.api.assignIncident(i.incident_id, { owner: owner, due_at: due_at, containment_action: (vals[2] || '').trim() || null }).then(function (r) {
        r.ok ? done('已指派责任人，批次转处置中') : fail(WB.errText(r.error));
      });
    }, { submitLabel: '指派' });
  }

  // 整改措施表单（spec §4 步骤 4）：确认根因 / 纠正措施 / 已验证复选
  function capaModal(i, batch, slot, done, fail) {
    WB.openModal('整改措施 · ' + String(i.incident_id).slice(0, 8), [
      { label: '确认根因', type: 'text', required: true, value: i.confirmed_root_cause || '' },
      { label: '纠正措施', type: 'textarea', rows: 3, required: true },
      { label: '已验证（需已有通过的复检记录）', type: 'checkbox', value: false },
    ], function (vals) {
      const confirmed_root_cause = (vals[0] || '').trim();
      const corrective_action = (vals[1] || '').trim();
      const verified = !!vals[2];
      if (!confirmed_root_cause || !corrective_action) { WB.toast('请填写确认根因与纠正措施'); return false; }
      WB.api.capaIncident(i.incident_id, { confirmed_root_cause: confirmed_root_cause, corrective_action: corrective_action, verified: verified }).then(function (r) {
        if (!r.ok) {
          // spec §4.9：verified 需通过复检 → 回退复选并提示
          if (r.error && r.error.key === 'capa_verified_requires_pass_reinspection') {
            WB.toast(WB.errText(r.error));
            capaModal(i, batch, slot, done, fail); // 重开表单让使用者取消已验证
            return;
          }
          fail(WB.errText(r.error));
          return;
        }
        done(i.status === 'pending_recheck' ? '整改措施已更新' : '已填报整改措施，批次转待复检');
      });
    }, { submitLabel: '提交' });
  }

  // 确认关闭异常（P0 缺口 #1）：close 端点五条件校验；缺失项逐条行内展示
  var MISSING_CN = {
    owner: '未指派责任人',
    sn_not_linked: '异常未关联受影响组件',
    original_decision_evidence: '缺少原始判定证据',
    containment_action: '未填报临时措施',
    capa_fields: '整改措施未填报完整（确认根因 / 纠正措施）',
    reinspection_record: '无独立复检记录',
    reinspection_passed: '复检未通过',
    capa_verified: '整改措施未勾选已验证',
  };
  function closeIncidentFlow(i, batch, slot, done, fail) {
    WB.api.closeIncident(i.incident_id).then(function (r) {
      if (r.ok) {
        WB.toast(r.data.batch_status === 'pending_release' ? '异常已关闭，批次转待放行' : '异常已关闭');
        WB.loadIncidents().then(function () { WB._loadDetail(batch.batch_id); WB.loadBatches(); });
        return;
      }
      const e = r.error;
      if (e && e.key === 'close_conditions_unmet' && Array.isArray(e.detail && e.detail.missing)) {
        slot.replaceChildren(WB.h('div', { class: 'hint-line', text: '关闭条件未满足：' }),
          WB.h('ul', {}, e.detail.missing.map(function (m) {
            return WB.h('li', { text: MISSING_CN[m] || m });
          })));
        return;
      }
      fail(WB.errText(e));
    });
  }

  /* ---- 创建异常表单（isolated 批次；spec §4 步骤 2）----
     打开前实时拉取本批未关闭异常：同触发原因已有在办异常时行内提示 +
     按钮变「仍要新建」（软防呆，不硬阻断——业务允许多条同因异常）。
     校验失败不关窗（保留已填输入）。 */
  function createIncidentForm(batch, body) {
    const sns = (batch.inspections || []).map(function (x) { return x.sn; });
    const fields = [
      { label: '触发原因', type: 'select', required: true, options: [
        { value: 'rule_fail', label: '规则失败' },
        { value: 'sample_fail', label: '抽样失败' },
        { value: 'appearance_manual', label: '外观人工触发' },
        { value: 'data_consistency', label: '数据一致性' },
        { value: 'rule_missing', label: '规则缺失' },
      ] },
      { label: '严重度', type: 'select', required: true, value: 'major', options: [
        { value: 'critical', label: '严重' },
        { value: 'major', label: '较严重' },
        { value: 'minor', label: '轻微' },
      ] },
      { label: '受影响组件条码（多行，每行一个）', type: 'textarea', rows: 3, required: true,
        hint: sns.length ? '本批可用：' + sns.join('、') : '按本批组件条码填写' },
      { label: '证据引用（可空，逗号分隔）', type: 'text' },
    ];
    const inline = WB.h('div', {});
    const dupSlot = WB.h('div', {});
    const errSlot = WB.h('div', { class: 'form-err' });
    inline.append(dupSlot, errSlot);
    const OPEN = ['new', 'isolated', 'assigned', 'pending_recheck'];
    // 实时取本批未关闭异常（state 里可能是旧数据，竞态正是重复单的根源）
    WB.api.listIncidents().then(function (res) {
      const open = res.ok ? (res.data || []).filter(function (i) {
        return i.batch_id === batch.batch_id && OPEN.indexOf(i.status) >= 0;
      }) : [];
      let m = null;
      const updateDup = function () {
        if (!m) return;
        const trigger = m.inputs[0] ? m.inputs[0].value : null;
        const dup = open.filter(function (i) { return i.trigger_type === trigger; });
        if (dup.length) {
          dupSlot.replaceChildren(WB.h('div', { class: 'hint-line', text: '注意：本批已有 ' + dup.length +
            ' 条未关闭的「' + (WB.TRIG_CN[trigger] || trigger) + '」异常（' +
            dup.map(function (i) { return String(i.incident_id).slice(0, 8); }).join('、') +
            '），提交将再新建一条。' }));
          if (m.submitBtn) m.submitBtn.textContent = '仍要新建';
        } else {
          dupSlot.replaceChildren();
          if (m.submitBtn) m.submitBtn.textContent = '创建异常';
        }
      };
      m = WB.openModal('创建异常 · ' + batch.batch_no, fields, function (vals) {
        const trigger_type = vals[0], severity = vals[1];
        const affected_sns = (vals[2] || '').split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
        const evidence_ids = (vals[3] || '').split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean);
        if (!affected_sns.length) {
          errSlot.replaceChildren(WB.h('div', { text: '请填写受影响组件条码' }));
          return false; // 不关窗，保留输入
        }
        WB.api.createIncident({
          batch_id: batch.batch_id, trigger_type: trigger_type, severity: severity,
          affected_sns: affected_sns, evidence_ids: evidence_ids,
        }).then(function (r) {
          if (!r.ok) {
            WB.toast(WB.errText(r.error));
            // sn_not_found / evidence_not_found → 缺失清单行内
            if (r.error && r.error.detail && Array.isArray(r.error.detail.missing)) {
              errSlot.replaceChildren(WB.h('div', { text: '以下引用不存在：' + r.error.detail.missing.join('、') }));
            }
            return;
          }
          WB.toast('异常 ' + String(r.data.incident_id).slice(0, 8) + ' 已创建，请隔离该异常');
          WB.loadIncidents().then(function () { WB._loadDetail(batch.batch_id); WB.loadBatches(); });
        });
      }, { submitLabel: '创建异常', inline: inline });
      if (m && m.inputs[0] && m.inputs[0].addEventListener) {
        m.inputs[0].addEventListener('change', updateDup);
      }
      updateDup();
    });
  }

  return { stageIndex: stageIndex, render: render };
})();

WB.stages._byIndex[2] = WB.stages.incident;
