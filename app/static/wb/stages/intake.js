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
      { label: '录入方式', type: 'select', value: 'raw', options: [
        { value: 'raw', label: '原始单据（AI 抽取）' },
        { value: 'structured', label: '已结构化数据（逐列粘贴）' },
      ] },
      { label: '检测数据（每行一条，9 列：SN,pmax,temp,isc,voc,vm,im,ff,外观）', type: 'textarea', rows: 10,
        placeholder: 'SN-N-001,550.0,25.0,10.5,48.0,40.0,13.0,0.80,pass\nSN-N-002,548.0,25.0,10.5,48.0,40.0,13.0,0.80,pass',
        required: true },
      { label: '人工覆盖字段（可选）', type: 'text', placeholder: 'pmax=24.1, appearance_result=pass' },
    ];
    WB.openModal('提交送检数据', fields, function (vals) {
      const source_type = vals[0];
      const mode = vals[1];
      const raw = (vals[2] || '').trim();
      const overrideStr = (vals[3] || '').trim();
      if (!raw) { WB.toast('请填写检测数据'); return; }
      const fieldsOverride = parseOverrides(overrideStr);

      // 原始单据通道（FR-1.1/1.2）：整段原文交给后端 AI 抽取 + 校验（FR-1.4），
      // 前端不再自行拆列——那才是「系统读懂原始单据」的演示路径。
      if (mode === 'raw') {
        _markModalBusy();
        WB.api.submitIntake({
          batch_id: batch.batch_id,
          source_type: source_type,
          raw_source: raw,
          raw_source_kind: source_type === 'OCR_FORM' ? 'paper_form' : 'iv_csv',
          fields: fieldsOverride || null,
        }).then(_onIntakeSubmitted(batch));
        return;
      }

      // 已结构化通道：前端拆列后按原有 9 列契约提交。
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
      _markModalBusy();
      WB.api.submitIntake({
        batch_id: batch.batch_id,
        source_type: source_type,
        inspections: inspections,
        fields: fieldsOverride || null,
      }).then(_onIntakeSubmitted(batch));
    }, { submitLabel: '提交送检' });
  }

  /* 提交回调：成功/失败都刷新详情，避免界面停在旧状态 */
  function _onIntakeSubmitted(batch) {
    return function (res) {
        if (!res.ok) {
          _unmarkModalBusy();
          WB.toast(WB.errText(res.error));
          // 失败也刷新，避免界面停在旧状态（如批次已推进/已有行）被重复提交
          WB._loadDetail(batch.batch_id);
          WB.loadBatches();
          return;
        }
        // 成功：后台可能仍在跑图（判定/抽样），不再一次性等待到底，
        // 展示初始结果并轮询批次状态直到离开 pending_intake（前端可视化处理进度）
        WB.toast('提交成功，批次已推进：' + _progressText(res.data));
        WB._loadDetail(batch.batch_id);
        WB.loadBatches();
        showIntakeResult(batch, res.data);
        _pollUntilLeftIntake(batch);
    };
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
    // 原始单据解析结果（FR-1.1/1.2/1.4）：本次解析出多少行、哪些行被校验挡下
    const rp = data.raw_parse;
    if (rp) {
      body.append(WB.h('div', { class: 'hint-line', text: '原始单据解析：' + rp.rows + ' 行有效' }));
      (rp.issues || []).forEach(function (iss) {
        body.append(WB.h('div', { class: 'hint-line', text: '· 已拦截 ' + (iss.sn || '?') + ' / ' + iss.field + '：' + iss.problem }));
      });
    }
    // 抽取字段对照（按组件逐行：SN + 置信度 + 字段值）
    const ex = data.extracted_fields || {};
    const sns = Object.keys(ex);
    if (sns.length) {
      body.append(WB.h('div', { class: 'hint-line', style: 'font-weight:600', text: 'AI 抽取结果（逐组件）' }));
      sns.forEach(function (sn) {
        const r = ex[sn] || {};
        const conf = r.confidence != null ? Math.round(r.confidence * 100) + '%' : '—';
        const lowConf = r.confidence != null && r.confidence < 0.8;
        const head = WB.h('div', { class: 'hint-line' },
          WB.h('span', { class: 'tag ' + (lowConf ? 'tag-warn' : 'tag-pass'), text: sn + ' · ' + conf }));
        body.append(head);
        const fields = r.fields || {};
        const rows = Object.keys(fields).map((k) => WB.h('tr', {},
          WB.h('td', { text: k }), WB.h('td', { class: 'num', text: String(fields[k]) })));
        if (rows.length) {
          body.append(WB.h('table', {}, rows));
        }
        (r.appearance_findings || []).forEach(function (f) {
          body.append(WB.h('div', { class: 'hint-line', text: '外观发现：' + f }));
        });
      });
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
      // 确认/拒绝后后端图同步推进（判定/抽样）→ 刷新批次跟随新状态（spec §4.3）；
      // 推进到隔离时后端会同事务自动建异常，异常列表必须同步刷新，
      // 否则面板停留在「尚无异常记录」误导再建（重复单缺陷，2026-10）
      WB.loadIncidents();
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

  /* 提交按钮忙状态：提交中禁用并显示处理进度，防止连点重复提交 */
  function _markModalBusy() {
    if (!WB._modal) return;
    WB._modal._busy = true;
    const btns = WB._modal.box.querySelectorAll('.foot button');
    if (btns.length > 1) {
      btns[1].textContent = '正在提交…';
      btns[1].disabled = true;
    }
  }
  function _unmarkModalBusy() {
    if (WB._modal && WB._modal._busy) WB._modal._busy = false;
  }

  /* 轮询批次状态直到离开 pending_intake（后台图仍在跑判定/抽样）；
     每次间隔 2s，上限 60 次；离开后最终刷新一次批次详情 + 侧栏。 */
  function _pollUntilLeftIntake(batch) {
    let tries = 0;
    const maxTries = 60;
    const timer = setInterval(function () {
      tries++;
      WB.api.getBatch(batch.batch_id).then(function (res) {
        const st = res.ok && res.data ? res.data.status : null;
        if (st && st !== 'pending_intake') {
          clearInterval(timer);
          // 离开入场（自动确认→判定→可能隔离+自动建异常）：异常列表一并刷新
          WB.loadIncidents();
          WB._loadDetail(batch.batch_id);
          WB.loadBatches();
          return;
        }
        if (tries >= maxTries) {
          clearInterval(timer);
          WB.toast('批次处理仍在进行中，当前状态：' + (st || '未知'));
        }
      });
    }, 2000);
  }

  return { stageIndex: stageIndex, render: render };
})();

WB.stages._byIndex[0] = WB.stages.intake;
