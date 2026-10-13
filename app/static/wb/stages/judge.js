/* WB.stages.judge — 判定节点（stageIndex 1）：回看 + 重新判定
   数据源为批次详情 inspections[]/sample_result（spec §4.4，非判定端点响应）；
   「重新判定」调 POST /judge 刷新明细（不改状态机）；
   400 unconfirmed_sns → 展示未确认条码清单 + 「请先完成入场确认」。 */
'use strict';

WB.stages.judge = (function () {
  const stageIndex = 1;

  function render(batch, listitem) {
    const card = WB.h('div', { class: 'card stage-panel' });
    card.append(WB.h('h3', { text: '判定' }));
    card.append(WB.h('div', { class: 'desc', text: '逐组件判定明细与抽样结论' }));
    const body = WB.h('div', {});
    card.append(body);

    const inspections = batch.inspections || [];
    const sn = WB.ST_CN;
    // 明细表
    if (inspections.length) {
      const rows = inspections.map((i) => {
        const tds = [WB.h('td', { class: 'num', text: i.sn }),
          WB.h('td', { text: sn[i.intake_status] || i.intake_status || '—' })];
        const dec = i.decision_status;
        tds.push(WB.h('td', {}, WB.h('span', {
          class: 'tag ' + (dec === 'pass' ? 'tag-pass' : dec === 'fail' ? 'tag-fail' : 'tag-warn'),
          text: sn[dec] || dec || '—' })));
        tds.push(WB._evidenceTd(i.decision_evidence));
        return WB.h('tr', {}, tds);
      });
      body.append(WB.h('table', {},
        WB.h('tr', {}, WB.h('th', { text: '组件条码' }), WB.h('th', { text: '入场状态' }),
          WB.h('th', { text: '判定' }), WB.h('th', { text: '规则结果' })),
        rows));
    } else {
      body.append(WB.h('div', { class: 'empty', text: '暂无判定明细（尚未完成入场）' }));
    }
    // 抽样结论
    const sample = batch.sample_result;
    if (sample) body.append(WB.h('div', { class: 'hint-line' },
      WB.h('span', { text: '抽样结论 · ' }),
      WB.h('span', { class: 'tag ' + (sample === 'pass' ? 'tag-pass' : 'tag-fail'), text: sn[sample] || sample })));

    // 重新判定按钮
    const errSlot = WB.h('div', {});
    body.append(errSlot);
    body.append(WB.h('div', { class: 'actions' },
      WB.h('button', { class: 'primary', onclick: () => rejudge(batch, errSlot), text: '重新判定' })));
    card._errSlot = errSlot;
    return card;
  }

  function rejudge(batch, errSlot) {
    errSlot.replaceChildren();
    WB.api.runJudge(batch.batch_id).then(function (res) {
      if (!res.ok) {
        const e = res.error;
        if (e && e.detail && Array.isArray(e.detail.unconfirmed_sns) && e.detail.unconfirmed_sns.length) {
          errSlot.replaceChildren(
            WB.h('div', { class: 'form-err' },
              WB.h('div', { text: '请先完成入场确认：以下组件尚未确认' }),
              WB.h('ul', {}, e.detail.unconfirmed_sns.map((s) => WB.h('li', { text: s })))));
        } else {
          errSlot.append(WB.h('div', { class: 'form-err', text: WB.errText(e) }));
        }
        return;
      }
      // 成功：刷新批次详情重渲染（spec §4.4 不改状态）
      WB.toast('已重新判定：' + (res.data.result === 'Pass' ? '合格' : '不合格'));
      WB._loadDetail(batch.batch_id);
    });
  }

  return { stageIndex: stageIndex, render: render };
})();

/* 规则证据单元格：decision_evidence 为逐条规则结果（规则名/结论） */
if (!WB._evidenceTd) {
  WB._evidenceTd = function (ev) {
    const td = WB.h('td', {});
    const items = Array.isArray(ev) ? ev : (ev ? [ev] : []);
    if (!items.length) { td.textContent = '—'; return td; }
    items.forEach((it) => {
      if (it && typeof it === 'object') {
        const pass = it.passed !== false;
        td.append(WB.h('div', {},
          WB.h('span', { class: 'tag ' + (pass ? 'tag-pass' : 'tag-fail'), text: (it.rule || it.name || '规则') + ' · ' + (pass ? '通过' : '未通过') }),
          it.detail ? WB.h('span', { class: 'muted', text: it.detail }) : null));
      } else {
        td.append(WB.h('div', { text: String(it) }));
      }
    });
    return td;
  };
}

WB.stages._byIndex[1] = WB.stages.judge;
