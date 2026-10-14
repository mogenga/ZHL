/* WB.stages.report — 报告节点（stageIndex 5）：展示本批次已生成的报告与正文。

   此前这个节点**没有注册面板**，点进流水线「报告」只有一句"该节点无可操作项"，
   而批次其实已经有放行报告了（2026-10-15 反馈：报告页面显示的不是报告内容）。

   这里复用主区的批次报告卡（``main.js::_reportPage``，左侧「报告」tab 用的是
   同一份实现，避免两套口径），并以 ``autoExpand`` 直接展开最新一版的正文——
   报告节点就该看到报告本身，而不是再点一次「查看正文」。 */
'use strict';

WB.stages.report = (function () {
  const stageIndex = 5;

  function render(batch) {
    return WB._reportPage(batch, { autoExpand: true });
  }

  return { stageIndex: stageIndex, render: render };
})();

WB.stages._byIndex[5] = WB.stages.report;
