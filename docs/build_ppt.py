# -*- coding: utf-8 -*-
"""按 2026 未来杯官方模板生成 Aegis 方案讲解 PPT（v2，对齐定稿方案书）。

模板事实（已实测）：
    16:9  13.333 x 7.5 英寸
    版式：标题幻灯片(深蓝封面) / 标题和内容(白底+蓝色页眉带) / 两栏内容 / 节标题(深蓝) / 空白
    设计元素全部在版式层（整幅背景图 + 左右页眉 logo），页面只有文本框
    内容页小标题位于 (0.39, 0.90)，24 号加粗；正文 20 号

用法：填仓库根目录的 personal.json 四项，然后 python docs/build_ppt.py（直接覆盖 docs 里的交付件）。
依赖：官方模板 2026未来杯PPT模版.pptx（不在仓库内，需按 TEMPLATE 路径放置）；配图从 docs/images/ 读取。
"""
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

TEMPLATE = r"D:\下载\电影\2026未来杯PPT模版.pptx"
OUTPUT = r"D:\下载\项目1\Aegis-赛道一-方案讲解-v2.pptx"

# ---- 个人信息：优先读仓库根目录的 personal.json（不用改 Python） ----
# 留空则封面只显示标签（"团队名称：" 后面空着）。
import json as _json
import sys as _sys
from pathlib import Path as _Path

_PERSONAL_KEYS = ["队伍", "演讲人", "指导教师", "学校"]
PERSONAL = {k: "" for k in _PERSONAL_KEYS}


def _load_personal(path):
    """从 json 覆盖四项；文件不存在就保持空值。"""
    if path and _Path(path).exists():
        data = _json.loads(_Path(path).read_text(encoding="utf-8-sig"))
        for k in _PERSONAL_KEYS:
            v = str(data.get(k, "") or "").strip()
            if v:
                PERSONAL[k] = v


_load_personal(_Path(__file__).resolve().parent.parent / "personal.json")

# ---- 视觉常量：跟随模板（蓝底 + 白底蓝页眉） ----
FONT = "微软雅黑"          # 模板标注为思源黑体；本机未装 Bold 字重，统一用微软雅黑保证任何机器不乱版式
MONO = "Consolas"
C_TITLE = RGBColor(0x0F, 0x1B, 0x2D)
C_BODY = RGBColor(0x33, 0x3D, 0x4D)
C_MUTE = RGBColor(0x6B, 0x77, 0x88)
C_BLUE = RGBColor(0x0B, 0x49, 0xC3)
C_BLUE_MID = RGBColor(0xDC, 0xE8, 0xFB)
C_BLUE_LT = RGBColor(0xEE, 0xF3, 0xFD)
C_ORANGE = RGBColor(0xD9, 0x6A, 0x1B)
C_LINE = RGBColor(0xD6, 0xE0, 0xF0)
C_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
C_SUB = RGBColor(0xB9, 0xCB, 0xEF)   # 深色页上的次级文字

import argparse as _argparse

_ap = _argparse.ArgumentParser(description="生成 Aegis 方案讲解 PPT")
_ap.add_argument("--personal", default=None, help="个人信息 json（默认仓库根 personal.json）")
_ap.add_argument("--out", default=r"D:\gh666\Aegis\docs\Aegis-赛道一-方案讲解.pptx",
                 help="输出 pptx 路径")
_ap.add_argument("--template", default=TEMPLATE, help="官方模板路径")
_args = _ap.parse_args()
if _args.personal is not None:
    _load_personal(_args.personal)
OUTPUT = _args.out
TEMPLATE = _args.template

prs = Presentation(TEMPLATE)
LAY = {lay.name: lay for lay in prs.slide_layouts}
SLIDE_W, SLIDE_H = prs.slide_width, prs.slide_height


# ------------------------------------------------------------------ 基础工具

def _tf_setup(tf, wrap=True, anchor=MSO_ANCHOR.TOP):
    tf.word_wrap = wrap
    tf.vertical_anchor = anchor
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0


def textbox(slide, x, y, w, h, text="", size=18, bold=False, color=C_BODY,
            align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=1.15, font=FONT):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    _tf_setup(tf, anchor=anchor)
    p = tf.paragraphs[0]
    p.alignment = align
    p.line_spacing = spacing
    if text:
        r = p.add_run()
        r.text = text
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.name = font
        r.font.color.rgb = color
    return box


def add_para(tf, text, size=18, bold=False, color=C_BODY, space_before=0,
             indent=0, align=PP_ALIGN.LEFT, spacing=1.2, font=FONT, first=False):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.space_before = Pt(space_before)
    p.line_spacing = spacing
    p.alignment = align
    if indent:
        p.paragraph_format.left_indent = Inches(indent) if hasattr(p, "paragraph_format") else None
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.name = font
    r.font.color.rgb = color
    return p


def rect(slide, x, y, w, h, fill=None, line=None, radius=0.04, shape=MSO_SHAPE.ROUNDED_RECTANGLE):
    box = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        try:
            box.adjustments[0] = radius
        except Exception:
            pass
    if fill is None:
        box.fill.background()
    else:
        box.fill.solid()
        box.fill.fore_color.rgb = fill
    if line is None:
        box.line.fill.background()
    else:
        box.line.color.rgb = line
        box.line.width = Pt(0.75)
    box.shadow.inherit = False
    return box


