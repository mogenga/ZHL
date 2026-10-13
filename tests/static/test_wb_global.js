// T2/T1 regression: emulate the browser's three <script> tags (ui.js → api.js →
// main.js) sharing ONE global environment, and confirm the global namespace
// WB pattern works (no redeclaration crash, WB populated, boot() runs).
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const read = (f) => fs.readFileSync(path.join(root, 'app', 'static', 'wb', f), 'utf8');
const ui = read('ui.js'), api = read('api.js'), main = read('main.js');
const intake = read('stages/intake.js');
const judge = read('stages/judge.js');
const incident = read('stages/incident.js');
const reinspection = read('stages/reinspection.js');

// ---- browser-like DOM shim ----
function el(id) {
  return {
    _id: id, children: [], style: {}, dataset: {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){return false;} },
    textContent: '', className: '', innerHTML: '', value: '',
    append(){}, replaceChildren(){}, setAttribute(){}, addEventListener(){},
    querySelectorAll(){ return []; }, querySelector(){ return el('x'); },
  };
}
const byId = {};
const documentStub = {
  readyState: 'complete',
  getElementById: (i) => (byId[i] = byId[i] || el(i)),
  createElement: (t) => el(t),
  createTextNode: (t) => ({ nodeType: 3, nodeValue: t }),
  querySelectorAll: () => [],
  addEventListener(){},
  body: { appendChild(){}, append(){} },
};

// Single context == the browser's shared global (all three scripts in one env).
const urlObj = new URL('http://x/static/workbench.html');
const windowObj = { location: urlObj, origin: 'http://x' };
const ctx = vm.createContext({
  document: documentStub,
  window: windowObj,
  location: urlObj,                 // bare `location` global (main.js _url reads it)
  URL: (typeof URL !== 'undefined' ? URL : undefined),
  URLSearchParams,
  history: { replaceState(){} },
  setTimeout, clearTimeout, console,
  fetch: () => Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('null') }),
  AbortController: function(){},
});

let failed = 0;
function assert(c, m){ if(!c){ console.error('FAIL:', m); failed++; } else console.log('ok:', m); }

// 1) The concatenated global must not throw "Identifier 'WB' already declared"
let threw = null;
try {
  // Simulate three separate scripts sharing the env: run each in sequence.
  vm.runInContext(ui, ctx, { filename: 'ui.js' });
  vm.runInContext(api, ctx, { filename: 'api.js' });
  vm.runInContext(main, ctx, { filename: 'main.js' });
  vm.runInContext(intake, ctx, { filename: 'intake.js' });
  vm.runInContext(judge, ctx, { filename: 'judge.js' });
  vm.runInContext(incident, ctx, { filename: 'incident.js' });
  vm.runInContext(reinspection, ctx, { filename: 'reinspection.js' });
} catch (e) { threw = e; }
assert(threw === null, 'three <script> tags share global without redeclaration crash' + (threw ? ' -> ' + threw.message : ''));
if (threw) { console.log('\n' + failed + ' FAILED'); process.exit(1); }

// 2) WB is one shared object across the three scripts
const WB = ctx.WB;
assert(!!WB && typeof WB === 'object', 'WB is a shared global object');
assert(typeof WB.h === 'function', 'WB.h present (ui.js)');
assert(typeof WB.errText === 'function', 'WB.errText present (api.js)');
assert(typeof WB.api.request === 'function', 'WB.api.request present (api.js)');
assert(typeof WB.boot === 'function', 'WB.boot present (main.js)');
assert(typeof WB.state === 'object' && WB.state.view === 'batch', 'WB.state initialized by main.js');

// 3) errText still maps correctly post-refactor
assert(WB.errText({ key: 'batch_not_found', status: 404 }) === '批次不存在或已被删除', 'errText intact');

// 4) boot() actually runs against the shim (no reference error)
try {
  WB.boot();
  assert(true, 'WB.boot() runs without throwing');
} catch (e) {
  assert(false, 'WB.boot() threw: ' + e.message);
}

// 5) T3: newBatchForm + createBatch wired up
assert(typeof WB.newBatchForm === 'function', 'WB.newBatchForm present (T3)');
assert(typeof WB.api.createBatch === 'function', 'WB.api.createBatch present (T3)');
assert(typeof WB.stages.intake === 'object' && WB.stages.intake.stageIndex === 0, 'intake stage defined at index 0 (T4)');
assert(WB.stages._byIndex[0] === WB.stages.intake, 'intake stage registered in _byIndex[0] (T4)');
assert(typeof WB.api.submitIntake === 'function', 'WB.api.submitIntake present (T4)');
assert(typeof WB.api.confirmIntake === 'function', 'WB.api.confirmIntake present (T4)');
assert(WB.stages._byIndex[1] === WB.stages.judge, 'judge stage registered at index 1 (T5)');
assert(typeof WB.api.runJudge === 'function', 'WB.api.runJudge present (T5)');
assert(WB.stages._byIndex[2] === WB.stages.incident, 'incident stage registered at index 2 (T6)');
assert(typeof WB.api.isolateIncident === 'function', 'WB.api.isolateIncident present (T6)');
assert(typeof WB.api.attributeIncident === 'function', 'WB.api.attributeIncident present (T6)');
assert(typeof WB.api.assignIncident === 'function', 'WB.api.assignIncident present (T6)');
assert(typeof WB.api.capaIncident === 'function', 'WB.api.capaIncident present (T6)');
assert(typeof WB.api.createIncident === 'function', 'WB.api.createIncident present (T6)');
assert(WB.stages._byIndex[3] === WB.stages.reinspection, 'reinspection stage registered at index 3 (T7)');
assert(typeof WB.api.createReinspection === 'function', 'WB.api.createReinspection present (T7)');
assert(typeof WB.api.listReinspections === 'function', 'WB.api.listReinspections present (T7)');
// newBatchForm with no rule versions should disable submit (not throw)
try {
  WB.state.ruleVersions = null; // force the disabled branch
  WB.newBatchForm();
  assert(true, 'newBatchForm handles missing rule versions without throwing');
} catch (e) {
  assert(false, 'newBatchForm threw: ' + e.message);
}

console.log(failed === 0 ? '\nALL PASS' : '\n' + failed + ' FAILED');
process.exit(failed === 0 ? 0 : 1);
