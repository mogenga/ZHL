/* WB.report — 报告抽屉渲染（T9）：拉取完整报告并渲染 Markdown + 关键块
   取代 main.js 中 _reportDrawer 的占位实现；报告内容来自 GET /reports/{id}
   的 content_md / content_json（spec §3 事实表）。渲染为只读 DOM，无原生对话框。 */
'use strict';

WB.report = (function () {
  /* ---- 轻量 Markdown→DOM（标题/列表/粗体/斜体/行内代码/围栏代码/表格/
     PASS·FAIL 徽标），不引外部库。行内样式先转义再替换，安全。 ---- */
  function _esc(t) { return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  // 行内：粗体/斜体/行内代码；返回 innerHTML 字符串（输入已转义）
  function _inlineHtml(text) {
    let out = _esc(text);
    out = out.replace(/`([^`]+)`/g, function (_, c) { return '<code>' + c + '</code>'; });
    out = out.replace(/\*\*([^*]+)\*\*/g, function (_, b) { return '<strong>' + b + '</strong>'; });
    out = out.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, function (_, pre, it) { return pre + '<em>' + it + '</em>'; });
    return out;
  }
  // 列表项里的 [PASS] / [FAIL] 徽标着色
  function _liBadge(el) {
    const t = el.textContent || '';
    if (/^\[PASS\]/.test(t)) { el.classList.add('li-pass'); el.textContent = t.slice(6); }
    else if (/^\[FAIL\]/.test(t)) { el.classList.add('li-fail'); el.textContent = t.slice(6); }
  }
  function mdToDom(md) {
    const wrap = WB.h('div', { class: 'report-md' });
    if (!md) { wrap.append(WB.h('div', { class: 'empty', text: '暂无报告正文' })); return wrap; }
    // 头部准化：压缩当前报告文本里的多余空行与纯装饰行（仅由 — – - * : . · | 空格/全角空格
    // 组成的“无字母数字”行），避免前导大片空白在渲染后撑出“一大片空圆点/空段落”的视觉。
    const raw = String(md).split(/\r?\n/);
    // 把全角空格等当作普通空格处理，避免看似空行实则有非标准可见字符产出灰点
    const norm = raw.map(function (l) {
      return l.replace(/[\u00a0\u2007\u2009\u200b\u202f\ufeff]/g, ' ').trim();
    });
    // 强制规则：仅由装饰字符（em-dash / en-dash / hyphen / asterisk / 冒号 / 点 / 中点 / 管符 / 空格）
    // 组成的“无字母数字”行视为装饰分隔线，当空行处理；内含任何可见内容的行一律保留。
    const lines = norm.filter(function (l) { return l !== '' && !/^[\u2014\u2013\-\*\.:\u00b7|\s\u3000]+$/.test(l); });
    let i = 0;
    while (i < lines.length) {
      const ln = lines[i];
      // 围栏代码块
      if (/^\s*```/.test(ln)) {
        const buf = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i++; }
        if (i < lines.length) i++;
        wrap.append(WB.h('pre', { class: 'md-code' }, WB.h('code', { text: buf.join('\n') })));
        continue;
      }
      // 表格（GFM：表头 + |---| 分隔行）
      if (/^\s*\|/.test(ln) && i + 1 < lines.length && /^\s*\|?[\s|:-]+\|\s*$/.test(lines[i + 1]) && /-/.test(lines[i + 1])) {
        const headCells = ln.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) { return c.trim(); });
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) {
          rows.push(lines[i].trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) { return c.trim(); }));
          i++;
        }
        const tbl = WB.h('table', { class: 'md-table' });
        tbl.append(WB.h('tr', {}, headCells.map(function (h) { return WB.h('th', { innerHTML: _inlineHtml(h) }); })));
        rows.forEach(function (r) {
          tbl.append(WB.h('tr', {}, r.map(function (c) { return WB.h('td', { innerHTML: _inlineHtml(c) }); })));
        });
        wrap.append(tbl);
        continue;
      }
      if (/^#{1,6}\s/.test(ln)) {
        const lvl = Math.min(ln.match(/^#+/)[0].length, 4);
        wrap.append(WB.h('h' + lvl, { innerHTML: _inlineHtml(ln.replace(/^#+\s*/, '')) }));
        i++; continue;
      }
      // 引用
      if (/^\s*>/.test(ln)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
        wrap.append(WB.h('blockquote', { innerHTML: buf.map(_inlineHtml).join('<br>') }));
        continue;
      }
      if (/^\s*[-*]\s/.test(ln)) {
        // 先看完整列表段：裸子项（“-” 后无内容）视为装饰分隔线跳过，
        // 避免报告正文里的准备段掉落字符产出数颗空项
        const ul = WB.h('ul', {});
        let items = 0;
        while (i < lines.length && /^\s*[-*]\s/.test(lines[i])) {
          const text = lines[i].replace(/^\s*[-*]\s?/, '');
          if (!/^\s*$/.test(text)) {
            const item = WB.h('li', { innerHTML: _inlineHtml(text) });
            _liBadge(item);
            ul.append(item);
            items++;
          }
          i++;
        }
        if (items) wrap.append(ul);
        continue;
      }
      if (/^\s*\d+\.\s/.test(ln)) {
        const ol = WB.h('ol', {});
        let items = 0;
        while (i < lines.length && /^\s*\d+\.\s/.test(lines[i])) {
          const text = lines[i].replace(/^\s*\d+\.\s?/, '');
          if (!/^\s*$/.test(text)) {
            const item = WB.h('li', { innerHTML: _inlineHtml(text) });
            _liBadge(item);
            ol.append(item);
            items++;
          }
          i++;
        }
        if (items) wrap.append(ol);
        continue;
      }
      // 水平线
      if (/^\s*(---+|\*\*\*+)\s*$/.test(ln)) {
        wrap.append(WB.h('hr'));
        i++; continue;
      }
      if (/^\s*$/.test(ln)) { i++; continue; }
      wrap.append(WB.h('p', { innerHTML: _inlineHtml(ln) }));
      i++;
    }
    return wrap;
  }

  /* ---- 关键数据块：默认折叠为人类可读摘要，原始 JSON 收进「查看原始数据」，
     避免整页被紧凑 JSON 淹没 ---- */
  function _fmtJsonVal(v) {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'object') {
      try { return JSON.stringify(v); } catch (e) { return String(v); }
    }
    return String(v);
  }
  // 人类可读摘要：只展示有信息量的顶层键
  function _summaryRows(contentJson) {
    const rows = [];
    const batch = contentJson.batch;
    if (batch) {
      if (batch.model) rows.push(['型号', batch.model]);
      if (batch.status) rows.push(['批次状态', batch.status]);
      if (batch.batch_id) rows.push(['批次号', batch.batch_id]);
    }
    const inc = contentJson.incidents || [];
    const rei = contentJson.reinspections || [];
    const dec = contentJson.decisions || [];
    const asserts = contentJson.assertions;
    const checks = (asserts && asserts.checks) || [];
    const nPass = checks.filter(function (c) { return c.passed; }).length;
    if (checks.length) rows.push(['放行断言', nPass + '/' + checks.length + ' 项通过' + (asserts && asserts.passed === false ? '（未通过）' : '（全部通过）')]);
    if (inc.length) rows.push(['异常', inc.length + ' 条']);
    if (rei.length) rows.push(['复检', rei.length + ' 条']);
    if (dec.length) rows.push(['决策记录', dec.length + ' 条']);
    if (contentJson.refusal_reason) rows.push(['拒绝原因', contentJson.refusal_reason]);
    if (contentJson.report_type) rows.push(['报告类型', contentJson.report_type]);
    return rows;
  }
  // 可折叠：默认收起摘要，点标题展开；「查看原始数据」再展开紧凑 JSON
  function jsonBlock(contentJson) {
    if (!contentJson || !Object.keys(contentJson).length) return null;
    const box = WB.h('div', { class: 'report-json', style: 'margin-top:12px' });
    const head = WB.h('div', { class: 'jblock-head', style: 'display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none' },
      WB.h('span', { class: 'jblock-title', style: 'font-weight:600', text: '关键数据' }),
      WB.h('span', { class: 'jblock-caret', text: '▾' }));
    const collapse = WB.h('div', { class: 'jblock-body' });
    const summary = WB.h('div', { class: 'jblock-summary' });
    const srows = _summaryRows(contentJson);
    if (srows.length) {
      const stbl = WB.h('table', { class: 'md-table' });
      stbl.append(WB.h('tr', {}, WB.h('th', { text: '项' }), WB.h('th', { text: '值' })));
      srows.forEach(function (r) {
        stbl.append(WB.h('tr', {}, WB.h('td', { text: r[0] }), WB.h('td', { class: 'num', text: r[1] })));
      });
      summary.append(stbl);
    } else {
      summary.append(WB.h('div', { class: 'hint-line muted', text: '无摘要项，可展开原始数据查看完整字段' }));
    }
    collapse.append(summary);
    // 「查看原始数据」入口
    const rawBtn = WB.h('button', { class: 'newbtn jblock-raw', style: 'font-size:12px;padding:2px 10px;margin-top:8px', text: '查看原始数据' });
    const rawBox = WB.h('div', { class: 'jblock-rawbox', style: 'display:none' });
    const rawTbl = WB.h('table', { class: 'md-table jblock-raw-table' });
    rawTbl.append(WB.h('tr', {}, WB.h('th', { text: '项' }), WB.h('th', { text: '值' })));
    Object.keys(contentJson).forEach((k) => {
      const v = contentJson[k];
      const isObj = v !== null && typeof v === 'object';
      const td = WB.h('td', { class: isObj ? 'json-cell' : 'num', text: _fmtJsonVal(v) });
      rawTbl.append(WB.h('tr', {}, WB.h('td', { text: k }), td));
    });
    rawBox.append(rawTbl);
    rawBtn.addEventListener('click', function () {
      if (rawBox.style.display !== 'none') {
        rawBox.style.display = 'none';
        rawBtn.textContent = '查看原始数据';
      } else {
        rawBox.style.display = '';
        rawBtn.textContent = '收起原始数据';
      }
    });
    collapse.append(rawBtn);
    collapse.append(rawBox);
    head.addEventListener('click', function () {
      const caret = head.querySelector('.jblock-caret');
      if (collapse.style.display !== 'none') {
        collapse.style.display = 'none';
        if (caret) caret.textContent = '▸';
      } else {
        collapse.style.display = '';
        if (caret) caret.textContent = '▾';
      }
    });
    box.append(head);
    box.append(collapse);
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