def band(slide, x, y, w, h, title, sub="", fill=C_BLUE_LT, title_color=C_TITLE,
         title_size=15, sub_size=12, line=C_LINE):
    box = rect(slide, x, y, w, h, fill=fill, line=line)
    tf = box.text_frame
    _tf_setup(tf, anchor=MSO_ANCHOR.MIDDLE)
    tf.margin_left = Inches(0.16)
    tf.margin_right = Inches(0.10)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    r = p.add_run()
    r.text = title
    r.font.size = Pt(title_size)
    r.font.bold = True
    r.font.name = FONT
    r.font.color.rgb = title_color
    if sub:
        p2 = tf.add_paragraph()
        p2.space_before = Pt(2)
        r2 = p2.add_run()
        r2.text = sub
        r2.font.size = Pt(sub_size)
        r2.font.name = FONT
        r2.font.color.rgb = RGBColor(0xD8, 0xE4, 0xFB) if fill is C_BLUE else C_MUTE
    return box


def code_box(slide, text, x, y, w, h, size=11.5):
    box = rect(slide, x, y, w, h, fill=RGBColor(0xF5, 0xF8, 0xFD), line=C_LINE, radius=0.03)
    tf = box.text_frame
    _tf_setup(tf)
    tf.margin_left = Inches(0.16)
    tf.margin_right = Inches(0.10)
    tf.margin_top = Inches(0.10)
    tf.margin_bottom = Inches(0.06)
    for i, line in enumerate(text.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        p.line_spacing = 1.02
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size)
        r.font.name = MONO
        r.font.color.rgb = RGBColor(0x1F, 0x29, 0x37)
    return box


def table(slide, rows, x, y, w, col_w, row_h=0.34, size=11.5, header_fill=C_BLUE,
          zebra=C_BLUE_LT, body_color=C_BODY, bold_first_col=True):
    nrows, ncols = len(rows), len(rows[0])
    shape = slide.shapes.add_table(nrows, ncols, Inches(x), Inches(y),
                                   Inches(w), Inches(row_h * nrows))
    tbl = shape.table
    tbl.first_row = True
    tbl.horz_banding = False
    for i, cw in enumerate(col_w):
        tbl.columns[i].width = Inches(cw)
    for r in range(nrows):
        tbl.rows[r].height = Inches(row_h)
        for c in range(ncols):
            cell = tbl.cell(r, c)
            cell.margin_left = Inches(0.09)
            cell.margin_right = Inches(0.06)
            cell.margin_top = Inches(0.02)
            cell.margin_bottom = Inches(0.02)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            if r == 0:
                cell.fill.fore_color.rgb = header_fill
            elif r % 2 == 0:
                cell.fill.fore_color.rgb = zebra
            else:
                cell.fill.fore_color.rgb = C_WHITE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.line_spacing = 1.05
            r_obj = p.add_run()
            r_obj.text = str(rows[r][c])
            r_obj.font.size = Pt(size)
            r_obj.font.name = FONT
            r_obj.font.bold = (r == 0) or (bold_first_col and c == 0)
            r_obj.font.color.rgb = C_WHITE if r == 0 else body_color
    return tbl


def page_title(slide, text, width=11.6, size=24):
    """内容页小标题：位置与字号沿用模板（0.39, 0.90 / 24 号加粗）"""
    old = None
    for sh in list(slide.shapes):
        if sh.has_text_frame and sh.name.startswith("文本框"):
            old = sh
            break
    if old is not None:
        old.left, old.top = Inches(0.39), Inches(0.86)
        old.width, old.height = Inches(width), Inches(0.62)
        tf = old.text_frame
        tf.clear()
        _tf_setup(tf)
        p = tf.paragraphs[0]
        r = p.add_run()
        r.text = text
        r.font.size = Pt(size)
        r.font.bold = True
        r.font.name = FONT
        r.font.color.rgb = C_TITLE
        return old
    return textbox(slide, 0.39, 0.86, width, 0.62, text, size=size, bold=True, color=C_TITLE)


def rule(slide, y=1.58):
    rect(slide, 0.39, y, 12.55, 0.018, fill=C_LINE, radius=0.0, shape=MSO_SHAPE.RECTANGLE)


def notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


def new_content(title):
    slide = prs.slides.add_slide(LAY["两栏内容"])
    page_title(slide, title)
    rule(slide)
    return slide


def new_divider(num, title, sub):
    slide = prs.slides.add_slide(LAY["节标题"])
    for sh in list(slide.shapes):
        if sh.has_text_frame:
            sh._element.getparent().remove(sh._element)
    textbox(slide, 0.39, 2.35, 6.2, 1.15, num, size=72, bold=True,
            color=RGBColor(0x5E, 0x8B, 0xE8))
    textbox(slide, 0.39, 3.72, 9.0, 0.72, title, size=32, bold=True, color=C_WHITE)
    textbox(slide, 0.42, 4.46, 9.0, 0.55, sub, size=16, color=C_SUB)
    rect(slide, 0.39, 3.56, 2.2, 0.055, fill=RGBColor(0x5E, 0x8B, 0xE8), radius=0,
         shape=MSO_SHAPE.RECTANGLE)
    return slide


def chip(slide, text, x, y, w, h=0.42, fill=C_BLUE, color=C_WHITE, size=13):
    box = rect(slide, x, y, w, h, fill=fill, line=None, radius=0.28)
    tf = box.text_frame
    _tf_setup(tf, anchor=MSO_ANCHOR.MIDDLE)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = True
    r.font.name = FONT
    r.font.color.rgb = color
    return box


