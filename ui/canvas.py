# -*- coding: utf-8 -*-
"""编排画布：把 WorkflowSpec 画成分层 SVG（离线、零依赖、边语义可控）。

为什么自己画，而不是 mermaid / graphviz：
  · mermaid 需要从 CDN 拉 JS，比赛现场一断网就白屏；
  · graphviz（st.graphviz_chart）只能出静态图，边标签容易乱，也没法给
    "成功 / 失败回边 / 条件分支 / 并行 / 汇合 / 人工恢复"分别上色；
  · SVG 自绘的布局只有三十来行（BFS 分层 + join 依赖下沉），坐标还能被测试断言。

对外三个纯函数（都可离线测试）：
    layout(spec)  -> {节点 id: (x, y)}，终态画在 __done / __failed
    edges(spec)   -> [{src, dst, kind, label}]，只包含定义里真实存在的转移
    render(spec)  -> 自包含 SVG 的 HTML 片段
"""
from __future__ import annotations

import html
from collections import deque

from core.workflow import TERMINAL, WorkflowSpec

NODE_W, NODE_H = 186, 60
COL_W, ROW_H = 252, 110
PAD_X, PAD_Y = 34, 42
TERM_R = 13

TYPE_STYLE = {                      # (填充, 描边, 文字)
    "llm": ("#EFF4FF", "#175CD3", "#0B3B8C"),
    "tool": ("#E7F5EC", "#067647", "#05603A"),
    "human": ("#F4F0FE", "#5925DC", "#3E1C96"),
    "join": ("#F2F4F7", "#475467", "#344054"),
}
TYPE_NAME = {"llm": "大模型", "tool": "工具", "human": "人工", "join": "汇合"}

# (描边色, dash, 默认标签)
EDGE_STYLE = {
    "on_success": ("#475467", "", "成功"),
    "next": ("#667085", "", ""),
    "route": ("#B54708", "4 3", ""),
    "parallel": ("#175CD3", "", "并行"),
    "on_failure": ("#B42318", "5 3", "失败"),
    "on_exhausted": ("#B42318", "2 3", "预算耗尽"),
    "on_resume": ("#5925DC", "6 3", "人工恢复"),
    "join": ("#98A2B3", "1 4", "汇合"),
}
TERMINAL_STYLE = {"done": ("#067647", "#E7F5EC"), "failed": ("#B42318", "#FEECEA")}


def _deps_after(spec: WorkflowSpec, depth: dict) -> None:
    """join 节点不能被排在它的依赖之前：反复把 join 下沉到 max(dep)+1。"""
    for _ in range(len(spec.nodes) + 1):
        changed = False
        for node in spec.nodes.values():
            if not node.join:
                continue
            want = 1 + max((depth.get(dep, 0) for dep in node.join), default=0)
            if depth.get(node.id, 0) < want:
                depth[node.id] = want
                changed = True
        if not changed:
            return


