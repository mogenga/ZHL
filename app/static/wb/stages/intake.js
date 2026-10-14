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
      { label: '检测数据（每行一条，9 列：SN,pmax,temp,isc,voc,vm,im,ff,外观）', type: 'textarea', rows: 10,
        placeholder: 'SN-N-001,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass\nSN-N-002,548.0,25.0,10.5,48.0,40.0,13.0,0.80,pass',
        required: true },
      { label: '人工覆盖字段（可选）', type: 'text', placeholder: 'pmax=24.1, appearance_result=pass' },
    ];
    WB.openModal('提交送检数据', fields, function (vals) {
      const source_type = vals[0];
      const raw = (vals[1] || '').trim();
      const overrideStr = (vals[2] || '').trim();
      if (!raw) { WB.toast('请填写检测数据'); return; }
      // 多行 → inspections[]；每行必须含 9 列（SN + 7 电参 + 外观），缺列/非数报错
      const cols = ['sn', 'pmax', 'temp', 'isc', 'voc', 'vm', 'im', 'ff', 'appearance_result'];
      const inspections = [];
      let err = null;
      raw.split(/\r?\n/).map((l) => l.trim()).filter(Boolean).forEach((line) => {
        const parts = line.split(/[，,]/).map((x) => x.trim());
        if (parts.length !== 9) { err = err || ('行「' + line + '」应为 9 列，当前 ' + parts.length + ' 列'); return; }
        const row = { sn: parts[0], source_type: source_type, captured_at: new Date().toISOString() };
        for (let i = 1; i <= 7; i++) {
          const v = Number(parts[i]);
          if (parts[i] === '' || Number.isNaN(v)) { err = err || ('行「' + line + '」第 ' + (i + 1) + ' 列非数字：' + parts[i]); return; }
          row[cols[i]] = v;
        }
        if (parts[8] !== 'pass' && parts[8] !== 'fail') { err = err || ('行「' + line + '」外观须为 pass/fail：' + parts[8]); return; }
        row.appearance_result = parts[8];
        inspections.push(row);
      });
      if (err) { WB.toast(err); return; }
      if (!inspections.length) { WB.toast('没有可解析的检测数据行'); return; }
      const fieldsOverride = parseOverrides(overrideStr);
      WB.api.submitIntake({
        batch_id: batch.batch_id,
        source_type: source_type,
        inspections: inspections,
        fields: fieldsOverride || null,
      }).then(function (res) {
        if (!res.ok) {
          WB.toast(WB.errText(res.error));
          // 失败也刷新，避免界面停在旧状态（如批次已推进/已有行）被重复提交
          WB._loadDetail(batch.batch_id);
          return;
        }
        WB.toast('提交成功，批次已推进：' + _progressText(res.data));
        // 刷新批次头/流水线/右栏进度，跳到新阶段；再在中栏改写入场结果卡
        WB._loadDetail(batch.batch_id);
        WB.loadBatches();
        showIntakeResult(batch, res.data);
      });
    }, { submitLabel: '提交送检' });
    // 快速填入：IV 设备导出的 CSV 每行一条（浏览器解析为结构化行，后端零改动）；
    // 演示场景提供示例数据一键填入。
    const box = modal && modal.box;
    if (box) {
      const ta = box.querySelector('textarea');
      const taRow = ta ? ta.closest('.form-row') : null;
      if (taRow) {
        const fileInput = WB.h('input', {
          type: 'file', accept: '.csv,.txt', style: 'font-size:12px',
          onchange: function (ev) {
            const f = ev.target.files && ev.target.files[0];
            if (!f) return;
            const reader = new FileReader();
            reader.onload = function () {
              ta.value = String(reader.result || '').replace(/^\uFEFF/, '').trim();
              const n = ta.value.split(/\r?\n/).filter(Boolean).length;
              WB.toast('已载入 ' + f.name + '（' + n + ' 行）');
            };
            reader.readAsText(f, 'utf-8');
          },
        });
        const exampleBtn = WB.h('button', { onclick: function () {
          const lines = [];
          for (let i = 1; i <= 3; i++) {
            lines.push('SN-DEMO-' + String(i).padStart(3, '0') + ',550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass');
          }
          ta.value = lines.join('\n');
        }, text: '填入示例（3 条合格）' });
        taRow.parentNode.insertBefore(
          WB.h('div', { class: 'form-row' },
            WB.h('label', { text: '快速填入' }),
            fileInput, exampleBtn),
          taRow);
      }
    }
  }

  /* 发饌进度提示：处理状态 + 下一步动作（依 WB.ST_CN / WB.NEXT_LABEL） */
  function _progressText(data) {
    var pending = data && data.status === 'pending';
    var next = pending ? '人工确认入场' : ((WB.NEXT_LABEL || {})[pending ? 'pending_judge' : 'pending_judge'] || '');
    if (pending) return '批次待处理：待人工确认入场→判定';
    return '已自动确认入场，批次转待判定，下一步：' + (WB.NEXT_LABEL['pending_judge'] || '查看判定');
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
    // 顶部流程进度提示（使用者不需看后台日志即可知批次当前处于哪一步）
    body.append(WB.h('div', { class: 'hint-line', style: 'font-weight:600' },
      WB.h('span', { class: 'tag ' + (data.status === 'confirmed' ? 'tag-pass' : 'tag-warn'),
        text: _progressText(data) })));
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