# ================================================================== 1 封面
s1 = prs.slides[0]
for sh in s1.shapes:
    if not sh.has_text_frame:
        continue
    if "演讲题目" in sh.text_frame.text:
        tf = sh.text_frame
        tf.clear()
        _tf_setup(tf)
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = "Aegis：可编排、可验收的多 Agent 协作平台"
        r.font.size = Pt(32)
        r.font.bold = True
        r.font.name = FONT
        r.font.color.rgb = C_WHITE
    elif "团队名称" in sh.text_frame.text:
        tf = sh.text_frame
        tf.clear()
        _tf_setup(tf)
        sh.left, sh.top, sh.width = Inches(5.55), Inches(3.18), Inches(4.30)
        rows = [("团队名称：", PERSONAL["队伍"]), ("演讲人：", PERSONAL["演讲人"]),
                ("指导教师：", PERSONAL["指导教师"]), ("学校：", PERSONAL["学校"])]
        for i, (label, val) in enumerate(rows):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = PP_ALIGN.LEFT
            p.space_before = Pt(6)
            r1 = p.add_run()
            r1.text = label
            r1.font.size = Pt(20)
            r1.font.bold = True
            r1.font.name = FONT
            r1.font.color.rgb = C_WHITE
            r2 = p.add_run()
            r2.text = val or "　"
            r2.font.size = Pt(20)
            r2.font.bold = True
            r2.font.name = FONT
            r2.font.color.rgb = C_WHITE
_note = ("开场：大家好，我们的作品是 Aegis —— 面向软件交付的多 Agent 协同平台。"
         "一句话：我们不追求更大的模型，而是把 Agent 协作做成可解释、可复现、可审计的工程系统。")
if any(not PERSONAL[k] for k in _PERSONAL_KEYS):
    _note += "（请在封面补团队名称、演讲人、指导教师、学校）"
notes(s1, _note)

# ================================================================== 2 目录
s2 = prs.slides[1]
for sh in s2.shapes:
    if not sh.has_text_frame:
        continue
    txt = sh.text_frame.text
    if txt.strip().startswith("目\n录") or txt.strip() == "目\n录" or "目录标题" in txt:
        if "目录标题" in txt:
            box = sh
            box.left, box.top = Inches(5.55), Inches(1.42)
            box.width, box.height = Inches(7.15), Inches(5.40)
            tf = box.text_frame
            tf.clear()
            _tf_setup(tf)
            tf.word_wrap = True
            toc = [
                ("一、问题与判断", True), ("1.1 赛题点名的三个痛点", False),
                ("1.2 我们的三条判断", False),
                ("二、架构：把编排变成数据", True), ("2.1 分层架构与任务黑板", False),
                ("2.2 控制结构全覆盖", False),
                ("三、可信交付：让“通过”有物理依据", True), ("3.1 物理锚点验收", False),
                ("3.2 修复—熔断—人工", False),
                ("四、验证、实证与下一步", True), ("4.1 测试与等价性验证", False),
                ("4.2 对照赛题与决赛计划", False),
            ]
            for i, (line, head) in enumerate(toc):
                p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                p.alignment = PP_ALIGN.LEFT
                p.space_before = Pt(9 if head else 1)
                r = p.add_run()
                r.text = line
                r.font.size = Pt(19 if head else 14)
                r.font.bold = head
                r.font.name = FONT
                r.font.color.rgb = C_WHITE if head else C_SUB
notes(s2, "整份汇报分四部分：先讲问题和判断，再讲架构如何把编排变成数据，"
          "然后讲可信交付的自我循环，最后给验证数据和下一步计划。")

# ================================================================== 3 章节一（复用模板第 3 页）
s3 = prs.slides[2]
for sh in list(s3.shapes):
    if sh.has_text_frame:
        sh._element.getparent().remove(sh._element)
textbox(s3, 0.39, 2.35, 6.2, 1.15, "01", size=72, bold=True,
        color=RGBColor(0x5E, 0x8B, 0xE8))
textbox(s3, 0.39, 3.72, 9.0, 0.72, "一、问题与判断", size=32, bold=True, color=C_WHITE)
textbox(s3, 0.42, 4.46, 9.6, 0.55,
        "赛题点名的问题，恰好是我们想重做的东西", size=16, color=C_SUB)
rect(s3, 0.39, 3.56, 2.2, 0.055, fill=RGBColor(0x5E, 0x8B, 0xE8), radius=0,
     shape=MSO_SHAPE.RECTANGLE)

# ================================================================== 4 痛点与判断（复用模板第 4 页）
s4 = prs.slides[3]
page_title(s4, "赛题痛点到我们的三条判断")
rule(s4)

box_l = rect(s4, 0.39, 1.78, 6.05, 3.55, fill=RGBColor(0xF7, 0xF9, 0xFD), line=C_LINE)
tf = box_l.text_frame
_tf_setup(tf)
tf.margin_left = Inches(0.22)
tf.margin_right = Inches(0.18)
tf.margin_top = Inches(0.18)
add_para(tf, "赛题点名的三个痛点", size=16, bold=True, color=C_BLUE, first=True)
for t in ["编排能力薄弱：脚本化 / 硬编码流水线，缺条件分支、循环控制与异常回退",
          "调度策略单一：串行或广播式通信，无优先级、负载与能力画像",
          "验证机制缺失：中间结果无人核验，幻觉与错误传播难以收敛"]:
    add_para(tf, "· " + t, size=13.5, color=C_BODY, space_before=10, spacing=1.3)

