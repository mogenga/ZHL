"""把 Markdown 设计文档转成「图片内嵌」的单文件 HTML。

为什么需要：文档里的图片是本地相对路径（images/xxx.png），文档单独发给别人时
图片不会跟着走，Markdown 渲染器就会显示裂图。这里把每张 PNG 转成 base64
data URI 内嵌进 HTML，产出单个 .html 文件 —— 双击能看，发微信/邮件也不会丢图。

用法：
    python scripts/md2html.py                      # 转换 docs/ 下全部 .md
    python scripts/md2html.py docs/xxx.md          # 只转指定文件
输出：
    docs/dist/<同名>.html
"""

from __future__ import annotations

import base64
import mimetypes
import re
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DIST = DOCS / "dist"

# 打印样式：正文宽度限行，图片自适应不溢出，表格可横向滚动。
CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 40px 32px 96px;
  max-width: 980px;
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  font-size: 15.5px; line-height: 1.85;
  color: #1f2328; background: #fff;
}
h1 { font-size: 30px; margin: 0 0 20px; padding-bottom: 14px; border-bottom: 3px solid #0969da; }
h2 { font-size: 23px; margin: 44px 0 16px; padding-bottom: 8px; border-bottom: 1px solid #d8dee4; }
h3 { font-size: 18.5px; margin: 30px 0 12px; }
h4 { font-size: 16px; margin: 22px 0 10px; color: #3a4149; }
p { margin: 12px 0; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
ul, ol { padding-left: 26px; margin: 12px 0; }
li { margin: 6px 0; }
hr { border: 0; border-top: 1px solid #d8dee4; margin: 32px 0; }
/* 图片：块级居中、自适应容器宽度，永远不会溢出或超出边界 */
img {
  display: block; margin: 20px auto;
  max-width: 100%; height: auto;
  border: 1px solid #e3e6ea; border-radius: 6px;
  background: #fff;
}
code {
  font-family: Consolas, "Cascadia Mono", monospace;
  font-size: 13.5px; background: #f2f4f7;
  padding: 2px 5px; border-radius: 4px; color: #b3247a;
}
pre {
  background: #f6f8fa; border: 1px solid #e3e6ea; border-radius: 6px;
  padding: 14px 16px; overflow-x: auto; line-height: 1.55;
}
pre code { background: none; padding: 0; color: #1f2328; font-size: 13px; }
/* 宽表格：外层允许横向滚动，不撑破页面 */
table {
  border-collapse: collapse; width: 100%; margin: 18px 0;
  font-size: 14px; display: block; overflow-x: auto;
}
th, td { border: 1px solid #d8dee4; padding: 8px 11px; text-align: left; vertical-align: top; }
th { background: #f2f4f7; font-weight: 600; white-space: nowrap; }
tr:nth-child(even) td { background: #fafbfc; }
blockquote {
  margin: 16px 0; padding: 8px 16px;
  border-left: 4px solid #0969da; background: #f6f9fd; color: #3a4149;
}
/* 打印：图片不跨页截断，A4 纵向 */
@media print {
  body { max-width: none; padding: 0; font-size: 11pt; }
  h2 { page-break-after: avoid; }
  img, table, pre { page-break-inside: avoid; }
}
"""

HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
{body}
</body>
</html>
"""


def embed_images(md_text: str, md_path: Path) -> tuple[str, list[str], list[str]]:
    """把 Markdown 里的本地图片引用替换为 base64 data URI。"""
    embedded: list[str] = []
    missing: list[str] = []

    def repl(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2)
        # 只处理本地相对路径；http(s) 外链原样保留
        if re.match(r"^(?:https?:)?//", src) or src.startswith("data:"):
            return match.group(0)
        img_path = (md_path.parent / src).resolve()
        if not img_path.is_file():
            missing.append(src)
            return match.group(0)
        mime = mimetypes.guess_type(img_path.name)[0] or "image/png"
        b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
        embedded.append(src)
        return f"![{alt}](data:{mime};base64,{b64})"

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, md_text), embedded, missing


def convert(md_path: Path) -> Path:
    text = md_path.read_text(encoding="utf-8")
    text, embedded, missing = embed_images(text, md_path)

    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
    )

    # 取首个 H1 作为标题
    m = re.search(r"^#\s+(.+)$", md_path.read_text(encoding="utf-8"), re.M)
    title = m.group(1).strip() if m else md_path.stem

    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / (md_path.stem + ".html")
    out.write_text(HTML_TMPL.format(title=title, css=CSS, body=body), encoding="utf-8")

    kb = out.stat().st_size / 1024
    print(f"OK  {md_path.name} -> {out.relative_to(ROOT)}  ({kb:.0f} KB, 内嵌 {len(embedded)} 图)")
    for s in missing:
        print(f"    !! 图片缺失，未内嵌: {s}")
    return out


def main() -> int:
    if len(sys.argv) > 1:
        targets = [Path(a).resolve() for a in sys.argv[1:]]
    else:
        targets = sorted(p for p in DOCS.glob("*.md"))

    if not targets:
        print("没有找到要转换的 .md 文件")
        return 1

    for t in targets:
        if not t.is_file():
            print(f"跳过（不存在）: {t}")
            continue
        convert(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