def layout(spec: WorkflowSpec):
    """BFS 分层 + 层内按 YAML 出现顺序排列；终态标记排在最后层。"""
    depth: dict[str, int] = {}
    if spec.entry in spec.nodes:
        depth[spec.entry] = 0
    queue = deque([spec.entry] if spec.entry in spec.nodes else [])
    while queue:
        cur = queue.popleft()
        for target in spec.nodes[cur].successors():
            if target in spec.nodes and target not in depth:
                depth[target] = depth[cur] + 1
                queue.append(target)

    _deps_after(spec, depth)

    fallback = (max(depth.values()) + 1) if depth else 0
    for node_id in spec.nodes:                     # 从 entry 走不到的节点（校验会拦，这里兜底）
        depth.setdefault(node_id, fallback)

    by_col: dict[int, list[str]] = {}
    for node_id in spec.nodes:
        by_col.setdefault(depth[node_id], []).append(node_id)

    pos: dict[str, tuple[int, int]] = {}
    for col, ids in by_col.items():
        for row, node_id in enumerate(ids):
            pos[node_id] = (PAD_X + col * COL_W, PAD_Y + row * ROW_H)

    # 终态：统一放在最右一列的下方两行
    last_col = (max(by_col) + 1) if by_col else 0
    for i, term in enumerate(sorted(TERMINAL)):
        if any(term in spec.nodes[t].successors() or
               term == spec.nodes[t].on_exhausted for t in spec.nodes):
            pos[f"__{term}"] = (PAD_X + last_col * COL_W + NODE_W // 2,
                                PAD_Y + i * ROW_H + NODE_H // 2)
    return pos


def edges(spec: WorkflowSpec):
    """所有真实出边。带类型，便于分色与画法；终态目标保留 done / failed 原样。"""
    out = []
    for node in spec.nodes.values():
        for target in node.parallel:
            out.append({"src": node.id, "dst": target, "kind": "parallel", "label": "并行"})
        if node.routes:
            for label, target in node.routes.items():
                out.append({"src": node.id, "dst": target, "kind": "route", "label": label})
        if node.on_success:
            out.append({"src": node.id, "dst": node.on_success, "kind": "on_success", "label": ""})
        elif node.next and not node.parallel:
            out.append({"src": node.id, "dst": node.next, "kind": "next", "label": ""})
        if node.on_failure:
            label = ("失败·共 %d 次重试" % node.max_retries) if node.max_retries is not None else ""
            out.append({"src": node.id, "dst": node.on_failure,
                        "kind": "on_failure", "label": label})
        if node.on_exhausted:
            out.append({"src": node.id, "dst": node.on_exhausted,
                        "kind": "on_exhausted", "label": ""})
        if node.on_resume:
            out.append({"src": node.id, "dst": node.on_resume,
                        "kind": "on_resume", "label": ""})
        for dep in node.join:
            out.append({"src": dep, "dst": node.id, "kind": "join", "label": ""})
    return out


def _clip(text, limit=22):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _text_width(text, size):
    """粗估文字宽度：中文字宽 ≈ 1em，ASCII ≈ 0.56em。"""
    wide = sum(1 for ch in text if ord(ch) > 0x2E7F)
    narrow = len(text) - wide
    return int(wide * size + narrow * size * 0.56)


def _node_svg(node, x, y):
    fill, stroke, ink = TYPE_STYLE.get(node.type, TYPE_STYLE["llm"])
    title = node.label or node.id
    sub = TYPE_NAME.get(node.type, node.type)
    if node.type == "llm" and node.agent:
        sub += " · %s" % node.agent
    elif node.type == "tool" and node.tool:
        sub += " · %s" % node.tool
    elif node.type == "join" and node.join:
        sub = "等待 " + ", ".join(node.join)
    tip = "%s（%s）" % (node.id, TYPE_NAME.get(node.type, node.type))
    if node.context and node.context != "{prompt}":
        tip += "\n上下文: " + node.context[:160]
    return (
        '<g><title>%s</title>'
        '<rect x="%d" y="%d" width="%d" height="%d" rx="7" fill="%s" stroke="%s" stroke-width="1"/>'
        '<text x="%d" y="%d" font-size="12.5" font-weight="600" fill="%s">%s</text>'
        '<text x="%d" y="%d" font-size="10.5" fill="#667085">%s</text>'
        "</g>"
        % (html.escape(tip), x, y, NODE_W, NODE_H, fill, stroke,
           x + 12, y + 24, ink, html.escape(_clip(title, 20)),
           x + 12, y + 43, html.escape(_clip(sub, 24)))
    )


def _edge_svg(edge, pos):
    src, dst, kind = edge["src"], edge["dst"], edge["kind"]
    if src not in pos:
        return ""
    sx, sy = pos[src]
    if dst in pos:
        tx, ty = pos[dst]
    elif dst in TERMINAL:
        key = f"__{dst}"
        if key not in pos:
            return ""
        tx, ty = pos[key]
        tx, ty = tx - TERM_R, ty - TERM_R
    else:
        return ""

    color, dash, default_label = EDGE_STYLE.get(kind, EDGE_STYLE["next"])
    label = edge.get("label") or default_label

    x1, y1 = sx + NODE_W, sy + NODE_H / 2
    x2, y2 = (tx, ty + NODE_H / 2) if dst in pos else (tx, ty + TERM_R)
    back = x2 <= x1 + 24
    if back:                                    # 回边（循环 / 人工恢复）：从下方绕回去
        dip = max(y1, y2) + 58
        path = "M %d %d C %d %d, %d %d, %d %d" % (x1, y1, x1 + 46, dip, x2 - 46, dip, x2, y2)
        lx, ly = (x1 + x2) / 2, dip - 8
    else:
        path = "M %d %d C %d %d, %d %d, %d %d" % (x1, y1, x1 + 62, y1, x2 - 62, y2, x2, y2)
        lx, ly = (x1 + x2) / 2, (y1 + y2) / 2 - 8

    dash_attr = ' stroke-dasharray="%s"' % dash if dash else ""
    out = ('<path d="%s" fill="none" stroke="%s" stroke-width="1.3"%s marker-end="url(#ag-arrow-%s)"/>'
           % (path, color, dash_attr, kind))
    if label:
        size = 10
        w = _text_width(label, size) + 10
        out += ('<rect x="%d" y="%d" width="%d" height="16" rx="3" fill="#FFFFFF" fill-opacity="0.92"/>'
                '<text x="%d" y="%d" font-size="%d" fill="%s" text-anchor="middle">%s</text>'
                % (lx - w / 2, ly - 12, w, lx, ly, size, color, html.escape(_clip(label, 18))))
    return out


def _terminal_svg(term, x, y, count):
    stroke, fill = TERMINAL_STYLE.get(term, ("#667085", "#F2F4F7"))
    return ('<g><title>%s（%d 条转移到此）</title>'
            '<circle cx="%d" cy="%d" r="%d" fill="%s" stroke="%s" stroke-width="2"/>'
            '<text x="%d" y="%d" font-size="10.5" fill="%s" text-anchor="middle">%s</text></g>'
            % (term, count, x, y, TERM_R, fill, stroke, x, y + 4, stroke, term))


def legend_svg():
    items = [("#475467", "", "成功/顺序"), ("#B54708", "4 3", "条件分支"),
             ("#B42318", "5 3", "失败回边"), ("#5925DC", "6 3", "人工恢复"),
             ("#98A2B3", "1 4", "汇合依赖")]
    out, x = [], 2
    for color, dash, text in items:
        dash_attr = ' stroke-dasharray="%s"' % dash if dash else ""
        out.append('<line x1="%d" y1="9" x2="%d" y2="9" stroke="%s" stroke-width="1.6"%s/>'
                   % (x, x + 22, color, dash_attr))
        out.append('<text x="%d" y="13" font-size="10.5" fill="#667085">%s</text>' % (x + 28, html.escape(text)))
        x += 28 + _text_width(text, 10.5) + 18
    return "".join(out) if out else ""


def render(spec: WorkflowSpec, states: dict | None = None, show_legend: bool = True) -> str:
    """自包含 SVG：不引用任何外部资源，离线可渲染。"""
    pos = layout(spec)
    edge_list = edges(spec)
    term_counts = {}
    for e in edge_list:
        if e["dst"] in TERMINAL:
            term_counts[e["dst"]] = term_counts.get(e["dst"], 0) + 1

    max_x = max((x for x, _ in pos.values()), default=PAD_X)
    max_y = max((y for _, y in pos.values()), default=PAD_Y)
    width = max_x + NODE_W + PAD_X
    height = max_y + NODE_H + PAD_Y + (24 if show_legend else 4)

    markers = "".join(
        '<marker id="ag-arrow-%s" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="%s"/></marker>' % (kind, style[0])
        for kind, style in EDGE_STYLE.items()
    )

    edge_svg = "".join(_edge_svg(e, pos) for e in edge_list)
    node_svg = "".join(_node_svg(n, *pos[n.id]) for n in spec.nodes.values() if n.id in pos)
    term_svg = "".join(
        _terminal_svg(term, *pos[f"__{term}"], term_counts.get(term, 0))
        for term in TERMINAL if f"__{term}" in pos
    )
    legend = ('<g transform="translate(%d,%d)">%s</g>'
              % (PAD_X, height - 22, legend_svg())) if show_legend else ""

    return (
        '<div class="ag-canvas" style="overflow-x:auto;border:1px solid #E4E7EC;'
        'border-radius:8px;background:#FFFFFF;padding:2px">'
        '<svg width="100%%" height="%d" viewBox="0 0 %d %d" style="display:block;max-width:100%%" '
        'xmlns="http://www.w3.org/2000/svg" font-family="ui-sans-serif,system-ui,Segoe UI,sans-serif">'
        "<defs>%s</defs>%s%s%s%s</svg></div>"
        % (height, width, height, markers, edge_svg, node_svg, term_svg, legend)
    )