box_r = rect(s4, 6.74, 1.78, 6.20, 3.55, fill=C_BLUE_LT, line=C_BLUE_MID)
tf = box_r.text_frame
_tf_setup(tf)
tf.margin_left = Inches(0.22)
tf.margin_right = Inches(0.18)
tf.margin_top = Inches(0.18)
add_para(tf, "我们的三条判断", size=16, bold=True, color=C_BLUE, first=True)
for n, l1, l2 in [("①", "编排必须是数据，不是代码", "流程写进 YAML，引擎只负责解释"),
                  ("②", "“通过”必须有物理依据", "真实执行 + 断言，而不是让模型自评"),
                  ("③", "失败是常态，要有预算与出口", "修两次仍不过就熔断，升级给人类")]:
    p = add_para(tf, n + " " + l1, size=14, color=C_TITLE, space_before=11, spacing=1.3)
    p.runs[0].font.bold = False
    add_para(tf, "　" + l2, size=13, color=C_BODY, space_before=2, spacing=1.3)

concl = rect(s4, 0.39, 5.62, 12.55, 0.82, fill=C_BLUE, line=None)
tf = concl.text_frame
_tf_setup(tf, anchor=MSO_ANCHOR.MIDDLE)
p = tf.paragraphs[0]
p.alignment = PP_ALIGN.CENTER
r = p.add_run()
r.text = "结论：不追求“更大的模型”，而是把 Agent 协作做成可解释、可复现、可审计的工程系统"
r.font.size = Pt(15)
r.font.bold = True
r.font.name = FONT
r.font.color.rgb = C_WHITE
notes(s4, "赛题背景页把“硬编码流水线”列为要解决的痛点之首——而这正是大多数 Agent Demo 的现状。"
          "我们的判断是：编排要变成数据；验收要有物理依据；失败要有预算和出口。")

# ================================================================== 5 章节二
new_divider("02", "二、架构：把编排变成数据",
            "workflows/*.yaml 定义流程，agents/*.yaml 定义角色，引擎只做解释")

# ================================================================== 6 五层架构
s6 = new_content("五层架构：引擎里没有一行业务分支代码")
bands6 = [
    ("声明式定义层　workflows/*.yaml · agents/*.yaml",
     "改流程不改代码 · 保存即热加载（按 mtime 缓存）"),
    ("编排层　core/engine.py + core/workflow.py",
     "认领 → 按 node.type 执行 → 算下一步 → 落库；4 类节点 / 5 类转移"),
    ("智能层　core/agents.py + agents/*.yaml",
     "Function Calling 工具循环（≤4 轮）；角色、温度、工具集都在声明里"),
    ("工具层　core/tools.py",
     "任务级 venv / 沙箱超时 / 8 类断言 / 节点级产物隔离 / 入参夹具落地"),
    ("状态层　core/db.py（SQLite WAL）",
     "原子认领 · 优先级老化 · trace 记忆 · 僵尸回收 · 引擎单写者租约"),
]
y = 1.80
for i, (title_, sub_) in enumerate(bands6):
    band(s6, 0.39, y, 12.55, 0.52, title_, sub=sub_,
         fill=RGBColor(0xF7, 0xF9, 0xFD) if i % 2 == 0 else C_BLUE_LT)
    y += 0.65
note = rect(s6, 0.39, 5.18, 12.55, 0.60, fill=C_BLUE, line=None)
tf = note.text_frame
_tf_setup(tf, anchor=MSO_ANCHOR.MIDDLE)
p = tf.paragraphs[0]
p.alignment = PP_ALIGN.CENTER
r = p.add_run()
r.text = "引擎只解释、不判断业务：加节点、换失败路径、改人工出口，都只动 YAML"
r.font.size = Pt(14)
r.font.bold = True
r.font.name = FONT
r.font.color.rgb = C_WHITE
textbox(s6, 0.39, 5.90, 12.55, 0.40,
        "六条工作流共享同一引擎：code_gen · data_pipeline · log_triage · "
        "data_quality_gate · excel_report · parallel_demo",
        size=11.5, color=C_MUTE)
notes(s6, "分层的意义：声明层改动零成本；引擎层只做与业务无关的四件事；"
          "工具层承担“真执行 + 真验收”；状态层保证崩溃不丢、多会话不抢。"
          "六条工作流全部跑在这个引擎上，没有一条改过引擎代码。")

# ================================================================== 7 编排即数据
s7 = new_content("编排即数据：一个字段 = 一种控制结构")
code_box(s7, """- id: verify
  type: tool
  tool: execute_spec
  on_success: summarize   # 成功往前走
  on_failure: fixer       # 失败走回边
  max_retries: 1          # 修复预算
  on_exhausted: human     # 预算耗尽→熔断

- id: human
  type: human
  on_resume: fixer        # 人工建议→恢复""",
         0.39, 1.78, 6.00, 3.30, size=10.5)
textbox(s7, 0.39, 5.16, 6.00, 0.34, "workflows/log_triage.yaml（节选）", size=10.5, color=C_MUTE)

rows7 = [["字段", "语义"],
         ["routes", "条件分支：按文本标签选目标"],
         ["next / on_success", "顺序推进"],
         ["on_failure + max_retries", "失败回边 + 修复预算"],
         ["on_exhausted", "预算用尽 → 熔断转人工"],
         ["on_resume", "人工下发建议后恢复"],
         ["parallel + join", "并行扇出 + 汇合等待"],
         ["wait_timeout", "汇合等待上限"],
         ["sub_workflow", "已解析校验，执行明确拒绝"]]
