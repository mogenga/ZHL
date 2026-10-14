/* WB.stages.reinspection — 复检节点（stageIndex 3）：录入复检结果 + 范围扩展
   对 pending_recheck 的异常录入复检（spec §4 步骤 5）：结果=通过 → 异常闭环、批次放行就绪；
   结果=不通过 → 异常/批次转拒绝（409 行内）。范围扩展需覆盖最小组件集，缺口行内（spec §4.9）。
   数据源为复检记录端点；刷新后展示历史复检记录。 */
'use strict';

WB.stages.reinspection = (function () {
  const stageIndex = 3;
  const SCOPE_CN = { minimal: '最小集', expanded: '扩展', full_lot: '全批' };
  const RESULT_CN = { pass: '通过', fail: '不通过' };

  function pendingIncidents(batch) {
    return WB.incidentByBatch(batch.batch_id).filter(function (i) { return i.status === 'pending_recheck'; });
  }

  function render(batch, listitem) {
    const card = WB.h('div', { class: 'card stage-panel' });
    card.append(WB.h('h3', { text: '复检' }));
    card.append(WB.h('div', { class: 'desc', text: batch.status === 'pending_recheck' ? '对本批待复检异常录入复检结果' : '回看复检记录' }));
    const body = WB.h('div', {});
    card.append(body);

    // 历史复检记录
    if (batch.status === 'pending_recheck') {
      const pending = pendingIncidents(batch);
      if (!pending.length) {
        body.append(WB.h('div', { class: 'empty', text: '本批无待复检异常（可能已由整改触发其它异常）' }));
      }
      pending.forEach(function (i) { body.append(recheckCard(i, batch)); });
    } else {
      _loadHistory(batch, body);
    }
    return card;
  }

  function recheckCard(i, batch) {
    const wrap = WB.h('div', { class: 'card', style: 'margin-top:12px' });
    wrap.append(WB.h('div', { class: 'bhead' },
      WB.h('span', { class: 'mono', text: String(i.incident_id).slice(0, 8) }),
      WB.chip(i.status, 'inc-' + i.status),
      WB.h('span', { class: 'muted', text: '待复检' })));
    const form = WB.h('div', {});
    wrap.append(form);
    _buildForm(i, batch, form);
    return wrap;
  }

  function _buildForm(i, batch, form) {
    form.replaceChildren();
    const inspections = batch.inspections || [];
    const snOptions = inspections.map(function (x) { return x.sn; });
    const fields = [
      { label: '复检组件', type: 'select', required: true, options: snOptions.map(function (s) { return { value: s, label: s }; }) },
      { label: '结果', type: 'select', required: true, value: 'pass', options: [
        { value: 'pass', label: '通过' }, { value: 'fail', label: '不通过' } ] },
      { label: 'Pmax', type: 'text', required: true },
      { label: 'Temp', type: 'text', required: true },
      { label: 'Isc', type: 'text', required: true },
      { label: 'Voc', type: 'text', required: true },
      { label: 'Vm', type: 'text', required: true },
      { label: 'Im', type: 'text', required: true },
      { label: 'Ff', type: 'text', required: true },
      { label: '执行人', type: 'text', required: true },
      { label: '执行时间', type: 'datetime-local', required: true },
      { label: '范围', type: 'select', value: 'minimal', options: [
        { value: 'minimal', label: '最小集' },
        { value: 'expanded', label: '扩展' },
        { value: 'full_lot', label: '全批' } ] },
      { label: '范围组件条码（扩展/全批时必填，多行）', type: 'textarea', rows: 2 },
    ];
    const err = WB.h('div', {});
    WB.openModal('录入复检 · ' + String(i.incident_id).slice(0, 8), fields, function (vals) {
      const num = (v) => parseFloat(v);
      const body2 = {
        inspection_id: _findInspectionId(batch, vals[0]),
        pmax: num(vals[2]), temp: num(vals[3]), isc: num(vals[4]),
        voc: num(vals[5]), vm: num(vals[6]), im: num(vals[7]), ff: num(vals[8]),
        result: vals[1], executor: vals[9], executed_at: new Date(vals[10]).toISOString(),
        scope_type: vals[11],
        scope_sns: (vals[11] !== 'minimal' && vals[12]) ? vals[12].split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean) : null,
      };
      if (!body2.inspection_id) { WB.toast('无法解析复检组件，请重新选择'); return; }
      if ([body2.pmax, body2.temp, body2.isc, body2.voc, body2.vm, body2.im, body2.ff].some(isNaN)) {
        WB.toast('各项复检数值必须为数字'); return;
      }
      WB.api.createReinspection(i.incident_id, body2).then(function (res) {
        if (!res.ok) {
          const e = res.error;
          if (e && e.detail && Array.isArray(e.detail.missing)) {
            WB.toast(WB.errText(e));
            _buildForm(i, batch, form); // 重开让用户补范围组件
            return;
          }
          WB.toast(WB.errText(e));
          return;
        }
        WB.toast(res.data.passed ? '复检通过，请在异常卡片补勾整改措施已验证后确认关闭' : '复检不通过，异常与批次转拒绝');
        WB.loadIncidents().then(function () { WB._loadDetail(batch.batch_id); WB.loadBatches(); });
      });
    }, { submitLabel: '提交复检', inline: err });
  }

  function _findInspectionId(batch, sn) {
    const hit = (batch.inspections || []).find(function (x) { return x.sn === sn; });
    return hit ? (hit.inspection_id || hit.inspection || null) : null;
  }

  function _loadHistory(batch, body) {
    const pending = WB.incidentByBatch(batch.batch_id);
    if (!pending.length) { body.append(WB.h('div', { class: 'empty', text: '本批无异常/复检记录' })); return; }
    body.append(WB.h('div', { class: 'hint-line', text: '本批异常与复检记录：' }));
    pending.forEach(function (i) {
      WB.api.listReinspections(i.incident_id).then(function (res) {
        const box = WB.h('div', { class: 'card', style: 'margin-top:8px' });
        box.append(WB.h('div', { class: 'bhead' },
          WB.h('span', { class: 'mono', text: String(i.incident_id).slice(0, 8) }),
          WB.chip(i.status, 'inc-' + i.status)));
        if (!res.ok || !res.data.length) {
          box.append(WB.h('div', { class: 'muted', text: '暂无复检记录' }));
        } else {
          res.data.forEach(function (r) {
            box.append(WB.h('div', { class: 'hint-line' },
              WB.h('span', { class: 'tag ' + (r.passed ? 'tag-pass' : 'tag-fail'), text: (r.sn || '—') + ' · ' + RESULT_CN[r.result] + ' · 范围 ' + (SCOPE_CN[r.scope_applied] || r.scope_applied) })));
          });
        }
        body.append(box);
      });
    });
  }

  return { stageIndex: stageIndex, render: render };
})();

WB.stages._byIndex[3] = WB.stages.reinspection;
