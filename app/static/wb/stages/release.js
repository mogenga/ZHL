/* WB.stages.release — 放行/拒绝节点（stageIndex 4）
   放行前置校验结果展示（spec §4.5）：全部通过 → 确认放行（released）；
   任一失败 → 展示未通过项与原因，不可放行。
   拒绝（spec §4.6）：填写原因 → 生成异常处置报告并自动归档（rejected+archived）。 */
'use strict';

WB.stages.release = (function () {
  const stageIndex = 4;
  // 与后台 assert_release 七项检查命中对齐（app/domain/release.py）
  const CHECK_CN = {
    state: '批次处于待放行',
    sample_passed: '抽样结论通过',
    no_open_critical_incident: '无未关闭严重异常',
    no_isolated_sn: '隔离组件已处置',
    all_evidence_present: '必需证据齐备',
    all_capa_verified: '整改措施已验证',
    reinspection_scope_covered: '复检范围覆盖充分',
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
      const archived = batch.status === 'archived';
      const stTxt = archived ? '已拒绝并归档' : (WB.ST_CN[batch.status] || batch.status);
      const cls = batch.status === 'released' ? 'tag-pass' : (archived ? '' : 'tag-fail');
      body.append(WB.h('div', { class: 'hint-line' },
        WB.h('span', { class: 'tag ' + cls, text: stTxt })));
      // 终态提示：终态批次的异常数与报告数（用户看得见关联数据，避免「无异常、无报告」的误论）
      body.append(WB.h('div', { class: 'hint-line muted' },
        WB.h('span', { text: '关联异常：' + ((batch.incidents || []).length || '暂无') })));
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
      // 抽样结论阻断时给一条可执行的出路：复检已通过并关闭的批次，结论侧
      // 可能因历史缺陷没有回写第 2 轮（结论永远停在第 1 轮 fail）。由质量
      // 工程师显式触发重算；闭环没走完时后台如实回报，界面不制造"已通过"。
      if (checks.some(function (c) { return c.name === 'sample_passed' && !c.passed; })) {
        body.append(WB.h('div', { class: 'actions', style: 'margin-top:12px' },
          WB.h('button', { class: 'primary', onclick: function () { recheck(batch, body); }, text: '重算复检结论' })));
      }
    });
  }

  /* 复检闭环结论重算（POST /api/batches/{id}/recheck，幂等）：
     闭环成立 → 补写第 2 轮抽样结论并重跑校验；不成立 → 如实回报仍缺复检的 SN。 */
  function recheck(batch, body) {
    WB.api.recheckBatch(batch.batch_id).then(function (res) {
      if (!res.ok) { WB.toast(WB.errText(res.error)); return; }
      const d = res.data || {};
      if (d.written) {
        WB.toast('已补写第 2 轮抽样结论（' + (d.result || '') + '）');
        runChecks(batch, body);
      } else if (d.result === 'pass') {
        WB.toast('第 2 轮抽样结论已在，无需重算');
        runChecks(batch, body);
      } else if (d.skipped === 'no_sample_conclusion') {
        // 该批次没有抽样结论可复检 —— 与"复检没覆盖到位"是两回事，分开说。
        WB.toast('该批次没有抽样结论，无需重算');
      } else {
        WB.toast('复检闭环未走完：' + (d.reason || '复检未覆盖全部不合格 SN'));
      }
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
      const box = WB.h('div', { style: 'margin-top:8px' });
      if (!res.ok) {
        box.append(WB.h('div', { class: 'hint-line muted', text: WB.errText(res.error) || '报告加载失败' }));
        body.append(box);
        return;
      }
      if (!res.data.length) {
        box.append(WB.h('div', { class: 'hint-line muted', text: '暂无报告记录' }));
        body.append(box);
        return;
      }
      const incCount = (batch.incidents || []).length;
      box.append(WB.h('div', { class: 'hint-line', text: '报告记录（' + res.data.length + '）' + (incCount ? ' · 关联异常 ' + incCount : '') }));
      res.data.forEach(function (r) {
        box.append(WB.h('div', { class: 'hint-line' },
          WB.h('span', { class: 'tag', text: (WB.RPT_CN[r.report_type] || r.report_type || '报告') }),
          WB.h('span', { class: 'muted', text: ' · 版本 v' + (r.version || 1) + ' · ' + (WB.fmtTime(r.generated_at || r.created_at) || '') }),
          WB.h('button', { class: 'newbtn', style: 'font-size:12px;padding:1px 8px;margin-left:8px', onclick: function () { _viewReport(batch, r); }, text: '查看' })));
      });
      body.append(box);
    });
  }
  // 复用左栏报告视图的统一抽屉渲染（WB.report），避免各自维护一份
  function _viewReport(batch, r) {
    if (WB.report && WB.report.open) {
      // listReports 已带 content_md/content_json，影响只是把它适配成 WB.report.open 期望的
      // 报告输出形（金 key 对齐 ReportOut）
      WB.report.open(r);
      return;
    }
    WB.api.getReport(batch.batch_id, r.report_id).then(function (res) {
      if (!res.ok) { WB.toast(WB.errText(res.error)); return; }
      WB.openDrawer((WB.RPT_CN[res.data.report_type] || res.data.report_type) + ' v' + res.data.version,
        WB.h('pre', { text: res.data.content_md || JSON.stringify(res.data.content_json || {}, null, 2),
          style: 'white-space:pre-wrap;font-size:13px' }));
    });
  }

  return { stageIndex: stageIndex, render: render };
})();

WB.RPT_CN = WB.RPT_CN || { release: '放行报告', incident: '异常处置报告', summary: '批次汇总' };
WB.stages._byIndex[4] = WB.stages.release;