table(s7, rows7, 6.62, 1.78, 6.32, [2.32, 4.00], row_h=0.40, size=10.5)

bar7 = rect(s7, 0.39, 5.62, 12.55, 0.72, fill=C_BLUE_LT, line=C_BLUE_MID)
tf = bar7.text_frame
_tf_setup(tf, anchor=MSO_ANCHOR.MIDDLE)
tf.margin_left = Inches(0.20)
p = tf.paragraphs[0]
p.alignment = PP_ALIGN.LEFT
r = p.add_run()
r.text = "写错一个字段，加载期就报错（悬空目标 / 重试无 on_failure / 入参路径逃逸）；"
r.font.size = Pt(12.5)
r.font.bold = True
r.font.name = FONT
r.font.color.rgb = C_BLUE
r2 = p.add_run()
r2.text = "sub_workflow 执行尚未实现 —— 明确报错，不假装支持"
r2.font.size = Pt(12.5)
r2.font.name = FONT
r2.font.color.rgb = C_ORANGE
notes(s7, "这一页回答“编排创新在哪”：控制结构是声明出来的，不是写死在代码里。"
          "同一个 verify 节点，改三个字段就从“自动修复”变成“直接停机”——门禁场景就是这么来的。")

# ================================================================== 8 五个 Agent
s8 = new_content("五个 Agent：职责质上不同，不只是量上不同")
rows8 = [["Agent", "职责", "工具", "为什么单独拆"],
         ["router", "分诊：只判 [CODE] / [CHAT]", "无", "二分类混进生成会污染判断；限 64 tokens"],
         ["coder", "产出代码 + 断言规格", "list_files\nread_file", "只给只读工具，避免自己造文件"],
         ["fixer", "复现失败 → 归因 → 全量重写", "+ run_python", "温度 0，修复要可复现"],
         ["analyst", "拆解步骤 / 字段 / 口径", "list_files\nread_file", "“想清楚再动手”独立成节点"],
         ["chat", "闲聊兜底", "无", "与工程任务彻底分流"]]
table(s8, rows8, 0.39, 1.78, 12.55, [1.35, 3.55, 2.35, 5.30], row_h=0.52, size=11)

box8 = rect(s8, 0.39, 5.02, 12.55, 1.32, fill=RGBColor(0xF7, 0xF9, 0xFD), line=C_LINE)
tf = box8.text_frame
_tf_setup(tf)
tf.margin_left = Inches(0.22)
tf.margin_top = Inches(0.12)
add_para(tf, "一条硬规则：role_prompt 只写「能力与纪律」，输出格式由调用它的编排节点给",
         size=13.5, bold=True, color=C_BLUE, first=True)
add_para(tf, "踩过的坑：analyst 的提示词里曾写死「支线A / 支线B」三段模板，"
             "另一条工作流调它时拿到了别的流程的格式 —— 能力层与编排层必须分开。",
         size=11.5, color=C_BODY, space_before=6, spacing=1.28)
notes(s8, "五个角色不是把一个大 Agent 拆成五个名字，而是判断标准互相冲突的部分分开："
          "生成要敢写，验收要敢否定，修复要能复现失败。")

# ================================================================== 9 六个工作流 + 企业场景
s9 = new_content("六条工作流：三条行业线 + 三个企业日常场景")
rows9 = [["工作流", "业务场景", "入参（examples/）", "交付物", "失败策略"],
         ["code_gen", "软件交付：一句话 → 带验收的代码", "无（自生成）", "main.py + 报告", "修复 ≤2 次→转人工"],
         ["log_triage", "运维值班：nginx 日志 → 周报", "120 行日志", "report.md", "修复 1 次→转人工"],
         ["data_quality_gate", "数仓上游：脏表清洗 + 门禁", "45 行脏数据", "清洗表 + 质量报告", "门禁：直接终止"],
         ["excel_report", "周会报表：明细 → xlsx", "24 行销售", "明细 + 汇总 + 合计", "修复 1 次→转人工"],
         ["data_pipeline", "数据产线：拆解→并行→合并", "无（自生成）", "两份明细 + summary", "验收失败即终止"],
         ["parallel_demo", "并行编排演示", "无", "汇总文本", "—"]]
table(s9, rows9, 0.39, 1.78, 12.55, [2.00, 3.35, 2.55, 2.60, 2.05], row_h=0.46, size=10.5)

bar9 = rect(s9, 0.39, 5.28, 12.55, 1.05, fill=C_BLUE_LT, line=C_BLUE_MID)
tf = bar9.text_frame
_tf_setup(tf)
tf.margin_left = Inches(0.22)
tf.margin_top = Inches(0.10)
add_para(tf, "实测（真实模型 / 真沙箱 / 真库）：6 条全绿，断言 3–13 条，单次成本 365–19,311 tokens",
         size=13, bold=True, color=C_BLUE, first=True)
add_para(tf, "三个企业场景的产物经人工核算：日志 120 请求 / 5xx 20 / 占比 16.67%；"
             "清洗后重复 0、空值 0；xlsx 汇总合计 32312.00 —— 与独立统计脚本逐项一致",
         size=11.5, color=C_BODY, space_before=6, spacing=1.28)
notes(s9, "三个企业场景是本轮新增的重点：它们不是演示用的玩具，而是带固定入参、"
          "产物可核算的真实工作流。门禁那条故意不自动修复——错数据被悄悄改好比失败更危险。")

# ================================================================== 10 章节三
new_divider("03", "三、可信交付：让“通过”有物理依据",
            "真执行 + 断言 + 产物校验 + 节点级隔离，四道关卡才叫“通过”")

