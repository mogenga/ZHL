"""ReportRenderer plugin (PB Task 4, D1 三层留痕 — 报告层).

Renders the two report types named by 总 spec §5 / PB spec §5.6:

- ``render_release_report``: a batch release / disposal report. Emits a
  Markdown body (batch id, all seven release-assertion checks, trace +
  evidence references) and a parallel structured JSON dict.
- ``render_incident_report``: an incident-disposal report; includes a
  refusal-reason section when ``context["refusal_reason"]`` is non-empty.

Determinism contract (PB spec §5.6): the renderer reads NO clock. The
``generated_at`` timestamp is part of the ``context`` the caller supplies,
so two renders of the same context are byte-identical (a re-render is
idempotent, matching D10).

No Jinja2 — the spec left the template engine open; a function-local
f-string / ``str.format`` template plus ``json.dumps`` is sufficient and
keeps the plugin dependency-free (reviewer may override this decision).
"""

from __future__ import annotations

import json
from typing import Any

#: Fixed context keys every report render consumes (PB spec §5.6).
_CONTEXT_KEYS = (
    "batch",
    "decisions",
    "incidents",
    "reinspections",
    "assertions",
    "refusal_reason",
    "trace_refs",
    "evidence_refs",
    "generated_at",
)


def _require_context(context: dict) -> dict:
    missing = [k for k in _CONTEXT_KEYS if k not in context]
    if missing:
        raise ValueError(f"report context missing required keys: {missing}")
    return context


def _checks_lines(checks: list[dict]) -> list[str]:
    """Render the assertion checks as Markdown list lines, one per check."""
    lines = []
    for c in checks:
        mark = "PASS" if c.get("passed") else "FAIL"
        line = f"- [{mark}] {c['name']}"
        if not c.get("passed") and c.get("reason"):
            line += f" — {c['reason']}"
        lines.append(line)
    return lines


class ReportRenderer:
    """Clock-free Markdown + JSON report renderer."""

    def _decisions_lines(self, decisions: Any) -> list[str]:
        """判定统计 → Markdown 列表行（dict 逐键 / list 逐条；非标量 json 化）。

        Phase 0 夹具传 list、现行 ReportSkill 传 dict（judgment_stats）——
        两种都如实渲染，绝不因形状不认识而静默丢章节。
        """
        lines: list[str] = []
        if isinstance(decisions, dict):
            for k, v in decisions.items():
                if isinstance(v, (str, int, float, bool)):
                    lines.append(f"- {k}: {v}")
                else:
                    lines.append(f"- {k}: {json.dumps(v, ensure_ascii=False)}")
        elif isinstance(decisions, list):
            for d in decisions:
                if isinstance(d, dict):
                    lines.append(f"- {json.dumps(d, ensure_ascii=False)}")
                else:
                    lines.append(f"- {d}")
        return lines

    def _judgment_section(self, context: dict) -> list[str]:
        """「## 判定统计」章节（Phase B spec §5.10 两类报告模板都要求）。"""
        dlines = self._decisions_lines(context.get("decisions"))
        if not dlines:
            return []
        return ["## 判定统计", "", *dlines, ""]

    def _build_json(self, context: dict, report_type: str) -> dict:
        return {
            "report_type": report_type,
            "generated_at": context["generated_at"],
            "batch": context["batch"],
            "decisions": context["decisions"],
            "incidents": context["incidents"],
            "reinspections": context["reinspections"],
            "assertions": context["assertions"],
            "refusal_reason": context["refusal_reason"],
            "trace_refs": context["trace_refs"],
            "evidence_refs": context["evidence_refs"],
        }

    def _three_layer_refs(self, context: dict) -> str:
        """Render the D1 three-layer留痕 reference block (trace + evidence)."""
        lines = ["## 留痕引用（轨迹层 / 证据层）", ""]
        lines.append("轨迹（agent_task_traces）:")
        if context["trace_refs"]:
            for ref in context["trace_refs"]:
                lines.append(f"- {ref}")
        else:
            lines.append("- （无轨迹引用）")
        lines.append("")
        lines.append("证据（evidence）:")
        if context["evidence_refs"]:
            for ref in context["evidence_refs"]:
                lines.append(f"- {ref}")
        else:
            lines.append("- （无证据引用）")
        return "\n".join(lines)

    def render_release_report(self, context: dict) -> tuple[str, dict]:
        ctx = _require_context(context)
        batch = ctx["batch"]
        assertions: dict[str, Any] = ctx["assertions"]
        checks: list[dict] = assertions.get("checks", [])

        md: list[str] = []
        md.append(f"# 批次放行报告 — {batch.get('batch_id', '?')}")
        md.append("")
        md.append(f"- 批次状态：{batch.get('status', '?')}")
        md.append(f"- 型号：{batch.get('model', '?')}")
        md.append(f"- 生成时间：{ctx['generated_at']}")
        md.append("")
        md.extend(self._judgment_section(ctx))
        md.append("## 放行断言（7 项）")
        md.append("")
        if checks:
            md.extend(_checks_lines(checks))
        else:
            md.append("- （无断言记录）")
        md.append("")
        md.append(f"放行结论：**{'通过' if assertions.get('passed') else '未通过'}**")
        md.append("")
        md.append(self._three_layer_refs(ctx))

        return "\n".join(md), self._build_json(ctx, "release")

    def render_incident_report(self, context: dict) -> tuple[str, dict]:
        ctx = _require_context(context)
        batch = ctx["batch"]
        refusal: str = ctx.get("refusal_reason", "")

        md: list[str] = []
        md.append(f"# 批次处置报告 — {batch.get('batch_id', '?')}")
        md.append("")
        md.append(f"- 批次状态：{batch.get('status', '?')}")
        md.append(f"- 生成时间：{ctx['generated_at']}")
        md.append("")
        md.extend(self._judgment_section(ctx))

        # 拒因 section (PB spec §5.1: rejected only attaches a disposal
        # report; the refusal reason is a section within it, never a
        # separate report).
        if refusal:
            md.append("## 拒因")
            md.append("")
            md.append(refusal)
            md.append("")

        incidents = ctx.get("incidents", [])
        if incidents:
            md.append("## 异常清单")
            md.append("")
            for inc in incidents:
                status = inc.get("status", "?")
                severity = inc.get("severity", "?")
                md.append(f"- {inc.get('incident_id', '?')}（{severity}，{status}）")
            md.append("")

        reinspections = ctx.get("reinspections", [])
        if reinspections:
            md.append("## 复检记录")
            md.append("")
            for re_ in reinspections:
                md.append(
                    f"- {re_.get('reinspection_id', '?')}："
                    f"{re_.get('result', '?')}"
                    f"（scope={re_.get('scope_type', 'minimal')}）"
                )
            md.append("")

        md.append(self._three_layer_refs(ctx))

        return "\n".join(md), self._build_json(ctx, "incident")
