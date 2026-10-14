"""从 CSV 导入的模板守卫：模板必须与解析器守同一套 9 列契约。

模板是给用户下载去填的。它一旦与解析器漂移（列数、表头形状、外观取值），
用户照着模板填完再导入反而被拦下——错在模板，却要怪系统。这里把两者钉在一起
（前端解析规则见 ``app/static/wb/stages/intake.js::parseInspectionCsv``）。
"""

from __future__ import annotations

from pathlib import Path

TPL = (
    Path(__file__).resolve().parents[2] / "app" / "static" / "templates" / "inspection_template.csv"
)


def test_template_has_utf8_bom() -> None:
    """带 BOM：中文 Windows 上 Excel 打开 UTF-8 CSV 才不会按 GBK 解成乱码。"""
    assert TPL.exists(), f"模板缺失：{TPL}"
    assert TPL.read_bytes().startswith(b"\xef\xbb\xbf"), "模板需带 UTF-8 BOM"


def test_template_follows_the_nine_column_contract() -> None:
    text = TPL.read_text(encoding="utf-8-sig")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    assert len(lines) >= 3, "模板应含表头 + 至少 2 行示例"

    header = [c.strip() for c in lines[0].split(",")]
    assert len(header) == 9, f"表头应为 9 列，当前 {len(header)}"
    # 解析器靠"首列 sn + 含 pmax"识别并跳过表头行
    assert header[0].lower() == "sn"
    assert "pmax" in [h.lower() for h in header]

    for idx, line in enumerate(lines[1:], start=2):
        cols = [c.strip() for c in line.split(",")]
        assert len(cols) == 9, f"模板第 {idx} 行应为 9 列，当前 {len(cols)}"
        assert cols[0], f"模板第 {idx} 行缺少 SN"
        for i in range(1, 8):
            float(cols[i])  # 数字列：解析器用 Number() 校验
        assert cols[8] in ("pass", "fail"), f"模板第 {idx} 行外观须为 pass/fail"
