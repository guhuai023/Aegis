# -*- coding: utf-8 -*-
"""方案书 Markdown -> DOCX 生成器。

用途：`docs/方案书.md` 是唯一事实来源，本脚本把它排成可交付的 DOCX（再由 Word 转 PDF）。
支持：标题层级、段落、无序/有序列表、表格、代码块、图片、粗体、水平线。
"""
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "方案书.md"
OUT = ROOT / "docs" / "方案书.docx"

CJK_FONT = "微软雅黑"
MONO_FONT = "Consolas"
ACCENT = RGBColor(0x17, 0x5C, 0xD3)
MUTED = RGBColor(0x66, 0x70, 0x85)


def _set_cjk(run, font=CJK_FONT):
    run.font.name = font
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)


def _shade(element, color="F2F4F7"):
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), color)
    element.append(shd)


def _border_bottom(paragraph, color="D0D5DD"):
    pPr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:color"), color)
    borders.append(bottom)
    pPr.append(borders)


def add_runs(paragraph, text, bold=False, mono=False, color=None, size=None):
    """处理 **粗体** 与 `行内代码`。"""
    parts = re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text)
    for part in parts:
        if not part:
            continue
        is_mono = mono
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            is_mono = True
            run.font.size = Pt(9.5)
            _shade(run._element.get_or_add_rPr(), "F5F5F5")
        else:
            run = paragraph.add_run(part)
        if bold:
            run.bold = True
        if color is not None:
            run.font.color.rgb = color
        if size is not None and not (part.startswith("`") and part.endswith("`")):
            run.font.size = Pt(size)
        _set_cjk(run, MONO_FONT if is_mono else CJK_FONT)


def heading(doc, text, level):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14 if level == 1 else 10)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.keep_with_next = True
    sizes = {0: 22, 1: 15, 2: 12.5}
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(sizes.get(level, 11))
    run.font.color.rgb = ACCENT if level <= 1 else RGBColor(0x10, 0x14, 0x20)
    _set_cjk(run)
    if level <= 1:
        _border_bottom(p)
    return p


def body(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing = 1.45
    add_runs(p, text)
    return p


def bullet(doc, text, numbered=False, num=None):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.35
    add_runs(p, text)
    return p


def numbered_item(doc, num, text):
    """手写序号的列表项：避免 Word 的 List Number 跨节连续编号（会从 4 开始）。"""
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.35
    p.paragraph_format.left_indent = Cm(0.95)
    p.paragraph_format.first_line_indent = Cm(-0.95)
    add_runs(p, f"{num}. {text}")
    return p


def code_block(doc, lines):
    for line in lines:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.05
        p.paragraph_format.left_indent = Cm(0.4)
        run = p.add_run(line if line else " ")
        run.font.name = MONO_FONT
        run._element.rPr.rFonts.set(qn("w:eastAsia"), MONO_FONT)
        run.font.size = Pt(9)
        _shade(p._p.get_or_add_pPr(), "F7F8FA")
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(4)


def table(doc, rows):
    cols = len(rows[0])
    t = doc.add_table(rows=len(rows), cols=cols)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    # 表头跨页重复、行不拆开（否则续页只剩半张表且看不到列名）
    for i, row in enumerate(rows):
        trPr = t.rows[i]._tr.get_or_add_trPr()
        cant = OxmlElement("w:cantSplit")
        trPr.append(cant)
        if i == 0:
            trPr.append(OxmlElement("w:tblHeader"))
        for j, cell_text in enumerate(row):
            cell = t.cell(i, j)
            cell.text = ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(2)
            add_runs(p, cell_text, bold=(i == 0), size=9.5 if i else 10)
            if i == 0:
                _shade(cell._tc.get_or_add_tcPr(), "EAF1FB")
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(6)


def image(doc, path, alt):
    full = (ROOT / "docs" / path).resolve()
    if not full.exists():
        body(doc, f"（缺图：{alt}）")
        return
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.keep_together = True       # 图片不被分页切开
    p.paragraph_format.keep_with_next = True      # 图题跟着图片走
    p.add_run().add_picture(str(full), width=Cm(15.4))
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = cap.add_run(alt)
    run.font.size = Pt(9)
    run.font.color.rgb = MUTED
    _set_cjk(run)


def parse_table(lines):
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
            continue                      # 分隔行
        rows.append(cells)
    return rows


def main():
    text = SRC.read_text(encoding="utf-8-sig")   # 容忍 BOM（PowerShell 写文件可能带）
    lines = text.splitlines()
    doc = Document()
    # 版面：A4 + 页边距
    section = doc.sections[0]
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.4)
    normal = doc.styles["Normal"]
    normal.font.name = CJK_FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT)

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):                       # 代码块
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            code_block(doc, block)
            i += 1
            continue

        if stripped.startswith("!["):                        # 图片
            m = re.match(r"!\[(.*?)\]\((.*?)\)", stripped)
            if m:
                image(doc, m.group(2), m.group(1))
            i += 1
            continue

        if stripped.startswith("|"):                         # 表格
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            table(doc, parse_table(block))
            continue

        if not stripped:
            i += 1
            continue

        if stripped.startswith("---"):
            i += 1
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            heading(doc, stripped[level:].strip(), level - 1 if level >= 2 else 0)
            i += 1
            continue

        m = re.match(r"^(\d+)\.\s+(.*)$", stripped)          # 有序列表
        if m:
            numbered_item(doc, m.group(1), m.group(2))
            i += 1
            continue

        if stripped.startswith(("- ", "* ")):
            bullet(doc, stripped[2:].strip())
            i += 1
            continue

        body(doc, stripped)
        i += 1

    doc.save(str(OUT))
    print("已生成", OUT, len(text), "字符源")


if __name__ == "__main__":
    sys.exit(main())