# ================================================================== 11 物理验收
s11 = new_content("物理验收：8 类断言，判定发生在 tool 节点")
kinds = [("CONTAINS", "输出含文本"), ("NOT_CONTAINS", "输出不含文本"),
         ("REGEX", "输出按行匹配"), ("FILE_EXISTS", "产物存在且非空"),
         ("FILE_MIN_SIZE", "产物大小下限"), ("REGEX_FILE", "直接校验产物内容"),
         ("EXIT_CODE", "校验退出码"), ("NEAR", "数值落在容差内")]
xs = 0.39
for name_, desc in kinds:
    chip(s11, name_, xs, 1.78, 1.50, h=0.44, size=10.5)
    textbox(s11, xs + 0.02, 2.28, 1.46, 0.50, desc, size=9.5, color=C_MUTE, spacing=1.1)
    xs += 1.585

items = [("① 真执行", "任务级 venv 按依赖清单装包，subprocess 真实跑 main.py；退出码非 0 直接否决，不看模型自我评价"),
         ("② 产物断言", "运行期新增文件登记为交付物；FILE_EXISTS + REGEX_FILE 校验“真的生成了、内容也对”"),
         ("③ 节点级隔离", "同一节点重试先清掉自己上一轮的产物，杜绝“上一次的文件骗过这一次的断言”")]
y = 2.92
for name_, sub in items:
    box = rect(s11, 0.39, y, 12.55, 0.80, fill=RGBColor(0xF7, 0xF9, 0xFD), line=C_LINE)
    tf = box.text_frame
    _tf_setup(tf, anchor=MSO_ANCHOR.MIDDLE)
    tf.margin_left = Inches(0.20)
    tf.margin_right = Inches(0.20)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.LEFT
    r = p.add_run()
    r.text = name_ + "　"
    r.font.size = Pt(14)
    r.font.bold = True
    r.font.name = FONT
    r.font.color.rgb = C_BLUE
    r2 = p.add_run()
    r2.text = sub
    r2.font.size = Pt(11.5)
    r2.font.name = FONT
    r2.font.color.rgb = C_BODY
    y += 0.94

fin = rect(s11, 0.39, 5.96, 12.55, 0.60, fill=C_BLUE, line=None)
tf = fin.text_frame
_tf_setup(tf, anchor=MSO_ANCHOR.MIDDLE)
p = tf.paragraphs[0]
p.alignment = PP_ALIGN.CENTER
r = p.add_run()
r.text = "验收是独立节点：生成方只负责写，判定发生在 tool 节点 —— 想靠“改松断言”通过验收，做不到"
r.font.size = Pt(13)
r.font.bold = True
r.font.name = FONT
r.font.color.rgb = C_WHITE
notes(s11, "断言不是新鲜概念，难的是真的执行、真的校验产物。"
          "本轮断言抓下三类“模型自认为成功”：文件名不一致、总额小数不一致、散文被当代码。")

# ================================================================== 12 自我循环
s12 = new_content("自我循环：执行 → 验证 → 修正 → 熔断 → 人工 → 恢复")
textbox(s12, 0.39, 1.66, 6.4, 0.34, "真实运行记录（trace 43cbcac1）", size=12, bold=True, color=C_BLUE)
steps = [("15:55:44", "启动：分诊判定 [CODE]，进入开发节点", C_BLUE),
         ("15:55:48", "第 1 次验收失败：ZeroDivisionError，退出码非 0", RGBColor(0xC0, 0x39, 0x2B)),
         ("15:55:50", "修复 #1 失败：仍在 10/0 上崩", RGBColor(0xC0, 0x39, 0x2B)),
         ("15:55:52", "修复 #2 失败：修复预算用尽", RGBColor(0xC0, 0x39, 0x2B)),
         ("15:55:53", "熔断：转入人工介入，任务置 waiting_human", C_ORANGE),
         ("15:56:23", "人工建议下发：捕获异常并提示“除数不能为零”", C_BLUE),
         ("15:56:25", "修复预算重置 → 第 3 次验收通过", RGBColor(0x1E, 0x8E, 0x3E))]
y = 2.06
for t_, desc, color in steps:
    rect(s12, 0.45, y + 0.10, 0.10, 0.10, fill=color, radius=0.5, shape=MSO_SHAPE.OVAL)
    textbox(s12, 0.72, y, 0.95, 0.32, t_, size=11.5, color=C_MUTE, font=MONO)
    textbox(s12, 1.80, y, 4.95, 0.32, desc, size=11.5, color=C_TITLE)
    y += 0.58

right = rect(s12, 7.05, 1.74, 5.90, 4.42, fill=C_BLUE_LT, line=C_BLUE_MID)
tf = right.text_frame
_tf_setup(tf)
tf.margin_left = Inches(0.24)
tf.margin_right = Inches(0.20)
tf.margin_top = Inches(0.16)
add_para(tf, "四个闭环，各有停机判据", size=15, bold=True, color=C_BLUE, first=True)
for t_ in ["需求本身自相矛盾（既要真实执行 10/0，又禁止 try/except）—— 平台没有死循环：预算 2 次用尽即熔断",
           "人工一句话把 Agent 拉回正轨：建议写入 trace 记忆，修复预算归零重来",
           "模型调用级重试只用于抖动（超时/限流/5xx），代码错误不重试",
           "工具轮数用尽会做一次“不带工具”的收尾调用，拿不到结论才判失败"]:
    add_para(tf, "· " + t_, size=11.5, color=C_BODY, space_before=9, spacing=1.3)
