/* WB.stages.release — 放行/拒绝节点（stageIndex 4）
   放行前置校验结果展示（spec §4.5）：全部通过 → 确认放行（released）；
   任一失败 → 展示未通过项与原因，不可放行。
   拒绝（spec §4.6）：填写原因 → 生成异常处置报告并自动归档（rejected+archived）。 */
'use strict';

WB.stages.release = (function () {
  const stageIndex = 4;
  const CHECK_CN = {
    sample_pass: '抽样结论通过',
    no_open_incidents: '无未关闭异常',
    isolated_sns_disposed: '隔离组件已处置',
    evidence_complete: '证据齐备',
    capa_verified: '整改已验证',
    reinspection_covered: '复检覆盖充分',
    iso_values_ok: 'ISO 值达标',
  };

  function render(batch, listitem) {
    const card = WB.h('div', { class: 'card stage-panel' });
    card.append(WB.h('h3', { text: batch.status === 'pending_release' ? '放行决策' : '放行/归档' }));
    card.append(WB.h('div', { class: 'desc', text: batch.status === 'pending_release'
      ? '运行前置校验；全部通过后方可确认放行' : '终态回看' }));
    const body = WB.h('div', {});
    card.append(body);

    if (batch.status === 'pending_release') {
      body.append(WB.h('div', { class: 'hint-line', text: '正在加载前置校验…' }));
      runChecks(batch, body);
      body.append(WB.h('div', { class: 'actions', style: 'margin-top:12px' },
        WB.h('button', { class: 'primary', onclick: function () { releaseModal(batch, body); }, text: '确认放行' }),
        WB.h('button', { class: 'danger', onclick: function () { rejectModal(batch, body); }, text: '拒绝本批' })));
    } else {
      // 终态回看
      const st = WB.ST_CN[batch.status] || batch.status;
      body.append(WB.h('div', { class: 'hint-line' },
        WB.h('span', { class: 'tag ' + (batch.status === 'released' ? 'tag-pass' : batch.status === 'archived' ? '' : 'tag-fail'), text: st })));
      _loadReports(batch, body);
    }
    return card;
  }

  function runChecks(batch, body) {
    WB.api.releaseBatch(batch.batch_id, 'probe').then(function (res) {
      const slot = body.querySelector('.hint-line');
      if (slot) slot.replaceChildren();
      if (!res.ok) {
        body.append(WB.h('div', { class: 'form-err', text: WB.errText(res.error) }));
        return;
      }
      // passed=true 说明已自动置位 released（probe 会真正推进）→ 重新拉取详情
      if (res.data.passed) {
        WB._loadDetail(batch.batch_id);
        return;
      }
      // passed=false：逐条展示未通过项（reasons 指明涉事异常/SN/证据）
      const checks = res.data.checks || [];
      const box = WB.h('div', {});
      checks.forEach(function (c) {
        const name = CHECK_CN[c.name] || c.name;
        if (c.passed) {
          box.append(WB.h('div', { class: 'hint-line' },
            WB.h('span', { class: 'tag tag-pass', text: '✓ ' + name })));
        } else {
          box.append(WB.h('div', { class: 'hint-line' },
            WB.h('span', { class: 'tag tag-fail', text: '✗ ' + name }),
            c.reason ? WB.h('span', { class: 'muted', text: '：' + c.reason }) : null));
        }
      });
      const failed = checks.filter(function (c) { return !c.passed; }).length;
      body.append(WB.h('div', { class: 'hint-line', style: 'font-weight:600', text: failed + ' 项未通过，暂不可放行（校验可重试）' }));
      body.append(box);
    });
  }

  function releaseModal(batch, body) {
    WB.openModal('确认放行 · ' + batch.batch_no, [
      { label: '批准人', type: 'text', required: true },
    ], function (vals) {
      const approved_by = (vals[0] || '').trim();
      if (!approved_by) { WB.toast('请填写批准人'); return; }
      WB.api.releaseBatch(batch.batch_id, approved_by).then(function (res) {
        if (!res.ok) { WB.toast(WB.errText(res.error)); return; }
        if (res.data.passed) {
          WB.toast('已放行，批次进入已放行');
          WB._loadDetail(batch.batch_id);
          WB.loadBatches();
        } else {
          WB.toast('前置校验未全通过，未放行');
          runChecks(batch, body);
        }
      });
    }, { submitLabel: '确认放行' });
  }

  function rejectModal(batch, body) {
    WB.openModal('拒绝本批 · ' + batch.batch_no, [
      { label: '拒绝原因', type: 'textarea', rows: 3, required: true },
      { label: '操作人', type: 'text', required: true },
    ], function (vals) {
      const reason = (vals[0] || '').trim();
      const rejected_by = (vals[1] || '').trim();
      if (!reason || !rejected_by) { WB.toast('请填写原因与操作人'); return; }
      WB.api.rejectBatch(batch.batch_id, reason, rejected_by).then(function (res) {
        if (!res.ok) { WB.toast(WB.errText(res.error)); return; }
        WB.toast('已拒绝并归档，异常处置报告已生成');
        WB._loadDetail(batch.batch_id);
        WB.loadBatches();
      });
    }, { submitLabel: '确认拒绝', danger: true });
  }

  function _loadReports(batch, body) {
    WB.api.listReports(batch.batch_id).then(function (res) {
      if (!res.ok || !res.data.length) return;
      const box = WB.h('div', { style: 'margin-top:8px' });
      box.append(WB.h('div', { class: 'hint-line', text: '报告记录：' }));
      res.data.forEach(function (r) {
        box.append(WB.h('div', { class: 'hint-line' },
          WB.h('span', { class: 'tag', text: (WB.RPT_CN[r.report_type] || r.report_type || '报告') }),
          WB.h('span', { class: 'muted', text: ' · ' + (WB.fmtTime(r.created_at) || '') })));
      });
      body.append(box);
    });
  }

  return { stageIndex: stageIndex, render: render };
})();

WB.RPT_CN = WB.RPT_CN || { release: '放行报告', incident: '异常处置报告', summary: '批次汇总' };
WB.stages._byIndex[4] = WB.stages.release;
