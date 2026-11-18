"""工作流规格（数据层）：workflows/*.yaml -> WorkflowSpec -> 引擎解释执行。

设计目标：把编排从「engine.py 里的 if/elif」搬到「仓库里的 YAML」。
新增/修改工作流只改 YAML，不改 Python。

支持的控制结构（对应赛题「工作流编排」要求）：
    顺序执行   next / on_success
    条件判断   routes: {"[CODE]": coder}      —— 按模型输出里的标签匹配
    循环迭代   on_failure 构成回边 + max_retries 预算 + on_exhausted 兜底
    并行分支   parallel: [a, b]  ->  汇合节点 join: [a, b]
    子流程嵌套 sub_workflow: <另一个工作流名>（引擎按子流程 entry 展开，预留）
    人工节点   type: human（置 waiting_human，人工下发后按 on_resume 恢复）

节点类型（type）：
    llm    调用已注册的 Agent（agents/*.yaml），产出文本
    tool   调用工具注册表（core/tools.py: TOOL_REGISTRY）
    join   等待 parallel 分支全部完成后再放行
    human  人工介入，等待外部输入
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = BASE_DIR / "workflows"

# 保留终态：不是一个真实节点，只是转移目标里的终止标记
TERMINAL = frozenset({"done", "failed"})
NODE_TYPES = frozenset({"llm", "tool", "human", "join"})

# 上下文模板占位符：{prompt} / {spec} / {last_error} / {files} / {advice} / {retry} ...
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


class WorkflowError(ValueError):
    """工作流定义不合法（校验失败时抛出，带全部错误列表）"""

    def __init__(self, name: str, errors: list[str]):
        self.name = name
        self.errors = errors
        super().__init__(f"工作流 [{name}] 定义校验失败：\n" + "\n".join(f"  - {e}" for e in errors))


@dataclass
class Node:
    """一个编排节点。字段全部来自 YAML，引擎不硬编码任何业务语义。"""

    id: str
    type: str = "llm"
    label: str = ""

    # llm 节点：用哪个 Agent（引用 agents/*.yaml 的 name）
    agent: str | None = None
    # tool 节点：用哪个工具（引用 core/tools.py: TOOL_REGISTRY 的键）
    tool: str | None = None

    # 上下文模板，占位符由引擎在运行时填充
    context: str = "{prompt}"
    # 成功后把输出写入 trace 变量（如 coder 把规格写进 {spec} 供 verify 使用）
    publish: str | None = None

    # 控制结构
    routes: dict = field(default_factory=dict)  # 条件分支：标签 -> 目标节点
    next: str | None = None                     # 默认后继 / 条件兜底
    on_success: str | None = None               # 优先于 next
    on_failure: str | None = None               # 失败时的目标（回边 = 循环）
    max_retries: int | None = None              # 失败重试预算
    on_exhausted: str | None = None             # 预算耗尽后的兜底目标
    on_resume: str | None = None                # human 节点：人工介入后回到哪
    parallel: list = field(default_factory=list)  # 成功后并发扇出
    join: list = field(default_factory=list)      # join 节点：等这些节点完成
    wait_timeout: int = 60                        # join 最长等待秒数

    sub_workflow: str | None = None

    @property
    def title(self) -> str:
        return self.label or self.id

    def successors(self) -> list[str]:
        """所有出边目标（用于图渲染与可达性校验）"""
        out = [t for t in (self.next, self.on_success, self.on_failure,
                           self.on_exhausted, self.on_resume) if t]
        out.extend(v for v in self.routes.values() if v)
        out.extend(self.parallel)
        return out


@dataclass
class WorkflowSpec:
    name: str
    entry: str
    nodes: dict = field(default_factory=dict)  # id -> Node
    description: str = ""
    version: int = 1
    path: Path | None = None

    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise WorkflowError(self.name, [f"引用了不存在的节点: {node_id}"]) from None

    def has(self, node_id: str) -> bool:
        return node_id in self.nodes

    def entry_node(self) -> Node:
        return self.node(self.entry)


# ---------------------------------------------------------------- 加载与校验

_CACHE: dict[Path, tuple[float, WorkflowSpec]] = {}


def _resolve(ref: str | Path) -> Path:
    p = Path(ref)
    if p.suffix.lower() in (".yaml", ".yml"):
        return p if p.is_absolute() else (WORKFLOWS_DIR / p.name)
    return WORKFLOWS_DIR / f"{p}.yaml"


def load_workflow(ref: str | Path, use_cache: bool = True) -> WorkflowSpec:
    """按名字（code_gen）或路径加载并校验工作流定义。"""
    path = _resolve(ref)
    if not path.exists():
        raise WorkflowError(str(ref), [f"工作流文件不存在: {path}"])

    mtime = path.stat().st_mtime
    if use_cache and path in _CACHE and _CACHE[path][0] == mtime:
        return _CACHE[path][1]

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise WorkflowError(str(ref), [f"YAML 语法错误: {e}"]) from None

    spec = _build(data or {}, path)
    _validate(spec)
    _CACHE[path] = (mtime, spec)
    return spec


def list_workflows() -> dict[str, WorkflowSpec]:
    """扫描 workflows/ 下所有定义；单个文件非法不影响其它文件可用。"""
    if not WORKFLOWS_DIR.exists():
        return {}
    specs: dict[str, WorkflowSpec] = {}
    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        try:
            spec = load_workflow(path)
        except WorkflowError:
            continue
        specs[spec.name] = spec
    return specs


def _build(data: dict, path: Path) -> WorkflowSpec:
    errors: list[str] = []
    name = str(data.get("name") or path.stem)
    entry = data.get("entry")
    if not isinstance(entry, str) or not entry:
        errors.append("缺少 entry（入口节点 id）")

    nodes: dict[str, Node] = {}
    raw_nodes = data.get("nodes") or []
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append("nodes 必须是非空列表")

    for i, item in enumerate(raw_nodes):
        if not isinstance(item, dict) or not item.get("id"):
            errors.append(f"nodes[{i}] 缺少 id")
            continue
        nid = str(item["id"]).strip()
        if nid in nodes:
            errors.append(f"节点 id 重复: {nid}")
            continue
        ntype = str(item.get("type") or "llm").strip().lower()
        if ntype not in NODE_TYPES:
            errors.append(f"节点 [{nid}] type 非法: {ntype}（可选 {sorted(NODE_TYPES)}）")
        routes = item.get("routes") or {}
        if not isinstance(routes, dict):
            errors.append(f"节点 [{nid}] routes 必须是映射（标签 -> 目标）")
            routes = {}
        nodes[nid] = Node(
            id=nid,
            type=ntype,
            label=str(item.get("label") or ""),
            agent=(str(item["agent"]) if item.get("agent") else None),
            tool=(str(item["tool"]) if item.get("tool") else None),
            context=str(item.get("context") or "{prompt}"),
            publish=(str(item["publish"]) if item.get("publish") else None),
            routes={str(k): str(v) for k, v in routes.items()},
            next=_opt(item.get("next")),
            on_success=_opt(item.get("on_success")),
            on_failure=_opt(item.get("on_failure")),
            max_retries=(int(item["max_retries"]) if item.get("max_retries") is not None else None),
            on_exhausted=_opt(item.get("on_exhausted")),
            on_resume=_opt(item.get("on_resume")),
            parallel=[str(x) for x in (item.get("parallel") or [])],
            join=[str(x) for x in (item.get("join") or [])],
            wait_timeout=int(item.get("wait_timeout") or 60),
            sub_workflow=_opt(item.get("sub_workflow")),
        )

    if errors:
        raise WorkflowError(name, errors)

    return WorkflowSpec(
        name=name, entry=str(entry), nodes=nodes,
        description=str(data.get("description") or ""),
        version=int(data.get("version") or 1),
        path=path,
    )


def _opt(v):
    return str(v) if v else None


def _validate(spec: WorkflowSpec) -> None:
    errors: list[str] = []

    if spec.entry not in spec.nodes:
        errors.append(f"entry 指向不存在的节点: {spec.entry}")

    if spec.entry in TERMINAL:
        errors.append("entry 不能是终态标记 done/failed")

    for node in spec.nodes.values():
        if node.type == "llm" and not node.agent:
            errors.append(f"llm 节点 [{node.id}] 必须声明 agent")
        if node.type == "tool" and not node.tool:
            errors.append(f"tool 节点 [{node.id}] 必须声明 tool")
        if node.type == "join" and not node.join:
            errors.append(f"join 节点 [{node.id}] 必须声明 join: [前置节点...]")
        if node.type != "join" and node.join:
            errors.append(f"节点 [{node.id}] 只有 type: join 才能声明 join")
        if node.max_retries is not None and node.max_retries < 0:
            errors.append(f"节点 [{node.id}] max_retries 不能为负")
        if node.max_retries is not None and not node.on_failure:
            errors.append(f"节点 [{node.id}] 声明了 max_retries 但没有 on_failure，预算无处生效")
        if node.on_exhausted and node.max_retries is None:
            errors.append(f"节点 [{node.id}] 声明了 on_exhausted 但没有 max_retries")
        if node.parallel and node.type != "llm" and node.type != "tool":
            errors.append(f"节点 [{node.id}] parallel 只能挂在 llm/tool 节点上")

        # 所有出边目标必须存在（或为终态标记）
        for target in node.successors():
            if target not in spec.nodes and target not in TERMINAL:
                errors.append(f"节点 [{node.id}] 指向不存在的节点: {target}")
        # join 依赖必须存在
        for dep in node.join:
            if dep not in spec.nodes:
                errors.append(f"join 节点 [{node.id}] 依赖不存在的节点: {dep}")
        if node.sub_workflow:
            sub_path = WORKFLOWS_DIR / f"{node.sub_workflow}.yaml"
            if not sub_path.exists():
                errors.append(f"节点 [{node.id}] 引用了不存在的子流程: {node.sub_workflow}")

    # 可达性：从 entry 出发走不到 = 写错了（拼错节点名最容易被这个抓出来）
    if spec.entry in spec.nodes:
        reachable, stack = set(), [spec.entry]
        while stack:
            cur = stack.pop()
            if cur in reachable or cur in TERMINAL or cur not in spec.nodes:
                continue
            reachable.add(cur)
            stack.extend(spec.nodes[cur].successors())
            for dep in spec.nodes[cur].join:
                stack.append(dep)
        orphans = sorted(set(spec.nodes) - reachable)
        if orphans:
            errors.append(f"存在从 entry 不可达的节点: {', '.join(orphans)}")

    if errors:
        raise WorkflowError(spec.name, errors)


# ---------------------------------------------------------------- 转移规则

def match_route(node: Node, output: str) -> str | None:
    """条件分支：按输出文本里出现的标签匹配 routes，未命中则走 next 兜底。"""
    for label, target in node.routes.items():
        if label and label in (output or ""):
            return target
    return node.next


def plan_transitions(node: Node, ok: bool, retry: int):
    """根据节点定义与本次结果，算出下一步。

    返回 (targets, next_retry)
      targets == []       -> 工作流在本节点终结（成功写 done / 失败写 failed）
      targets 含终态标记   -> 显式终止
      其它                -> 需要为每个目标创建任务

    失败语义：
      retry 是「当前失败循环里已发生的次数」，失败时 +1；
      超过 max_retries 则跳到 on_exhausted（兜底，通常是 human）。
      成功时原样带到下游——循环计数器靠它跨过「修复-验证」对，不会被重置。
    """
    if ok:
        if node.parallel:
            return list(node.parallel), retry
        target = node.on_success or node.next
        return ([target] if target else []), retry

    next_retry = retry + 1
    if node.max_retries is not None and next_retry > node.max_retries:
        if node.on_exhausted:
            return [node.on_exhausted], 0
        return [], next_retry
    if node.on_failure:
        return [node.on_failure], next_retry
    return [], next_retry


def render_context(template: str, variables: dict) -> str:
    """填充上下文模板。缺失的变量原样保留占位符，便于排查。"""
    def _sub(m):
        key = m.group(1)
        if key in variables and variables[key] is not None:
            return str(variables[key])
        if key == "retry":
            return str(variables.get("__retry", 0))
        return m.group(0)

    return PLACEHOLDER.sub(_sub, template or "{prompt}").strip()


# ---------------------------------------------------------------- 图渲染（供前端用）

_STATE_CLASS = {"done": "done", "running": "running", "failed": "failed",
                "waiting_human": "waiting", "pending": "pending"}


def to_dot(spec: WorkflowSpec, states: dict | None = None) -> str:
    """把工作流定义画成 Graphviz DOT。

    为什么不用 mermaid：mermaid 需要从 CDN 拉 JS，比赛现场/内网一断就渲染不出来；
    Streamlit 的 st.graphviz_chart 把 viz.js 打包在前端资源里，离线可用。
    配色与前端设计系统一致（done 绿 / running 蓝 / failed 红 / waiting 琥珀 / pending 灰）。
    """
    states = states or {}
    FILL = {"done": "#E7F5EC", "joined": "#E7F5EC", "running": "#E8EFFC",
            "failed": "#FBE9E7", "waiting_human": "#FDF1E0", "pending": "#F7F9FD"}
    BORD = {"done": "#1E8E3E", "joined": "#1E8E3E", "running": "#0B49C3",
            "failed": "#C0392B", "waiting_human": "#D97706", "pending": "#C9D6EA"}
    lines = [
        "digraph workflow {",
        '  graph [rankdir=LR, bgcolor="transparent", pad="0.12", nodesep="0.40", '
        'ranksep="0.55", fontname="Microsoft YaHei"];',
        '  node [shape=box, style="rounded,filled", fontname="Microsoft YaHei", '
        'fontsize=11, penwidth=1.3, color="#C9D6EA", fillcolor="#F7F9FD", fontcolor="#0F1B2D"];',
        '  edge [fontname="Microsoft YaHei", fontsize=9, color="#8A94A6", arrowsize=0.7];',
    ]
    for node in spec.nodes.values():
        state = states.get(node.id, "pending")
        style = "rounded,filled" + (",dashed" if state == "pending" else "")
        label = f"{node.title}\\n{node.id} · {node.type}"
        lines.append(
            f'  "{node.id}" [label="{label}", fillcolor="{FILL.get(state, FILL["pending"])}", '
            f'color="{BORD.get(state, BORD["pending"])}", style="{style}"];'
        )

    terminals = sorted({t for n in spec.nodes.values() for t in n.successors() if t in TERMINAL})
    for i, t in enumerate(terminals):
        fill = "#E7F5EC" if t == "done" else "#FBE9E7"
        bord = "#1E8E3E" if t == "done" else "#C0392B"
        lines.append(f'  "__{t}" [shape=circle, label="{t}", fontsize=9, width=0.55, '
                     f'fixedsize=true, fillcolor="{fill}", color="{bord}"];')

    def _tgt(name):
        return f"__{name}" if name in TERMINAL else f'"{name}"'

    seen = set()

    def edge(src, dst, label="", style="solid", color="#8A94A6"):
        key = (src, dst, label, style, color)
        if key in seen:
            return
        seen.add(key)
        attrs = f'style={style}, color="{color}"'
        if label:
            attrs += f', label=" {label} "'
        lines.append(f'  "{src}" -> {dst} [{attrs}];')

    for node in spec.nodes.values():
        for label, target in node.routes.items():
            if target in spec.nodes or target in TERMINAL:
                edge(node.id, _tgt(target), label, "solid", "#2F6BEA")
        for target in (node.next, node.on_success):
            if target in spec.nodes or target in TERMINAL:
                edge(node.id, _tgt(target), "", "solid", "#8A94A6")
        if node.on_failure and (node.on_failure in spec.nodes):
            edge(node.id, _tgt(node.on_failure), "失败", "dashed", "#C0392B")
        if node.on_exhausted and (node.on_exhausted in spec.nodes):
            edge(node.id, _tgt(node.on_exhausted), "预算耗尽", "dashed", "#D97706")
        if node.on_resume and (node.on_resume in spec.nodes):
            edge(node.id, _tgt(node.on_resume), "人工恢复", "dashed", "#7C4DDB")
        for target in node.parallel:
            if target in spec.nodes:
                edge(node.id, _tgt(target), "并行", "dashed", "#0B49C3")
        for dep in node.join:
            if dep in spec.nodes:
                edge(dep, _tgt(node.id), "汇合", "dotted", "#6B7788")

    lines.append("}")
    return "\n".join(lines)


def to_mermaid(spec: WorkflowSpec, states: dict | None = None) -> str:
    """把工作流定义画成 Mermaid 图——图直接来自 YAML，改了 YAML 图就变。"""
    states = states or {}
    lines = ["graph LR"]

    # 1) 真实节点
    for node in spec.nodes.values():
        cls = _STATE_CLASS.get(states.get(node.id, "pending"), "pending")
        shape_open, shape_close = ("{{", "}}") if node.type in ("join", "human") else ("[", "]")
        label = f"{node.title}<br/><small>{node.type}</small>"
        lines.append(f'    {node.id}{shape_open}"{label}"{shape_close}:::{cls}')

    # 2) 终态节点（done / failed）只声明一次
    terminals = sorted({t for n in spec.nodes.values() for t in n.successors() if t in TERMINAL})
    for t in terminals:
        lines.append(f'    __{t}(("{t}"))')

    # 3) 边（去重，避免同一条边画两遍）
    seen = set()
    # join 依赖单独用粗箭头画，普通 next 边就不重复画了
    join_pairs = {(dep, node.id) for node in spec.nodes.values()
                  if node.type == "join" for dep in node.join}

    def edge(src, dst, label=None, style="-"):
        key = (src, dst, label, style)
        if key in seen:
            return
        seen.add(key)
        if label:
            lines.append(f'    {src} {style}->|"{label}"| {dst}')
        else:
            lines.append(f"    {src} {style}-> {dst}")

    for node in spec.nodes.values():
        if node.parallel:
            for target in node.parallel:
                if target in spec.nodes:
                    edge(node.id, target, "并行", "-.")
        for label, target in node.routes.items():
            if target in spec.nodes:
                edge(node.id, target, label)
            elif target in TERMINAL:
                edge(node.id, f"__{target}", label)
        for target in (node.next, node.on_success):
            if (node.id, target) in join_pairs:
                continue
            if target in spec.nodes:
                edge(node.id, target)
            elif target in TERMINAL:
                edge(node.id, f"__{target}")
        if node.on_failure and node.on_failure in spec.nodes:
            edge(node.id, node.on_failure, "失败", "-.")
        if node.on_exhausted and node.on_exhausted in spec.nodes:
            edge(node.id, node.on_exhausted, "预算耗尽", "-.")
        if node.on_resume and node.on_resume in spec.nodes:
            edge(node.id, node.on_resume, "人工恢复", "-.")
        for dep in node.join:
            if dep in spec.nodes:
                edge(dep, node.id, None, "=")

    lines += [
        "    classDef done fill:#4CAF50,color:#fff",
        "    classDef running fill:#FFC107,color:#000",
        "    classDef failed fill:#F44336,color:#fff",
        "    classDef waiting fill:#9C27B0,color:#fff",
        "    classDef pending fill:#E0E0E0,color:#000",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 序列化与保存（可视化编辑器用）
# 编辑器的"所见即所得"必须落回同一份 YAML：界面改完 -> spec_to_dict -> 校验 -> 原子写盘，
# 引擎下一次认领就能读到新定义（带 mtime 缓存，无需重启）。

_SIMPLE_EDGES = ("next", "on_success", "on_failure", "on_exhausted", "on_resume")


def spec_to_dict(spec: WorkflowSpec) -> dict:
    """WorkflowSpec -> YAML 同构 dict。只写非默认值，生成的 YAML 保持可读。"""
    nodes = []
    for node in spec.nodes.values():
        item: dict = {"id": node.id, "type": node.type}
        if node.label:
            item["label"] = node.label
        if node.agent:
            item["agent"] = node.agent
        if node.tool:
            item["tool"] = node.tool
        if node.context and node.context != "{prompt}":
            item["context"] = node.context
        if node.publish:
            item["publish"] = node.publish
        if node.routes:
            item["routes"] = dict(node.routes)
        for key in _SIMPLE_EDGES:
            value = getattr(node, key)
            if value:
                item[key] = value
        if node.max_retries is not None:
            item["max_retries"] = node.max_retries
        if node.parallel:
            item["parallel"] = list(node.parallel)
        if node.join:
            item["join"] = list(node.join)
        if node.type == "join" and node.wait_timeout != 60:
            item["wait_timeout"] = node.wait_timeout
        if node.sub_workflow:
            item["sub_workflow"] = node.sub_workflow
        nodes.append(item)

    data: dict = {"name": spec.name}
    if spec.description:
        data["description"] = spec.description
    data["version"] = spec.version
    data["entry"] = spec.entry
    data["nodes"] = nodes
    return data


def spec_to_yaml(spec: WorkflowSpec) -> str:
    """序列化成 YAML 文本（中文不转义，字段顺序固定）。"""
    return yaml.safe_dump(spec_to_dict(spec), allow_unicode=True,
                          sort_keys=False, width=100, default_flow_style=False)


def validate_data(data: dict, name: str | None = None) -> WorkflowSpec:
    """不落盘地校验一份定义（编辑器「校验」按钮用）；不合法直接抛 WorkflowError。"""
    label = name or str((data or {}).get("name") or "draft")
    spec = _build(data or {}, WORKFLOWS_DIR / f"{label}.yaml")
    _validate(spec)
    return spec


def _leading_comment_block(path: Path) -> list:
    """读出文件顶部的注释块（含空行），保存时原样保留。

    画布编辑走的是"spec -> dict -> YAML"，逐节点注释无法无损往返，
    但文件头部的说明（为什么这么编排、字段约定）是文档资产，必须保住。
    """
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()   # utf-8-sig 吃掉 BOM
    except OSError:
        return []
    header = []
    for line in lines:
        if line.strip().startswith("#") or not line.strip():
            header.append(line)
        else:
            break
    while header and not header[-1].strip():
        header.pop()
    return header


def save_workflow_data(name: str, data: dict) -> Path:
    """校验通过才写盘，且用临时文件 + replace 保证不会留下半截 YAML。

    顶部注释块会被原样保留（逐节点注释不保留，界面上有提示）。
    """
    spec = validate_data(data, name)
    path = WORKFLOWS_DIR / f"{name}.yaml"
    header = _leading_comment_block(path)
    body = spec_to_yaml(spec)
    text = ("\n".join(header) + "\n" + body) if header else body
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    _CACHE.pop(path, None)
    return path


def blank_node(node_id: str, ntype: str = "llm") -> dict:
    """新建节点的初始形状（按类型给出必填字段，避免一建出来就校验失败）。"""
    node: dict = {"id": node_id, "type": ntype}
    if ntype == "llm":
        node["agent"] = "coder"
    elif ntype == "tool":
        node["tool"] = "execute_spec"
    elif ntype == "join":
        node["join"] = []
    return node