add_para(tf, "另有一条运维级闭环：引擎启动抢租约（心跳 20s / 90s 过期接管），"
             "避免旧进程用旧 Agent 快照抢走新任务。",
         size=11, color=C_BLUE, space_before=10, spacing=1.28)
notes(s12, "这条时间线是真实日志。重点讲：熔断不是失败，是设计——预算用尽就把问题交给人类。"
           "另外三条闭环分别防模型抖动、防“查了半天不说话”、防旧进程抢任务。")

# ================================================================== 13 演示实拍
s13 = new_content("演示实拍：控制台、编排画布与真实产物")
textbox(s13, 0.39, 1.66, 6.6, 0.32, "执行控制台：KPI / 拓扑 / 节点明细 / 日志 / 交付物", size=11, bold=True, color=C_BLUE)
textbox(s13, 7.28, 1.66, 5.6, 0.32, "编排画布：三条企业场景（可切换、可改、保存即热加载）",
        size=11, bold=True, color=C_BLUE)

IMG = r"D:\gh666\Aegis\docs\images"


def add_pic(slide, path, x, y, w):
    """按宽度放图，返回图片底边的英寸位置（用于紧随其后的图注）"""
    pic = slide.shapes.add_picture(path, Inches(x), Inches(y), width=Inches(w))
    return y + pic.height / 914400.0


bottom = add_pic(s13, IMG + r"\console.png", 0.39, 2.02, 6.10)
textbox(s13, 0.39, bottom + 0.08, 6.10, 0.30,
        "同一条 trace 的全过程可回放：每一步、每条断言、每份产物都落库",
        size=10, color=C_MUTE)

y13 = 2.02
for fname, cap in [("canvas_log_triage.png", "运维值班周报 / log_triage"),
                   ("canvas_data_quality_gate.png", "数仓质量门禁 / data_quality_gate"),
                   ("canvas_excel_report.png", "Excel 周会报表 / excel_report")]:
    b13 = add_pic(s13, IMG + "\\" + fname, 7.26, y13, 5.30)
    textbox(s13, 7.26, b13 + 0.05, 5.30, 0.26, cap, size=9, color=C_MUTE)
    y13 = b13 + 0.42
notes(s13, "演示环节：左边控制台展示一条真实 trace 的全貌，右边三条企业场景的画布。"
           "强调画布是自绘 SVG（离线可用），保存后引擎按 mtime 热加载。")

# ================================================================== 14 验证与实证
s14 = new_content("验证与实证：158 项测试 + 四层真跑 + 真实成本")
textbox(s14, 0.39, 1.66, 6.1, 0.34, "四层真跑（不是断言代码形状）", size=12, bold=True, color=C_BLUE)
rows14 = [["层", "覆盖", "结果"],
          ["控制结构矩阵", "分支/回边/预算/熔断/并行/拒绝/回收/并发认领", "14/14"],
          ["调度器", "两 Worker 能力分区 · 快照 · LLM 并发闸门", "9/9"],
          ["工具边界", "沙箱超时 · 路径逃逸拒绝 · venv 真装包", "11/11"],
          ["前端交互", "CDP 真点击：深链/六区/画布编辑与保存", "30/30"]]
table(s14, rows14, 0.39, 2.04, 6.10, [1.35, 3.85, 0.90], row_h=0.46, size=10)
textbox(s14, 0.39, 4.42, 6.10, 0.62,
        "四层验证脚本 820 行 + 测试代码 1,975 行，全部可离线复现（scripts/verify/）",
        size=10, color=C_MUTE, spacing=1.2)

boxes14 = [("158 项离线测试", "打桩 LLM 与工具，不联网：引擎/断言/隔离/调度/画布/入参/租约/降级"),
           ("六条工作流真跑", "真实模型 + 真沙箱：断言 3–13 条，成本 365–19,311 tokens，产物可核算"),
           ("一次命令复现", "python -m unittest discover -s tests -t .  →  Ran 158 tests, OK")]
y = 1.78
for name_, sub in boxes14:
    box = rect(s14, 6.72, y, 6.22, 1.20, fill=RGBColor(0xF7, 0xF9, 0xFD), line=C_LINE)
    tf = box.text_frame
    _tf_setup(tf)
    tf.margin_left = Inches(0.20)
    tf.margin_top = Inches(0.10)
    add_para(tf, name_, size=13, bold=True, color=C_BLUE, first=True)
    add_para(tf, sub, size=10.5, color=C_BODY, space_before=5, spacing=1.25)
    y += 1.34

bar14 = rect(s14, 0.39, 5.98, 12.55, 0.58, fill=C_BLUE_LT, line=C_BLUE_MID)
tf = bar14.text_frame
_tf_setup(tf, anchor=MSO_ANCHOR.MIDDLE)
tf.margin_left = Inches(0.20)
p = tf.paragraphs[0]
p.alignment = PP_ALIGN.LEFT
r = p.add_run()
r.text = "本轮验证抓出并修掉 7 个真实缺陷："
r.font.size = Pt(12)
r.font.bold = True
r.font.name = FONT
r.font.color.rgb = C_BLUE
r2 = p.add_run()
r2.text = "sub_workflow 假成功 · 坏依赖拖死 trace · 工具轮不收敛 · 画布丢注释 · 散文当代码 · Agent 格式串味 · 旧进程抢任务"
r2.font.size = Pt(11)
r2.font.name = FONT
r2.font.color.rgb = C_BODY
notes(s14, "可靠性这页不讲“很稳”，讲证据：158 项测试、四层真跑、六条工作流成本，"
           "以及验证过程中真被我们抓出来修掉的 7 个缺陷。")

