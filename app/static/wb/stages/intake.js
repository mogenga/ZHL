/* WB.stages.intake — 数据入场节点（stageIndex 0）
   提交送检数据（spec §4.2）→ 展示置信度/低置信清单/抽取字段；
   确认/拒绝入场（spec §4.3）；自动确认（≥0.8）时界面跟随新状态不死等。
   数据来源仅 spec §3 事实表端点；刷新后回看降级为组件条码+入场状态（§1.3 边界）。 */
'use strict';

WB.stages.intake = (function () {
  const stageIndex = 0;

  /* ---- 提交送检数据表单 ---- */
  function submitForm(batch) {
    const fields = [
      { label: '数据来源', type: 'select', value: 'IV_CSV', options: [
        { value: 'IV_CSV', label: '检测数据' },
        { value: 'OCR_FORM', label: '表单识别' },
        { value: 'MANUAL', label: '人工录入' },
      ] },
      { label: '检测数据（每行一条，含组件条码）', type: 'textarea', rows: 6,
        placeholder: 'SN-001,23.5,62.1,...\nSN-002,24.0,61.8,...', required: true },
      { label: '人工覆盖字段（可选）', type: 'text', placeholder: 'pmax=24.1, appearance_result=pass' },
    ];
    WB.openModal('提交送检数据', fields, function (vals) {
      const source_type = vals[0];
      const raw = (vals[1] || '').trim();
      const overrideStr = (vals[2] || '').trim();
      if (!raw) { WB.toast('请填写检测数据'); return; }
      // 多行 → inspections[]；仅保留可解析行（SN 为第一列）
      const inspections = raw.split(/\r?\n/).map((l) => l.trim()).filter(Boolean)
        .map((l) => { const sn = l.split(',')[0].trim(); return { sn: sn, source_type: source_type }; });
      const fieldsOverride = parseOverrides(overrideStr);
      WB.api.submitIntake({
        batch_id: batch.batch_id,
        source_type: source_type,
        inspections: inspections,
        fields: fieldsOverride || null,
      }).then(function (res) {
        if (!res.ok) { WB.toast(WB.errText(res.error)); return; }
        showIntakeResult(batch, res.data);
      });
    }, { submitLabel: '提交送检' });
  }

  function parseOverrides(s) {
    if (!s) return null;
    const out = {};
    s.split(/[,，]/).forEach((pair) => {
      const i = pair.indexOf('=');
      if (i > 0) out[pair.slice(0, i).trim()] = pair.slice(i + 1).trim();
    });
    return out;
  }

  /* ---- 提交后即时展示：置信度/抽取字段/低置信清单 + 确认/拒绝 ---- */
  function showIntakeResult(batch, data) {
    const panel = WB.el('mainPane');
    const card = WB.h('div', { class: 'card stage-panel' });
    card.append(WB.h('h3', { text: '入场结果' }));
    const body = WB.h('div', {});
    card.append(body);
    // 移除占位面板
    const existing = panel.querySelector('.stage-panel'); if (existing) existing.remove();
    panel.append(card);
    _renderIntakeBody(body, batch, data);
  }
  function _renderIntakeBody(body, batch, data) {
    body.replaceChildren();
    const conf = data.confidence != null ? Math.round(data.confidence * 100) + '%' : '—';
    const stTxt = data.status === 'confirmed' ? '已自动确认入场' : '待人工确认';
    body.append(WB.h('div', { class: 'hint-line' },
      WB.h('span', { class: 'tag ' + (data.status === 'confirmed' ? 'tag-pass' : 'tag-warn'), text: stTxt + ' · 置信度 ' + conf })));
    // 低置信组件清单
    const low = data.low_conf_sns || [];
    if (low.length) {
      body.append(WB.h('div', { class: 'hint-line', text: '低置信组件（' + low.length + '）：' + low.join('、') }));
    }
    // 抽取字段对照
    const ex = data.extracted_fields || {};
    const keys = Object.keys(ex);
    if (keys.length) {
      const rows = keys.map((k) => WB.h('tr', {},
        WB.h('td', { text: k }), WB.h('td', { class: 'num', text: String(ex[k]) })));
      body.append(WB.h('table', {}, WB.h('tr', {}, WB.h('th', { text: '字段' }), WB.h('th', { text: '值' })), rows));
    }
    // 操作按钮
    const actions = WB.h('div', { class: 'actions' },
      WB.h('button', { class: 'primary', onclick: () => _intakeAct(batch, 'confirm'), text: '确认入场' }),
      WB.h('button', { class: 'danger', onclick: () => _intakeAct(batch, 'reject'), text: '拒绝入场' }),
      WB.h('button', { onclick: () => WB._loadDetail(batch.batch_id), text: '重新提交' }));
    body.append(actions);
  }

  function _intakeAct(batch, action) {
    WB.api.confirmIntake(batch.batch_id, { action: action, fields: null }).then(function (res) {
      if (!res.ok) { WB.toast(WB.errText(res.error)); return; }
      WB.toast(action === 'confirm' ? '已确认入场' : '已拒绝入场，批次仍待入场');
      // 确认/拒绝后后端图同步推进（判定/抽样）→ 刷新批次跟随新状态（spec §4.3）
      WB._loadDetail(batch.batch_id);
      WB.loadBatches();
    });
  }

  /* ---- 节点面板：当前 pending_intake → 提交表单；回看（非当前态）→ 降级组件入场状态 ---- */
  function render(batch, listitem) {
    const card = WB.h('div', { class: 'card stage-panel' });
    card.append(WB.h('h3', { text: '数据入场' }));
    card.append(WB.h('div', { class: 'desc', text: batch.status === 'pending_intake'
      ? '提交本批送检数据，确认或拒绝入场' : '回看：组件条码与入场状态' }));
    const body = WB.h('div', {});
    card.append(body);
    if (batch.status === 'pending_intake') {
      body.append(WB.h('button', { class: 'primary', onclick: () => submitForm(batch), text: '提交送检数据' }));
      body.append(WB.h('div', { class: 'hint-line', text: '提交后展示置信度与低置信清单；确认/拒绝入口随之出现' }));
    } else {
      // 降级：组件条码 + 入场状态（§1.3 边界；置信度不重建）
      const inspections = batch.inspections || [];
      if (inspections.length) {
        const rows = inspections.map((i) => WB.h('tr', {},
          WB.h('td', { class: 'num', text: i.sn }),
          WB.h('td', { text: WB.ST_CN[i.intake_status] || i.intake_status || '—' })));
        body.append(WB.h('table', {},
          WB.h('tr', {}, WB.h('th', { text: '组件条码' }), WB.h('th', { text: '入场状态' })), rows));
      } else {
        body.append(WB.h('div', { class: 'empty', text: '暂无组件数据' }));
      }
    }
    return card;
  }

  return { stageIndex: stageIndex, render: render };
})();

WB.stages._byIndex[0] = WB.stages.intake;
