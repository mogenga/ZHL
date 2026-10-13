// T2 node test: WB._normDetail + WB.errText against real backend shapes.
// Load api.js with a tiny browser-global shim (fetch unused in these paths),
// then assert normalization + Chinese mapping. Run: node test_wb_api_norm.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..', '..');
const apiSrc = fs.readFileSync(path.join(root, 'app', 'static', 'wb', 'api.js'), 'utf8');

// Each wb file declares its own `const WB` for module scope and relies on it
// being the shared global the browser puts on window. We simulate that by:
//  1. creating a shared WB object on the context global (like window.WB),
//  2. running api.js in its own context so its `const WB = ...` re-declaration
//     is *inside that context's top level* — but Node's vm context global vs
//     local scopes: `const WB` at the top of runInContext script is scoped to
//     that script, so it shadows any pre-existing global.WB within the script
//     body. That's why api.js alone failed before.
//
// The reliable trick: run api.js with its `const WB` line *rewritten* to
// `WB` (bare), so api.js reads the shared global we inject — matching how
// every other wb file in the browser extends the existing window.WB.
const apiNoConst = apiSrc.replace(
  /const\s+WB\s*=\s*\(typeof\s+WB\s*!==\s*'undefined'\s*\?\s*WB\s*:\s*\{\}\)\s*;/,
  'WB'
);

const sharedWB = {};
const sandbox = {
  WB: sharedWB,
  window: { AbortController: function () {}, setTimeout: setTimeout, clearTimeout: clearTimeout },
  setTimeout, clearTimeout,
  console,
};
vm.createContext(sandbox);
vm.runInContext(apiNoConst, sandbox, { filename: 'api.js' });

// api.js extended the shared global WB object.
const W = sharedWB;
let failed = 0;
function assert(cond, msg) { if (!cond) { console.error('FAIL:', msg); failed++; } else { console.log('ok:', msg); } }

// 1) string detail -> key=原文, msg=原文
{
  const e = W._normDetail('batch_not_found', 404);
  assert(e.key === 'batch_not_found', 'string detail key');
  assert(e.msg === 'batch_not_found', 'string detail msg');
  assert(W.errText(e) === '批次不存在或已被删除', 'errText batch_not_found');
}
// 2) string "batch archived, terminal state"
{
  const e = W._normDetail('batch archived, terminal state', 409);
  assert(W.errText(e) === '批次已归档，内容只读', 'errText archived terminal');
}
// 3) object detail with error key (e.g. incident_not_isolated + hint)
{
  const e = W._normDetail({ error: 'incident_not_isolated', status: 'new', hint: '请先调用 POST /api/incidents/{id}/isolate 完成隔离，再指派责任人' }, 409);
  assert(e.key === 'incident_not_isolated', 'object error key');
  assert(e.msg.indexOf('请先调用') === 0, 'object hint as msg');
  assert(W.errText(e) === '请先隔离该异常', 'errText incident_not_isolated');
}
// 4) object detail with unconfirmed_sns array
{
  const e = W._normDetail({ unconfirmed_sns: ['SN-1','SN-2'] }, 400);
  assert(e.key === 'unconfirmed_sns', 'unconfirmed_sns key');
  assert(Array.isArray(e.detail.unconfirmed_sns), 'detail preserved for list render');
  assert(W.errText(e) === '请先完成入场确认', 'errText unconfirmed_sns');
}
// 5) object detail with missing[] (sn_not_found)
{
  const e = W._normDetail({ error: 'sn_not_found', missing: ['X-9'] }, 400);
  assert(e.key === 'sn_not_found', 'sn_not_found key');
  assert(e.detail.missing[0] === 'X-9', 'missing preserved');
  assert(W.errText(e) === '部分组件条码不在本批次内', 'errText sn_not_found');
}
// 6) object detail reason only (e.g. rule not effective yet)
{
  const e = W._normDetail({ rule_version_id: 'rv1', reason: 'rule not effective yet' }, 400);
  assert(e.key === 'rule not effective yet', 'reason key');
  assert(W.errText(e) === '规则版本尚未生效，无法判定', 'errText rule not effective');
}
// 7) 404 with no detail → HTTP 兜底
{
  const e = W._normDetail(null, 404);
  assert(e.key === 'http', 'no-detail key http');
  assert(W.errText(e) === '数据不存在或已被删除', 'errText 404 fallback');
}
// 7b) 404 WITH semantic key → 语义键优先于 404 兜底
{
  const e = W._normDetail('batch_not_found', 404);
  assert(W.errText(e) === '批次不存在或已被删除', 'errText semantic beats 404 fallback');
}
// 8) network error shape
{
  const e = { key: 'network', msg: '网络异常', status: 0, detail: null };
  assert(W.errText(e) === '网络异常，请检查服务连接', 'errText network');
}

// 9) WB.api.listBatches returns {ok,data} on success — stub fetch
{
  // fetch(url, init) — 参数顺序为 (url, init)，非 (method, url)
  sandbox.fetch = function (url, init) {
    if (typeof url === 'string' && url.indexOf('/api/batches') === 0) {
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('["b1"]') });
    }
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('null') });
  };
  W.api.listBatches().then(function (r) {
    assert(r.ok === true, 'listBatches ok');
    assert(Array.isArray(r.data) && r.data[0] === 'b1', 'listBatches data parsed');
    // 10) error path: 404 getBatch
    sandbox.fetch = function (url) {
      return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve('{"detail":"batch_not_found"}') });
    };
    W.api.getBatch('nope').then(function (g) {
      assert(g.ok === false, 'getBatch not ok');
      assert(g.error.key === 'batch_not_found', 'getBatch error key');
      assert(W.errText(g.error) === '批次不存在或已被删除', 'getBatch errText');
      console.log(failed === 0 ? '\nALL PASS' : '\n' + failed + ' FAILED');
      process.exit(failed === 0 ? 0 : 1);
    });
  });
}
