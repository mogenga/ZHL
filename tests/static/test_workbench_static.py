"""Static guards for the new workbench frontend (T1).

T1 establishes: no native dialogs + charset declared. T10 extends: status
label coverage + full-file scan. These are *static* checks — no DOM, no
backend — so they run on any checkout without a database.
"""

from __future__ import annotations

from pathlib import Path

WB_FILES = [
    "workbench.html", "wb.css",
    "wb/ui.js", "wb/api.js", "wb/main.js", "wb/report.js",
    "wb/stages/intake.js", "wb/stages/judge.js", "wb/stages/incident.js",
    "wb/stages/reinspection.js", "wb/stages/release.js",
]
ROOT = Path(__file__).resolve().parents[2] / "app" / "static"


def test_api_js_served_by_html():
    html = (ROOT / "workbench.html").read_text(encoding="utf-8")
    # 缓存击穿：脚本引用允许带 ?v= 版本参数（workbench.html 维护）
    import re

    def _src_pos(f: str) -> int:
        m = re.search(r'src="/static/' + re.escape(f) + r'(\?[^"]*)?"', html)
        assert m, f"{f} 未在 workbench.html 中加载"
        return m.start()

    assert _src_pos("wb/api.js") >= 0
    # 顺序：ui.js → api.js → main.js → 各 stage → report.js
    order = ["wb/ui.js", "wb/api.js", "wb/main.js",
             "wb/stages/intake.js", "wb/stages/judge.js", "wb/stages/incident.js",
             "wb/stages/reinspection.js", "wb/stages/release.js", "wb/report.js"]
    idx = [_src_pos(f) for f in order]
    assert idx == sorted(idx), f"脚本加载顺序错误：{list(zip(order, idx))}"


def test_no_native_dialogs():
    for f in WB_FILES:
        src = (ROOT / f).read_text(encoding="utf-8")
        for banned in ("window.alert(", "window.prompt(", "window.confirm(",
                       "alert(", "prompt(", "confirm("):
            assert banned not in src, f"{f} 使用了原生对话框 {banned}"


def test_charset_declared():
    html = (ROOT / "workbench.html").read_text(encoding="utf-8")
    assert 'charset="utf-8"' in html
