/* ZHL 平台 — 共享原生 JS（零框架、零构建、零 CDN）
 * 提供：fetch 三态封装（加载/成功/失败）、状态名映射、HTML 转义、顶栏、
 *       错误信息渲染。所有页面复用，不 mock 任何数据。
 */
"use strict";

/* ---------- 顶栏导航（静态页 state.html 不渲染数据栏，其余页面共用） ---------- */
const PAGES = [
  { href: "index.html", label: "首页" },
  { href: "intake.html", label: "数据入场" },
  { href: "batch.html", label: "批次" },
  { href: "incidents.html", label: "异常" },
  { href: "incident.html", label: "异常详情" },
  { href: "recheck.html", label: "复检" },
  { href: "release.html", label: "放行" },
  { href: "report.html", label: "报告" },
  { href: "trace.html", label: "轨迹" },
];

function renderTopbar(active) {
  const bar = document.createElement("div");
  bar.className = "topbar";
  bar.innerHTML = '<span class="brand">质慧链 · 光伏检验数字化平台</span>' +
    PAGES.map((p) =>
      `<a href="${p.href}" ${p.href === active ? 'class="active"' : ""}>${p.label}</a>`
    ).join("");
  document.body.prepend(bar);
}

/* ---------- 状态名映射（后端 status 为英文，展示为中文） ---------- */
const BATCH_STATUS_ZH = {
  draft: "草稿",
  pending_intake: "待入场",
  pending_judge: "待判定",
  pending_release: "待放行",
  isolated: "已隔离",
  disposing: "待处置",
  pending_recheck: "待复检",
  released: "已放行",
  rejected: "已拒绝",
  archived: "已归档",
};

const INCIDENT_STATUS_ZH = {
  new: "新建",
  isolated: "已隔离",
  assigned: "已指派",
  pending_recheck: "待复检",
  closed: "已关闭",
  rejected: "已拒绝",
  scrapped: "已报废",
};

function statusZh(status, kind) {
  const map = kind === "incident" ? INCIDENT_STATUS_ZH : BATCH_STATUS_ZH;
  if (!status) return "—";
  return map[status] || status;
}

function statusBadge(status, kind) {
  const isBatch = kind !== "incident";
  let cls = "info";
  if (isBatch) {
    if (["released", "archived"].includes(status)) cls = "ok";
    else if (["rejected", "isolated"].includes(status)) cls = "err";
    else if (["isolated", "disposing", "pending_recheck", "pending_release", "pending_judge"].includes(status))
      cls = "warn";
  } else {
    if (["closed"].includes(status)) cls = "ok";
    else if (["rejected", "scrapped"].includes(status)) cls = "err";
    else if (["assigned", "pending_recheck", "isolated"].includes(status)) cls = "warn";
  }
  const text = statusZh(status, kind);
  return `<span class="badge ${cls}">${text} <small class="mono">${status || ""}</small></span>`;
}

function decisionBadge(status) {
  if (status === "Pass" || status === "pass") return `<span class="badge ok">Pass</span>`;
  if (status === "Fail" || status === "fail") return `<span class="badge err">Fail</span>`;
  if (!status) return `<span class="badge">—</span>`;
  return `<span class="badge warn">${escapeHtml(status)}</span>`;
}

