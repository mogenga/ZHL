/* WB.report — 报告抽屉渲染（T9）：拉取完整报告并渲染 Markdown + 关键块
   取代 main.js 中 _reportDrawer 的占位实现；报告内容来自 GET /reports/{id}
   的 content_md / content_json（spec §3 事实表）。渲染为只读 DOM，无原生对话框。 */
'use strict';

WB.report = (function () {
  // 轻量 Markdown→DOM（标题/列表/粗体/代码/表格）；不引外部库
  function mdToDom(md) {
    const wrap = WB.h('div', { class: 'report-md' });
    if (!md) { wrap.append(WB.h('div', { class: 'empty', text: '暂无报告正文' })); return wrap; }
    const lines = md.split(/\r?\n/);
    let i = 0;
    while (i < lines.length) {
      const ln = lines[i];
      if (/^#{1,6}\s/.test(ln)) {
        const lvl = Math.min(ln.match(/^#+/)[0].length, 4);
        wrap.append(WB.h('h' + lvl, { text: ln.replace(/^#+\s*/, '') }));
        i++; continue;
      }
      if (/^\s*[-*]\s/.test(ln)) {
        const ul = WB.h('ul', {});
        while (i < lines.length && /^\s*[-*]\s/.test(lines[i])) {
          ul.append(WB.h('li', { text: lines[i].replace(/^\s*[-*]\s*/, '') }));
          i++;
        }
        wrap.append(ul); continue;
      }
      if (/^\s*$/.test(ln)) { i++; continue; }
      wrap.append(WB.h('p', { text: ln }));
      i++;
    }
    return wrap;
  }

  // 关键数据块（content_json 里若有 checks/summary 等）
  function jsonBlock(contentJson) {
    if (!contentJson || !Object.keys(contentJson).length) return null;
    const box = WB.h('div', { class: 'report-json', style: 'margin-top:12px' });
    box.append(WB.h('div', { class: 'hint-line', style: 'font-weight:600', text: '关键数据' }));
    const tbl = WB.h('table', {});
    const head = WB.h('tr', {}, WB.h('th', { text: '项' }), WB.h('th', { text: '值' }));
    tbl.append(head);
    Object.keys(contentJson).forEach((k) => {
      const v = contentJson[k];
      const vs = (v === null || v === undefined) ? '—' : (typeof v === 'object' ? JSON.stringify(v) : String(v));
      tbl.append(WB.h('tr', {}, WB.h('td', { text: k }), WB.h('td', { class: 'num', text: vs })));
    });
    box.append(tbl);
    return box;
  }

  // 打开报告抽屉：先展示骨架，再异步拉完整内容填充
  function open(reportRef) {
    const r = reportRef;
    const title = ((WB.REP_CN || {})[r.report_type] || '报告') + ' · ' + (r.batch_no || (r.batch_id || '').slice(0, 8));
    const body = WB.h('div', {});
    body.append(WB.h('div', { class: 'hint-line' },
      WB.h('span', { class: 'tag', text: ((WB.REP_CN || {})[r.report_type] || '报告') }),
      WB.h('span', { class: 'muted', text: ' · 版本 v' + (r.version || 1) + ' · ' + (WB.fmtTime(r.generated_at) || '') })));
    const content = WB.h('div', { class: 'report-body' }, WB.h('div', { class: 'empty', text: '正在加载报告正文…' }));
    body.append(content);

    WB.openDrawer(title, body);

    // 已有完整内容（来自 list 的 content_md / 拉取的 GET 结果）直接渲染；
    // 其他情况（单元仅 batch_id+report_id）按需拉取完整报告后再渲染
    if (r.content_md || r.content_json) {
      _fill(content, r);
    } else {
      WB.api.getReport(r.batch_id, r.report_id).then(function (res) {
        if (!res.ok) {
          content.replaceChildren(WB.h('div', { class: 'form-err', text: WB.errText(res.error) }));
          return;
        }
        res.data.content_md = res.data.content_md;
        res.data.content_json = res.data.content_json;
        _fill(content, res.data);
      });
    }
  }

  function _fill(content, r) {
    content.replaceChildren();
    content.append(mdToDom(r.content_md));
    const jb = jsonBlock(r.content_json);
    if (jb) content.append(jb);
  }

  // 报告正文在主区展示（工作台）：非侧边抽屉，直接渲染到指定容器，正文不被压缩到少量空间
  function render(body, r) {
    body.replaceChildren();
    body.append(WB.h('div', { class: 'hint-line' },
      WB.h('span', { class: 'tag', text: ((WB.REP_CN || WB.RPT_CN || {})[r.report_type] || '报告') }),
      WB.h('span', { class: 'muted', text: ' · 版本 v' + (r.version || 1) + ' · ' + (WB.fmtTime(r.generated_at) || '') })));
    const content = WB.h('div', { class: 'report-body', style: 'margin-top:8px' });
    if (r.content_md != null || r.content_json != null) {
      _fill(content, r);
    } else {
      content.append(WB.h('div', { class: 'empty', text: '正文加载中…' }));
      WB.api.getReport(r.batch_id, r.report_id).then(function (res) {
        if (!res.ok) {
          content.replaceChildren(WB.h('div', { class: 'form-err', text: WB.errText(res.error) || '报告加载失败' }));
          return;
        }
        _fill(content, res.data);
      });
    }
    body.append(content);
  }

  return { open: open, render: render, mdToDom: mdToDom };
})();

// 覆盖 main.js 的占位 _reportDrawer
WB._reportDrawer = function (r) {
  if (WB.report) WB.report.open(r);
  else WB.toast('报告渲染模块未加载');
};

// 报告视图下的“生成报告”动作（由批次详情/放行节点触发）
// 工作台主区展示报告正文：在主区「报告」视图卡片下方追加正文区（不用右侧抽屉，避开正文被压缩）
WB._expandReportInMain = function (report, batch) {
  if (WB.state.view !== 'reports') return;
  const main = WB.el('mainPane');
  const page = main.querySelector('.report-view-page');
  if (!page) return;
  const host = page.querySelector('.report-detail-host');
  if (host) host.remove();
  const batchNo = (batch && batch.batch_no) || (report.batch_no) || '';
  const detail = WB.h('div', { class: 'report-detail-host card stage-panel', style: 'margin-top:10px' });
  detail.append(WB.h('h3', { text: '报告正文 · ' +
    ((WB.REP_CN || WB.RPT_CN || {})[report.report_type] || '报告') +
    (batchNo ? ' · ' + batchNo : '') }));
  const body = WB.h('div', {});
  detail.append(body);
  if (WB.report && WB.report.render) WB.report.render(body, report);
  else if (report.content_md != null) {
    body.append(WB.h('pre', { text: report.content_md,
      style: 'white-space:pre-wrap;font-size:13px' }));
  } else {
    WB.api.getReport(report.batch_id, report.report_id).then(function (res) {
      if (!res.ok) {
        body.replaceChildren(WB.h('div', { class: 'form-err', text: WB.errText(res.error) || '报告加载失败' }));
        return;
      }
      body.replaceChildren(WB.h('pre', { text: res.data.content_md,
        style: 'white-space:pre-wrap;font-size:13px' }));
    });
  }
  page.append(detail);
};

WB.generateReportForBatch = function (batch) {
  const types = [
    { value: 'release', label: '放行报告' },
    { value: 'incident', label: '异常处置报告' },
  ];
  WB.openModal('生成报告 · ' + batch.batch_no, [
    { label: '报告类型', type: 'select', value: 'release', options: types },
  ], function (vals) {
    WB.api.generateReport(batch.batch_id, vals[0]).then(function (res) {
      if (!res.ok) { WB.toast(WB.errText(res.error)); return; }
      WB.toast('报告已生成');
      // 刷新批次（可能触发自动归档）；报告正文在工作台主区展示，不压缩到侧边抽屉
      WB._loadDetail(batch.batch_id);
      WB.loadBatches();
      if (WB.state.view === 'reports') {
        // 工作台报告页重渲染：新报告立即出现在主区列表，并扩展新报告正文
        WB._renderCenterAndRight();
        WB._expandReportInMain(res.data, batch);
      } else if (WB._reportDrawer) {
        WB._reportDrawer(res.data);
      }
    });
  }, { submitLabel: '生成' });
};
