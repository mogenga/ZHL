"""Static guards for the new workbench frontend (T1).

T1 establishes: no native dialogs + charset declared. T10 extends: status
label coverage + full-file scan. These are *static* checks — no DOM, no
backend — so they run on any checkout without a database.
"""

from __future__ import annotations

from pathlib import Path

WB_FILES = ["workbench.html", "wb.css", "wb/ui.js", "wb/main.js"]
ROOT = Path(__file__).resolve().parents[2] / "app" / "static"


def test_no_native_dialogs():
    for f in WB_FILES:
        src = (ROOT / f).read_text(encoding="utf-8")
        for banned in ("alert(", "prompt(", "confirm("):
            assert banned not in src, f"{f} 使用了原生对话框 {banned}"


def test_charset_declared():
    html = (ROOT / "workbench.html").read_text(encoding="utf-8")
    assert 'charset="utf-8"' in html