# ================================================================== 15 对照赛题 + 决赛
s15 = new_content("对照赛题六大能力，以及决赛要补的三件事")
rows15 = [["赛题要求", "现状", "判定"],
          ["Agent 团队定义与管理", "5 个角色 · 工具集 / 记忆 / 多版本 / 热加载", "已具备"],
          ["工作流编排", "YAML 声明：顺序/条件/回边/预算/并行/汇合/人工", "已具备"],
          ["智能调度引擎", "原子认领 + 优先级 + 老化 + 能力分区（默认单 Worker）", "基础具备"],
          ["工具与外部集成", "工具注册表 + 任务级 venv 真执行（MCP 未接入）", "部分"],
          ["执行监控与验证", "日志流 / 拓扑 / 8 类断言 / 人工节点 / 产物校验", "已具备"],
          ["反馈与自我优化", "失败链 + 预算重置 + 人工恢复（Prompt 自适应待做）", "部分"]]
table(s15, rows15, 0.39, 1.74, 7.85, [2.40, 4.30, 1.15], row_h=0.44, size=10)

box = rect(s15, 8.40, 1.74, 4.54, 3.20, fill=C_BLUE_LT, line=C_BLUE_MID)
tf = box.text_frame
_tf_setup(tf)
tf.margin_left = Inches(0.22)
tf.margin_right = Inches(0.16)
tf.margin_top = Inches(0.14)
add_para(tf, "决赛要补的三件事", size=14, bold=True, color=C_BLUE, first=True)
for n, t_ in [("①", "独立验收 Agent：需求侧独立产出断言，与实现侧交叉比对"),
              ("②", "多 Worker 能力画像调度的量化对比（含延迟与成功率）"),
              ("③", "基线实验：单 Agent / 串行无验证 / Aegis 同一任务集对比")]:
    add_para(tf, n + " " + t_, size=11, color=C_BODY, space_before=9, spacing=1.28)
add_para(tf, "已明确不做的：向量 RAG（供应商无 embedding）、sub_workflow 展开执行",
         size=10.5, color=C_ORANGE, space_before=10, spacing=1.25)

bar = rect(s15, 0.39, 5.30, 12.55, 0.86, fill=C_BLUE, line=None)
tf = bar.text_frame
_tf_setup(tf, anchor=MSO_ANCHOR.MIDDLE)
p = tf.paragraphs[0]
p.alignment = PP_ALIGN.CENTER
r = p.add_run()
r.text = "一句话收尾：Aegis 把 Agent 协作从“看起来能跑”推进到“可以验收、可以复现、可以追责”"
r.font.size = Pt(15)
r.font.bold = True
r.font.name = FONT
r.font.color.rgb = C_WHITE
notes(s15, "六大能力四项已具备、两项部分具备，待做项主动标出且都可量化。"
           "最后一句话是整场的落点：验收、复现、追责。")# ================================================================== 删除模板的"大会PPT提示“页
tip_idx = None
for i, slide in enumerate(prs.slides):
    for sh in slide.shapes:
        if sh.has_text_frame and "大会PPT提示" in sh.text_frame.text:
            tip_idx = i
            break
    if tip_idx is not None:
        break
if tip_idx is not None:
    sldIdLst = prs.slides._sldIdLst
    ids = list(sldIdLst)
    rId = ids[tip_idx].get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    prs.part.drop_rel(rId)
    sldIdLst.remove(ids[tip_idx])

# ================================================================== 把"谢谢观看“移到最后
sldIdLst = prs.slides._sldIdLst
ids = list(sldIdLst)
thanks = None
for el in ids:
    rId = el.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    slide_part = prs.part.related_part(rId)
    try:
        txt = "\n".join(sh.text_frame.text for sh in slide_part.slide.shapes if sh.has_text_frame)
    except Exception:
        txt = ""
    if "谢谢观看" in txt:
        thanks = el
        break
if thanks is not None:
    sldIdLst.remove(thanks)
    sldIdLst.append(thanks)

# 结束页沿用模板内容，但字体统一成正文那套：模板里写的是思源黑体，缺失时会回退成衬线体
_last = prs.slides[len(prs.slides) - 1]
for sh in _last.shapes:
    if not sh.has_text_frame:
        continue
    for para in sh.text_frame.paragraphs:
        for run in para.runs:
            run.font.name = FONT

# 给章节页与结束页补讲解备注（正文页的备注在各页构造时已写入）
notes(prs.slides[2], "第一部分：先对齐问题。赛题把「硬编码流水线」列为痛点之首，"
                     "而这恰好是大多数 Agent Demo 的现状——我们的判断是编排必须是数据。")
notes(prs.slides[4], "第二部分：讲架构。一句话概括——把编排从代码搬到 YAML，引擎退化成解释器。")
notes(prs.slides[9], "第三部分：讲可信交付。重点是真执行、真断言，以及失败时的熔断与人工恢复。")
notes(prs.slides[15], "演示结束，进入提问环节。备用材料：docs/编排引擎改造说明.md、tests/test_engine.py，"
                      "以及仓库里的 workflows/*.yaml（评委可以现场改一行看拓扑图变化）。")

prs.save(OUTPUT)
print("saved:", OUTPUT)
print("slides:", len(prs.slides))