/* ---------- HTML 转义 ---------- */
function escapeHtml(v) {
  if (v === null || v === undefined) return "";
  const s = String(v);
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

/* ---------- 通用时间格式化（所有页面共用，不各写一份） ----------
 * 后端 datetime 统一以 ISO-8601 UTC 字符串返回（如 2026-10-12T16:36:33.021078+00:00）。
 * 直接输出会：(a) 微秒/时区原样曝光撑宽表格列；(b) 用户看不出本地时间。
 * 所以列表页统一走 fmtDateTimeCell：显示「YYYY-MM-DD HH:mm」本地时间，
 * 完整带时区的信息放进 title 属性，悬浮可见，信息量不丢失。
 *
 * 为什么放 common.js 而不是每页各写一份：
 *   - 所有静态页都 <script src="common.js">，改一处全页生效（治根）。
 *   - 时间展示是纯 UI 关注点，不碰业务逻辑与数据接口。
 * 只处理「展示」，不改动后端返回的任何字段值。
 */
function _localDateParts(v) {
  /* 把 ISO 字符串解析成本地 Date；解析失败返回 null。
   * 注意：不用 new Date() 的隐式本地时区假设 —— 后端带 +00:00 偏移，
   * JS 会正确换算成浏览器本地时区，这是期望行为（表格显示本地时间）。 */
  if (v === null || v === undefined || v === "") return null;
  const d = new Date(v);
  if (isNaN(d.getTime())) return null; // 非 ISO 串原样返回，交由调用方决定
  return d;
}

function _pad2(n) { return n < 10 ? "0" + n : String(n); }

/* 短格式：YYYY-MM-DD HH:mm（本地时区）。用于列表 / 时间线等紧凑场景。 */
function fmtDateTime(v) {
  const d = _localDateParts(v);
  if (!d) return v === null || v === undefined || v === "" ? "" : String(v);
  return d.getFullYear() + "-" + _pad2(d.getMonth() + 1) + "-" + _pad2(d.getDate()) +
    " " + _pad2(d.getHours()) + ":" + _pad2(d.getMinutes());
}

/* 完整格式：YYYY-MM-DD HH:mm:ss（本地时区），用于 title 悬浮，信息量不丢。 */
function fmtDateTimeFull(v) {
  const d = _localDateParts(v);
  if (!d) return String(v || "");
  return fmtDateTime(v) + ":" + _pad2(d.getSeconds());
}

/* 列表页时间单元格：<span> 显示短格式，title 属性放完整时间（含秒）。
 * 返回 HTML 字符串，需嵌入 <td>。空值显示「—」占位，避免列宽跳动。
 * 为什么用 span+title 而不是直接改 <td>：调用点已经在 <td> 里拼接，
 * 只需替换 escapeHtml(xxx_at) 为 fmtDateTimeCell(xxx_at)，改动最小。 */
function fmtDateTimeCell(v) {
  const short = fmtDateTime(v);
  if (short === "") return `<span class="muted">—</span>`;
  return `<span title="${escapeHtml(fmtDateTimeFull(v))}">${escapeHtml(short)}</span>`;
}

window.fmtDateTime = fmtDateTime;
window.fmtDateTimeFull = fmtDateTimeFull;
window.fmtDateTimeCell = fmtDateTimeCell;

/* ---------- 把后端 detail（字符串或对象）渲染成可读错误 ----------
 * 优先级：detail.reason（B3 标准错误体）> detail 字符串 > 整个 detail 对象。
 * detail.reason 是后端约定的「人类可读原因」字段（如 unknown rule version），
 * 比裸 UUID 更可读；detail 对象序列化只作兜底。
 */
function describeError(res, data) {
  let detail = "";
  if (data && data.detail !== undefined) detail = data.detail;
  else if (data) detail = data;
  // B3 标准：detail.reason 优先作为主因；没有再退到 detail 本身。
  const reason = detail && typeof detail === "object" ? detail.reason : null;
  let text;
  if (typeof reason === "string" && reason.trim()) {
    text = reason.trim();
  } else {
    text = typeof detail === "string" ? detail : JSON.stringify(detail || {}, null, 2);
  }
  const code = res.status;
  let hint = "";
  if (code === 404) hint = "未找到对应资源（ID 可能错误或记录不存在）。";
  else if (code === 409) hint = "状态冲突：当前批次/异常状态不允许该操作。";
  else if (code === 400) hint = "请求参数不合法。";
  else if (code >= 500) hint = "服务端内部错误。";
  return { code, text, hint, reason };
}

/* ---------- 三态渲染容器 ----------
 * 用法：const box = ZHL.mount(container, "加载中…");
 * 成功：box.success(innerHTML)   失败：box.fail(code, detailText, hint)
 * 加载中：box.loading()         自动清理旧内容
 */
const ZHL = {
  mount(container, initial) {
    container.innerHTML = "";
    const box = document.createElement("div");
    if (initial) box.innerHTML = `<div class="state-loading"><div class="spinner"></div>${escapeHtml(initial)}</div>`;
    container.appendChild(box);
    return {
      set(html) {
        box.innerHTML = html;
      },
      loading(text) {
        box.innerHTML = `<div class="state-loading"><div class="spinner"></div>${escapeHtml(text || "加载中…")}</div>`;
      },
      fail(code, detailText, hint, extra) {
        let block = `<div class="state-error"><strong>请求失败（HTTP ${escapeHtml(code)}）</strong>`;
        if (hint) block += `<div class="note err" style="margin-top:8px;margin-bottom:0">${escapeHtml(hint)}</div>`;
        if (extra) block += `<div class="note warn" style="margin-top:8px;margin-bottom:0">${escapeHtml(extra)}</div>`;
        if (detailText) block += `<code>${escapeHtml(detailText)}</code>`;
        block += "</div>";
        box.innerHTML = block;
      },
      clear() {
        box.innerHTML = "";
      },
    };
  },

  /* 统一 fetch：返回 { ok, res, data }。
   * 成功（2xx）时 data 为解析后的 JSON（GET 空列表亦为 []）。
   * 失败时自动用 box.fail 渲染，data 为解析后的 JSON 或 null。
   */
  async request(method, url, body, box) {
    const init = { method, headers: {} };
    // 必须同时排除 null：GET/HEAD 带 body 会被浏览器 fetch 直接拒绝
    // （TypeError: Request with GET/HEAD method cannot have body），
    // 请求根本发不出去。调用点历史上用 null 表示「无 body」，故一并判空。
    if (body !== undefined && body !== null) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, init);
    } catch (e) {
      // 反直觉的坑：fetch 在这里抛异常（TypeError: Failed to fetch / network error）
      // 意味着请求根本没到达后端——不是后端返回了 4xx/5xx。
      // 最常见的两种现实原因：
      //   1) 浏览器停在旧端口的标签页（如 8000 的幽灵实例），而后端实际在 8001；
      //   2) 页面 origin 与 uvicorn 监听端口不符（复制了别处的 URL）。
      // 历史 bug：旧文案把「网络层失败」渲染成「HTTP 网络错误」，让人误以为是
      // 后端 HTTP 报错（实际一个字节都没到后端），导致排障绕远路。
      // 治根改法（共用封装一处改）：把「当前页面 origin」直接打出来，让使用者
      // 一眼看出是否走错端口；文案明确写「请求未到达后端」，不再冒充 HTTP 错误。
      // 错误仍真实暴露：完整异常文本 + origin + 实际请求 URL 全部可见，不吞异常。
      // 注意边界：catch 只捕获「网络层异常」；后端返回的 4xx/5xx 走下方 !res.ok
      // 分支真实渲染（box.fail(res.status, ...)），两条路径互不混叠——绝不用
      // try/catch 吞掉后端业务错误来「消除报错」。
      if (box) box.fail(
        "网络层错误（请求未到达后端）",
        `fetch 抛异常：${e}｜当前页面 origin=${location.origin}，请求「${url}」实际打到 ${location.origin}${url}。`,
        "出现此错 = 浏览器停在了别的端口（如 8000 幽灵实例）或后端未在该 origin 上运行。请确认地址栏端口与 uvicorn 实际监听端口一致后刷新重试。"
      );
      return { ok: false, res: null, data: null };
    }
    let data = null;
    try {
      const text = await res.text();
      if (text) data = JSON.parse(text);
    } catch (e) {
      /* 非 JSON 响应（如空 body），保留 null */
    }
    if (!res.ok) {
      const d = describeError(res, data);
      if (box) box.fail(res.status, d.text, d.hint);
      return { ok: false, res, data };
    }
    return { ok: true, res, data };
  },

  /* 把任意值渲染为 evidence 代码块 */
  evidenceBlock(v) {
    if (v === null || v === undefined || v === "") return `<span class="muted">（无证据数据）</span>`;
    const text = typeof v === "string" ? v : JSON.stringify(v, null, 2);
    return `<div class="evidence-block">${escapeHtml(text)}</div>`;
  },

  /* 取 URL 查询参数 */
  qparam(name) {
    const qs = new URLSearchParams(location.search);
    const raw = qs.get(name);
    return raw ? decodeURIComponent(raw) : null;
  },

  /* ---------- localStorage 上下文记忆（流程串联，避免手抄 UUID） ----------
   * remember(kind, value)：把某类上下文写入 localStorage（命名空间 zhl_ctx_）。
   * recall(kind)：读回；优先返回 URL 参数（若调用方用 qparam 传入同 key），
   * 否则读 localStorage。返回值 { value, from } ，from ∈ 'url' | 'local' | null。
   * 注意：本函数不读 URL，页面应自己 `qparam('batchId') || recall('batch').value`。
   * 这里 recall 只读 localStorage，保持单一职责。
   */
  remember(kind, value) {
    if (value === null || value === undefined || value === "") return;
    try { localStorage.setItem("zhl_ctx_" + kind, String(value)); } catch (e) { /* 隐私模式等 */ }
  },
  recall(kind) {
    try {
      const v = localStorage.getItem("zhl_ctx_" + kind);
      return v ? { value: v, from: "local" } : { value: null, from: null };
    } catch (e) { return { value: null, from: null }; }
  },
  /* 综合预填：URL 参数优先，其次 localStorage；返回 { value, from }，
   * from='url' 无需提示，from='local' 提示「已自动带入上次上下文」。 */
  prefill(name, kind) {
    const fromUrl = this.qparam(name);
    if (fromUrl) return { value: fromUrl, from: "url" };
    return this.recall(kind);
  },
};

/* 状态别名：让页面脚本直接写 window.ZHL */
window.ZHL = ZHL;
window.renderTopbar = renderTopbar;
window.statusBadge = statusBadge;
window.decisionBadge = decisionBadge;
window.statusZh = statusZh;
window.escapeHtml = escapeHtml;
window.describeError = describeError;

/* ---------- 提交按钮防重提交 helper ----------
 * 用法：const guard = ZHL.guardSubmit(btn, "提交中…");
 * 成功/失败后 guard.done() 恢复。防抖：连续点击只触发一次；
 * 请求完成前按钮禁用并显示「提交中…」。 */
ZHL.guardSubmit = function (btn, busyLabel) {
  let inFlight = false;
  return {
    run: async function (fn) {
      if (inFlight) return; // 防重：上一笔未完成直接忽略
      inFlight = true;
      const prev = btn ? btn.textContent : null;
      if (btn) { btn.disabled = true; btn.textContent = busyLabel || "提交中…"; }
      try {
        await fn();
      } finally {
        inFlight = false;
        if (btn) { btn.disabled = false; btn.textContent = prev; }
      }
    },
    get busy() { return inFlight; },
  };
};
