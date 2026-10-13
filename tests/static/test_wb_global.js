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
  body: { appendChild(){} },
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

console.log(failed === 0 ? '\nALL PASS' : '\n' + failed + ' FAILED');
process.exit(failed === 0 ? 0 : 1);
