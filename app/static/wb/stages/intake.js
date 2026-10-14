/* WB.stages.intake — 数据入场节点（stageIndex 0）
   提交送检数据（spec §4.2）→ 等待窗（转圈 + 计时；超时自动切轮询，2026-10-15
   「提交送检之后老是超时」）→ 展示置信度/低置信清单/抽取字段；
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
      // 校验失败必须 return false：onOk 返回 undefined 会被 ui.js 当作放行而关窗，
      // 用户粘的数据全部丢失（「填了 10 行、少 1 列、整窗消失」）
      if (!raw) { WB.toast('请填写检测数据'); return false; }
      const fieldsOverride = parseOverrides(overrideStr);

      // 原始单据通道（FR-1.1/1.2）：整段原文交给后端 AI 抽取 + 校验（FR-1.4），
      // 前端不再自行拆列——那才是「系统读懂原始单据」的演示路径。
      if (mode === 'raw') {
        return _intakeSubmit({
          batch_id: batch.batch_id,
          source_type: source_type,
          raw_source: raw,
          raw_source_kind: source_type === 'OCR_FORM' ? 'paper_form' : 'iv_csv',
          fields: fieldsOverride || null,
        }, batch);
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
      if (err) { WB.toast(err); return false; }
      if (!inspections.length) { WB.toast('没有可解析的检测数据行'); return false; }
      return _intakeSubmit({
        batch_id: batch.batch_id,
        source_type: source_type,
        inspections: inspections,
        fields: fieldsOverride || null,
      }, batch);
    }, { submitLabel: '提交送检' });
  }

  /* ---- 从 CSV 导入（仪器导出的检测数据本来就是 CSV）----
     流程：选文件 → 前端解析 → 预览行数/SN/问题行 → 确认导入 →
     与粘贴通道同一个 submitIntake 契约（inspections[]），后端零改动。
     有错整批不提交（与粘贴通道同策略）：质量数据部分导入比不导入更危险。 */
  const IMPORT_SOURCE_TYPE = 'IV_CSV';
  const TEMPLATE_URL = '/static/templates/inspection_template.csv';

  function importForm(batch) {
    const state = { parsed: null };
    const preview = WB.h('div', { class: 'import-preview' },
      WB.h('div', { class: 'hint-line muted', text: '选择文件后在此显示解析预览' }));
    const inline = WB.h('div', {},
      WB.h('div', { class: 'hint-line' },
        WB.h('a', { href: TEMPLATE_URL, download: 'inspection_template.csv', text: '下载模板' }),
        WB.h('span', { class: 'muted', text: '（9 列表头 + 示例行；外观列填 pass / fail）' })),
      preview);
    WB.openModal('从 CSV 导入检测数据', [
      { label: '选择文件（.csv / .txt；UTF-8 或 GBK）', type: 'file', accept: '.csv,.txt' },
    ], function () {
      if (!state.parsed || !state.parsed.rows.length) {
        WB.toast('请先选择能解析出检测数据的文件');
        return false;
      }
      if (state.parsed.errors.length) {
        WB.toast('文件中有 ' + state.parsed.errors.length + ' 行问题，请修正后重新导入');
        return false;
      }
      const now = new Date().toISOString();
      const inspections = state.parsed.rows.map((r) => Object.assign(
        { source_type: IMPORT_SOURCE_TYPE, captured_at: now }, r));
      return _intakeSubmit({
        batch_id: batch.batch_id,
        source_type: IMPORT_SOURCE_TYPE,
        inspections: inspections,
        fields: null,
      }, batch);
    }, { submitLabel: '确认导入', inline: inline });
    const input = WB._modal.inputs[0];
    if (input && input.addEventListener) {
      input.addEventListener('change', () => _readImportFile(input, state, preview, batch));
    }
    _setImportEnabled(false);   // 未选文件时不放行提交（避免空提交/误点）
  }

  function _readImportFile(input, state, preview, batch) {
    const file = (input.files || [])[0];
    if (!file) {
      state.parsed = null;
      _renderImportPreview(preview, null, '', batch);
      _setImportEnabled(false);
      return;
    }
    const fr = new FileReader();
    fr.onload = function () {
      const text = decodeCsvBytes(new Uint8Array(fr.result));
      const parsed = parseInspectionCsv(text);
      state.parsed = parsed;
      _renderImportPreview(preview, parsed, file.name, batch);
      _setImportEnabled(parsed.rows.length > 0 && parsed.errors.length === 0);
    };
    fr.readAsArrayBuffer(file);
  }

  function _renderImportPreview(preview, parsed, fileName, batch) {
    preview.replaceChildren();
    if (!parsed) return;
    preview.append(WB.h('div', { class: 'hint-line' },
      WB.h('span', { class: 'mono', text: fileName }),
      WB.h('span', { class: 'muted', text: ' · ' + parsed.rows.length + ' 行有效'
        + (parsed.errors.length ? '，' + parsed.errors.length + ' 行有问题' : '')
        + (parsed.skippedHeader ? '（已跳过表头行）' : '') })));
    // 计划数量 vs 实际行数：计划数量是抽样方案的 lot size 输入
    // （main_chain._lot_size 拿它查 ISO 表 → n/ac/re），行数才是实际入库并被
    // 判定的组件数——两者不校验、不比对，错配会静默按错误的批量抽样（还可能
    // 让报告里的 n 与实际只数不符）。这里**只提示、不阻断**：不改判定语义。
    const planned = Number(batch && batch.planned_quantity);
    if (parsed.rows.length > 0 && Number.isFinite(planned) && planned > 0
      && planned !== parsed.rows.length) {
      preview.append(WB.h('div', { class: 'hint-line' },
        WB.h('span', { class: 'tag tag-warn', text: '计划数量不一致' }),
        WB.h('span', { class: 'muted', text: ' CSV 有 ' + parsed.rows.length
          + ' 行，批次计划数量 ' + planned + '：抽样方案（n/ac/re）按计划数量计算，'
          + '请确认哪边填错' })));
    }
    if (parsed.sns.length) {
      const head = parsed.sns.slice(0, 10);
      preview.append(WB.h('div', { class: 'hint-line' },
        WB.h('span', { class: 'muted', text: 'SN：' + head.join('、')
          + (parsed.sns.length > head.length ? ' 等共 ' + parsed.sns.length + ' 条' : '') })));
    }
    if (parsed.errors.length) {
      const list = WB.h('div', { class: 'form-err' });
      parsed.errors.slice(0, 8).forEach((e) => list.append(WB.h('div', { text: e })));
      if (parsed.errors.length > 8) {
        list.append(WB.h('div', { text: '…还有 ' + (parsed.errors.length - 8) + ' 行问题' }));
      }
      preview.append(list);
    }
  }

  function _setImportEnabled(on) {
    if (WB._modal && WB._modal.submitBtn) WB._modal.submitBtn.disabled = !on;
  }

  /* ---- 提交（带等待动画 + 后台轮询）----
     POST /api/intake 在一个请求里同步跑完整条主链：原始单据解析 + 逐组件
     抽取都要现场调真模型，实测单次 40~90s 且逐组件累加；浏览器 fetch 没有无限等待，
     长时间无反馈就是「点了没反应」。策略：
     ① 提交后立即关表单、开「处理中」等待窗（转圈动画 + 阶段文案，提交中不可再点）；
     ② 等待窗独立计时，提交请求超时**不**视为失败——后端不随浏览器中断而停，
        跑完照常落库，此时改为轮询批次状态直到离开 pending_intake；
     ③ 提交成功或轮询追上结果，都关等待窗并输出入场结果（置信度/抽取值）。 */
  function _intakeSubmit(body, batch) {
    WB.closeModal();                       // 表单数据已进请求体，先关掉防重复编辑
    _openWaitModal(batch);
    return WB.api.submitIntake(body).then(function (res) {
      if (res.ok) { _finishWait(batch, res.data); return; }
      const e = res.error || {};
      // 超时 ≠ 失败：后端仍在跑，切轮询模式继续等（等待窗文案随阶段变化）
      if (e.key === 'timeout' || e.key === 'network') {
        _waitPhase('polling');
        _pollUntilLeftIntake(batch);
        return;
      }
      // 其余才是真失败（409 已入场 / 400 数据问题……），关窗如实报错
      _closeWaitModal();
      WB.toast(WB.errText(e));
      // 失败也刷新：批次可能已被先前一次超时的提交推进，界面不能停在旧状态
      WB._loadDetail(batch.batch_id);
      WB.loadBatches();
    });
  }

  /* ---- 等待窗：转圈动画 + 阶段文案 + 计时 ---- */
  function _openWaitModal(batch) {
    const tip = WB.h('div', { class: 'hint-line', text: '正在提交送检数据…' });
    const elapsed = WB.h('div', { class: 'hint-line muted', text: '' });
    // 复用 .modal 的居中/置顶样式（box 需 z-index 高过 mask，否则被遮罩盖住）
    const box = WB.h('div', { class: 'modal on' },
      WB.h('h3', { text: '处理中 · ' + batch.batch_no }),
      WB.h('div', { class: 'wait-body' },
        WB.h('div', { class: 'wb-spin' }), tip, elapsed));
    const ov = WB.h('div', { class: 'mask on' });
    document.body.append(ov, box);
    WB._waitModal = {
      ov: ov, box: box, tip: tip,
      start: Date.now(),
      timer: setInterval(function () {
        if (!WB._waitModal) return;
        const s = Math.round((Date.now() - WB._waitModal.start) / 1000);
        elapsed.textContent = '已等待 ' + s + ' 秒';
      }, 1000),
    };
    // 等待窗不提供关闭按钮：关闭就等于放弃跟随，用户会再点一次「提交」
    // 造成重复入场 409。轮询上限到点会自动收窗并提示。
  }

  /* 等待窗阶段文案：提交请求超时 → 改为轮询观察 */
  function _waitPhase(phase) {
    if (!WB._waitModal) return;
    if (phase === 'polling') {
      WB._waitModal.tip.textContent =
        '提交请求已超时，后台仍在处理（结果会落库），正在自动查询进度…';
    }
  }

  function _closeWaitModal() {
    if (!WB._waitModal) return;
    clearInterval(WB._waitModal.timer);
    WB._waitModal.ov.remove();
    WB._waitModal.box.remove();
    WB._waitModal = null;
  }

  /* 完成输出：关等待窗 + 展示入场结果卡（置信度/低置信清单/抽取字段）。
     pending 路径（低置信等人工确认）要先刷新再放卡片：确认/拒绝按钮要对着
     最新详情操作。degraded 标记超时后由轮询合成的结果（抽取明细只在提交
     响应里，补一句去向）。 */
  function _finishWait(batch, data) {
    const wasPending = data && data.status === 'pending';
    _closeWaitModal();
    WB.toast('提交成功，批次已推进：' + _progressText(data));
    if (wasPending) {
      // 先拉详情再挂结果卡：_loadDetail 的异步回调会整幅重画主区
      WB._loadDetail(batch.batch_id);
      WB.loadBatches();
      WB.api.getBatch(batch.batch_id).then(function () {
        showIntakeResult(batch, data);
      });
    } else {
      WB._loadDetail(batch.batch_id);
      WB.loadBatches();
      showIntakeResult(batch, data);
    }
    if (data && data.degraded) {
      WB.toast('已自动追上进度；本次为超时后合成结果，抽取明细可在判定节点「重新判定」查看');
    }
  }

  /* 发饌进度提示：处理状态 + 下一步动作（依 WB.ST_CN / WB.NEXT_LABEL） */
  function _progressText(data) {
    var pending = data && data.status === 'pending';
    var next = pending ? '人工确认入场' : ((WB.NEXT_LABEL || {})[pending ? 'pending_judge' : 'pending_judge'] || '');
    if (pending) return '批次待处理：待人工确认入场→判定';
    return '已自动确认入场，批次转待判定，下一步：' + (WB.NEXT_LABEL['pending_judge'] || '查看判定');
  }

  /* ---- CSV 解析（纯函数：无 DOM、无时钟）----
     列契约与「已结构化数据（逐列粘贴）」完全一致：
       SN,pmax,temp,isc,voc,vm,im,ff,外观
     - 表头行**仅当**首行形如 sn,pmax,… 时跳过，数据行不会被误判（首列是 SN-xxx）
     - 行号按文件原样（含表头）计数，便于在 Excel 里定位
     - 每个问题行都进 errors 且不产出该行；是否拦住提交由调用方决定
       （当前策略：有错整批不提交，与粘贴通道一致）
     - 文件内重复 SN 也在此拦下：后端只查"已入库的同批 SN"，载荷内重复会撞
       uq_inspection 变成 500，而不是可读的 400（app/api/intake.py 的重复防护
       只覆盖 existing_sns） */
  const CSV_COLS = ['sn', 'pmax', 'temp', 'isc', 'voc', 'vm', 'im', 'ff', 'appearance_result'];

  function parseInspectionCsv(text) {
    const out = { rows: [], sns: [], errors: [], skippedHeader: false, dataLines: 0 };
    const raw = String(text == null ? '' : text).replace(/^﻿/, '');
    const seen = {};
    raw.split(/\r?\n/).forEach(function (line, idx) {
      const n = idx + 1;
      const s = line.trim();
      if (!s) return;
      const parts = s.split(/[，,]/).map(function (x) { return x.trim(); });
      if (idx === 0 && parts[0].toLowerCase() === 'sn'
        && parts.some(function (p) { return p.toLowerCase() === 'pmax'; })) {
        out.skippedHeader = true;
        return;
      }
      out.dataLines++;
      if (parts.length !== 9) {
        out.errors.push('第 ' + n + ' 行：应为 9 列，当前 ' + parts.length + ' 列');
        return;
      }
      const row = { sn: parts[0] };
      for (let i = 1; i <= 7; i++) {
        const v = Number(parts[i]);
        if (parts[i] === '' || Number.isNaN(v)) {
          out.errors.push('第 ' + n + ' 行：' + CSV_COLS[i] + ' 不是数字（' + parts[i] + '）');
          return;
        }
        row[CSV_COLS[i]] = v;
      }
      if (parts[8] !== 'pass' && parts[8] !== 'fail') {
        out.errors.push('第 ' + n + ' 行：外观须为 pass/fail（当前 ' + parts[8] + '）');
        return;
      }
      row.appearance_result = parts[8];
      if (seen[row.sn]) {
        out.errors.push('第 ' + n + ' 行：SN ' + row.sn + ' 在文件内重复（第 ' + seen[row.sn] + ' 行已出现）');
        return;
      }
      seen[row.sn] = n;
      out.rows.push(row);
      out.sns.push(row.sn);
    });
    return out;
  }

  /* 文件字节 → 文本：UTF-8 优先（TextDecoder 默认剥 BOM），解出替换字符时回退
     GBK —— 中文 Windows 上 Excel 导出的 CSV 默认就是 GBK，按 UTF-8 读会整片乱码。 */
  function decodeCsvBytes(bytes) {
    const utf8 = new TextDecoder('utf-8').decode(bytes);
    if (utf8.indexOf('�') < 0) return utf8;
    try {
      const gbk = new TextDecoder('gbk').decode(bytes);
      if (gbk.indexOf('�') < 0) return gbk;
    } catch (e) { /* 环境无 gbk 解码器：保留 UTF-8 结果，问题行由预览指出 */ }
    return utf8;
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
    // 结果进状态而不是直接 append 到 mainPane：_loadDetail / loadBatches 的
    // 异步回调都会 replaceChildren 重画主区，直接 append 的卡片会被抹掉
    // （2026-10-15 反馈「提交巡检数据后无反馈」）。由 renderCenter 在画完
    // 节点面板后统一追加，任何一次重渲染都带着它。
    WB.state.intakeResult = { batch_id: batch.batch_id, data: data };
    WB._renderCenterAndRight();
  }
  /* 供 renderCenter 调用：把「入场结果」卡片 append 到已画好的主区末尾。 */
  function appendResultCard(mainPane, data) {
    const card = WB.h('div', { class: 'card stage-panel' });
    card.append(WB.h('h3', { text: '入场结果' }));
    const body = WB.h('div', {});
    card.append(body);
    mainPane.append(card);
    _renderIntakeBody(body, { batch_id: WB.state.batchId }, data);
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
      // 确认/拒绝后即时结果卡已完成使命，在此显式清除。
      // 卡片寿命现在**只**由生命周期决定（renderCenter 不再按批次状态过滤，
      // 否则自动确认那条路看不到卡片）——这里的清除就是它唯一的结束点之一，
      // 与换批次（selectBatch）、回总览（goHome）并列。
      WB.state.intakeResult = null;
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
      // 两条入口共用同一次提交：手工粘贴（原样）与文件导入（仪器导出的 CSV）
      body.append(WB.h('div', { class: 'actions' },
        WB.h('button', { class: 'primary', onclick: () => submitForm(batch), text: '提交送检数据' }),
        WB.h('button', { onclick: () => importForm(batch), text: '从 CSV 导入' })));
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

  /* 轮询批次状态直到离开 pending_intake（后台图仍在跑判定/抽样）；
     每次间隔 2s，上限 120 次（4 分钟，对齐后端真模型逐组件抽取的量级）。
     批次**留在** pending_intake 且检验行已入库 = 低置信等人工确认（不是还在
     跑图）：检验行已落库、详情能看到行数，此时收窗展示结果（pending 路径）。
     离开后：关窗 + 刷新详情/侧栏/异常列表（自动确认 → 判定 → 可能隔离+建异常）。
     情况 A：提交请求还在飞（等待窗在）——轮询只是后台旁路观察，提交返回时
       _finishWait 关窗，这里的观察发现已离开就静默停表；
     情况 B：提交请求已超时——本函数就是唯一的完成路径，负责关窗 + 输出。 */
  function _pollUntilLeftIntake(batch) {
    let tries = 0;
    const maxTries = 120;
    const timer = setInterval(function () {
      tries++;
      WB.api.getBatch(batch.batch_id).then(function (res) {
        const d = res.ok && res.data ? res.data : null;
        const st = d ? d.status : null;
        if (st && st !== 'pending_intake') {
          clearInterval(timer);
          if (WB._waitModal) {
            // 合成结果卡：confidence 取不到就显示 —（抽取明细只在提交响应里有）
            _finishWait(batch, { status: 'confirmed', confidence: null, low_conf_sns: [],
              degraded: true });
          }
          WB.loadIncidents();
          WB._loadDetail(batch.batch_id);
          WB.loadBatches();
          return;
        }
        // 行数 > 0 而状态没动：低置信挂起等人工确认，不会再自己推进 → 收窗交还给人
        if (d && (d.inspections || []).length > 0 && WB._waitModal) {
          clearInterval(timer);
          _finishWait(batch, { status: 'pending', confidence: null,
            low_conf_sns: [], degraded: true });
          return;
        }
        if (tries >= maxTries) {
          clearInterval(timer);
          _closeWaitModal();
          WB.toast('批次处理仍在进行中，当前状态：' + (st || '未知') + '，可稍后刷新查看');
        }
      });
    }, 2000);
  }

  // parseInspectionCsv / decodeCsvBytes 导出以便单测（纯函数，不碰 DOM）；
  // appendResultCard 供 renderCenter 统一绘制「入场结果」卡片（异步重渲染
  // 不会再把它抹掉）；_intakeSubmit 供导入/粘贴两条通道共用。
  return {
    stageIndex: stageIndex,
    render: render,
    parseInspectionCsv: parseInspectionCsv,
    decodeCsvBytes: decodeCsvBytes,
    appendResultCard: appendResultCard,
    _intakeSubmit: _intakeSubmit,
  };
})();

WB.stages._byIndex[0] = WB.stages.intake;
