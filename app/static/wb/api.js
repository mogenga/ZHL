/* WB.api — API 层（T2）：fetch 三态封装 + detail 归一化 + 错误中文化
   沿用 common.js「请求前置 loading / 成功 / 失败」思路；原生 JS，零依赖。
   非 2xx 解析 detail（字符串或对象），归一为 {key,msg,detail}；
   WB.errText 按 spec §4.9 映射为中文业务提示，缺省「操作失败，请重试」。
   全局命名空间 WB 由首脚本 ui.js 挂在 window 上，本文件直接引用（不重声明）。 */
'use strict';

/* spec §4.9 错误映射表：key（语义键 / detail 原文）→ 中文提示 */
WB.errText = function (error) {
  const MAP = {
    'batch archived, terminal state': '批次已归档，内容只读',
    batch_not_isolated: '判定失败后批次隔离才能创建异常',
    incident_not_isolated: '请先隔离该异常',
    incident_not_new: '该异常已隔离',
    isolate_denied: '该异常已隔离',
    incident_not_assignable: '该异常当前状态不可指派',
    incident_not_assigned: '该异常尚未指派责任人',
    incident_transition_denied: '异常状态流转被拒绝',
    batch_transition_denied: '批次状态流转被拒绝',
    batch_not_pending_release: '批次当前状态不可放行',
    rejected_batch_only_incident_report: '已拒绝批次只能生成异常处置报告',
    sn_not_found: '部分组件条码不在本批次内',
    evidence_not_found: '引用的证据不存在',
    scope_sns_required: '请填写复检范围组件条码',
    scope_gap: '复检范围未覆盖全部受影响组件',
    unknown_scope_type: '复检范围类型不正确',
    inspection_not_found: '请重新选择组件',
    unconfirmed_sns: '请先完成入场确认',
    'rule not effective yet': '规则版本尚未生效，无法判定',
    'rule version not published': '所选规则版本未发布（草稿/已停用），无法创建批次',
    'cannot_draft': '当前状态不可发布',
    'cannot_published': '已停用为终态，不可再发布',
    'cannot_disabled': '已停用为终态，不可再停用',
    rule_version_not_found: '规则版本不存在或已被删除',
    cross_batch_sn_conflict: '组件条码已存在于其他批次',
    sn_already_intaked: '该组件条码已在本批次送检，请勿重复提交',
    capa_verified_requires_pass_reinspection: '需先有通过的复检记录才能勾选已验证',
    close_conditions_unmet: '关闭条件未满足，请补齐缺失项',
    incident_not_closeable: '该异常当前状态不可关闭',
    batch_not_found: '批次不存在或已被删除',
    incident_not_found: '异常记录不存在',
    report_not_found: '报告不存在或已被删除',
    table_missing: '规则版本数据暂不可用',
    network: '网络异常，请检查服务连接',
    timeout: '请求超时，请重试',
    http: '操作失败，请重试',
  };
  if (!error) return '操作失败，请重试';
  const key = error.key || null;
  // 1) 精确语义键（最具体，承载可操作提示）；'http' 是「无 detail」哨兵键，跳过让它落到状态码兜底
  if (key && key !== 'http' && MAP[key]) return MAP[key];
  // 2) detail 原文匹配（字符串 detail 的 key/msg=原文）；'http' 哨兵跳过
  if (error.msg && error.msg !== 'HTTP ' + (error.status || 0) && MAP[error.msg]) return MAP[error.msg];
  // 3) HTTP 状态码兜底（无语义键时按 404/422/409/5xx 归类）
  if (error.status === 404) return '数据不存在或已被删除';
  if (error.status === 422) return '参数不正确';
  if (error.status === 409) return '状态冲突，请刷新后重试';
  if (error.status && error.status >= 500) return '服务端内部错误';
  return '操作失败，请重试';
};

/* detail 归一化：字符串 → {key:null,msg:detail}；
   对象 → 取语义键（error/reason/unconfirmed_sns/sn_not_found/evidence_not_found/
   scope_gap/missing）作 key，hint/reason 作 msg，整对象保留为 detail。 */
WB._normDetail = function (detail, status) {
  if (detail == null) {
    return { key: 'http', msg: 'HTTP ' + status, status: status, detail: null };
  }
  if (typeof detail === 'string') {
    // 字符串形态：原文即语义键候选（如 'batch_not_found'/'batch archived, terminal state'）
    return { key: detail, msg: detail, status: status, detail: detail };
  }
  // 对象形态：语义键优先级 error > 缺失清单 > reason；msg 取 hint > reason > key
  const out = { key: null, msg: '', status: status, detail: detail };
  if (Array.isArray(detail.unconfirmed_sns)) out.key = 'unconfirmed_sns';
  else if (Array.isArray(detail.missing)) out.key = detail.error || 'sn_not_found';
  else if (typeof detail.error === 'string') out.key = detail.error;
  else if (typeof detail.reason === 'string') out.key = detail.reason;
  if (typeof detail.hint === 'string' && detail.hint.trim()) out.msg = detail.hint;
  else if (typeof detail.reason === 'string') out.msg = detail.reason;
  else if (out.key) out.msg = out.key;
  return out;
};

WB.api = {
  /* 三态封装：返回 Promise<{ok,status,data,error}>
     error = 归一化对象 {key,msg,status,detail}；成功时 error=null。
     网络层异常 → {key:'network',msg:'网络异常'}；超时 → {key:'timeout'}。 */
  async request(method, path, body) {
    const init = { method: method, headers: {} };
    if (body !== undefined && body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const hasAC = (typeof AbortController !== 'undefined');
    const ctrl = hasAC ? new AbortController() : null;
    if (ctrl) {
      init.signal = ctrl.signal;
      ctrl._t = setTimeout(function () { ctrl.abort(); }, 15000);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      if (ctrl) clearTimeout(ctrl._t);
      if (e && e.name === 'AbortError') {
        return { ok: false, status: 0, data: null, error: { key: 'timeout', msg: '请求超时', status: 0, detail: null } };
      }
      return { ok: false, status: 0, data: null, error: { key: 'network', msg: '网络异常', status: 0, detail: null } };
    }
    if (ctrl) clearTimeout(ctrl._t);
    let data = null;
    try {
      const text = await res.text();
      if (text) data = JSON.parse(text);
    } catch (e) { /* 非 JSON 保留 null */ }
    if (!res.ok) {
      const detail = (data && data.detail !== undefined) ? data.detail : data;
      return { ok: false, status: res.status, data: data, error: WB._normDetail(detail, res.status) };
    }
    return { ok: true, status: res.status, data: data, error: null };
  },

  listBatches(status) {
    const q = status ? '?status=' + encodeURIComponent(status) : '';
    return WB.api.request('GET', '/api/batches' + q).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data || [] };
    });
  },

  getBatch(id) {
    return WB.api.request('GET', '/api/batches/' + id).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },

  listRuleVersions() {
    return WB.api.request('GET', '/api/quality-rule-versions').then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data || { items: [], available: false, reason: null } };
    });
  },

  /* 规则版本管理面（spec 2026-10-14-rule-version-management）：创建/发布/停用/审计事件 */
  createRuleVersion(body) {
    return WB.api.request('POST', '/api/quality-rule-versions', body).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  publishRuleVersion(id, body) {
    return WB.api.request('POST', '/api/quality-rule-versions/' + id + '/publish', body || { operator: 'manual' }).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  disableRuleVersion(id, body) {
    return WB.api.request('POST', '/api/quality-rule-versions/' + id + '/disable', body || { operator: 'manual' }).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  listRuleVersionEvents(id) {
    return WB.api.request('GET', '/api/quality-rule-versions/' + id + '/events').then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data || [] };
    });
  },

  listIncidents() {
    return WB.api.request('GET', '/api/incidents').then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data || [] };
    });
  },

  listTraces(batchId) {
    return WB.api.request('GET', '/api/batches/' + batchId + '/traces').then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data || [] };
    });
  },

  /* T3：创建批次（POST /api/batches）→ {ok, data:{batch_id,status}, error} */
  createBatch(body) {
    return WB.api.request('POST', '/api/batches', body).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },

  /* T4：数据入场（POST /api/intake；POST /api/intake/{task_id}/confirm） */
  submitIntake(body) {
    return WB.api.request('POST', '/api/intake', body).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  confirmIntake(taskId, body) {
    return WB.api.request('POST', '/api/intake/' + taskId + '/confirm', body).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },

  /* T5：重新判定（POST /api/batches/{id}/judge，只重算不推进状态机） */
  runJudge(batchId) {
    return WB.api.request('POST', '/api/batches/' + batchId + '/judge').then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },

  /* T6：异常处置全链路 */
  getIncident(id) {
    return WB.api.request('GET', '/api/incidents/' + id).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  createIncident(body) {
    return WB.api.request('POST', '/api/incidents', body).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  isolateIncident(id) {
    return WB.api.request('POST', '/api/incidents/' + id + '/isolate').then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  attributeIncident(id, decision, cause) {
    var body = decision == null ? {} : { decision: decision, cause: cause };
    return WB.api.request('POST', '/api/incidents/' + id + '/attribute', body).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  /* 归因人工采纳（跨请求 HITL）：生成假设后由人另一次请求提交三选一 */
  adoptAttribution(id, decision, cause) {
    return WB.api.request('POST', '/api/incidents/' + id + '/adopt', { decision: decision, cause: cause }).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  assignIncident(id, body) {
    return WB.api.request('POST', '/api/incidents/' + id + '/assign', body).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  capaIncident(id, body) {
    return WB.api.request('POST', '/api/incidents/' + id + '/capa', body).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },

  closeIncident(id) {
    return WB.api.request('POST', '/api/incidents/' + id + '/close', {}).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  /* T7：复检记录（POST/GET /api/incidents/{id}/reinspections） */
  createReinspection(incidentId, body) {
    return WB.api.request('POST', '/api/incidents/' + incidentId + '/reinspections', body).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  listReinspections(incidentId) {
    return WB.api.request('GET', '/api/incidents/' + incidentId + '/reinspections').then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: [] };
      return { ok: true, data: r.data || [] };
    });
  },
  /* T8：放行/拒绝/报告 */
  releaseBatch(batchId, approvedBy) {
    return WB.api.request('POST', '/api/batches/' + batchId + '/release', { approved_by: approvedBy }).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  rejectBatch(batchId, reason, rejectedBy) {
    return WB.api.request('POST', '/api/batches/' + batchId + '/reject', { reason: reason, rejected_by: rejectedBy }).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  generateReport(batchId, reportType) {
    return WB.api.request('POST', '/api/batches/' + batchId + '/report', { report_type: reportType || 'release' }).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  getReport(batchId, reportId) {
    return WB.api.request('GET', '/api/batches/' + batchId + '/reports/' + reportId).then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: null };
      return { ok: true, data: r.data };
    });
  },
  listReports(batchId) {
    return WB.api.request('GET', '/api/batches/' + batchId + '/reports').then(function (r) {
      if (!r.ok) return { ok: false, error: r.error, data: [] };
      return { ok: true, data: r.data || [] };
    });
  },
};
