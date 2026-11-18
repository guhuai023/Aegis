"""编排引擎：解释执行 workflows/*.yaml，不再硬编码任何一支流程。

旧版 core/engine.py 的 _step() 是一串 `if node_id == "router" / elif node_id == "coder" / ...`，
加一个节点、换一条修复路径都要改 Python 代码——那正好是赛题点名要解决的"硬编码流水线"。

现在引擎只做四件事，节点语义全部来自 YAML：
    1. 从黑板（tasks 表）认领任务
    2. 按 node.type 执行节点：llm / tool / join / human
    3. 按节点定义算下一步：routes / on_success / on_failure / max_retries / parallel
    4. 落库 + 写 trace 级工作记忆（trace_vars）

顺带修掉两个可靠性硬伤：
    · 节点执行异常不再静默把整条流程判死，而是交给节点的 on_failure（大模型抖动会进修复分支）
    · 每条 trace 有步数上限，YAML 写出无边界环也不会把 Worker 卡死
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid

from core import db
from core.agents import AgentError, ask_agent_spec, get_agent, reload_agents
from core.config import (
    DEFAULT_WORKFLOW, JOIN_POLL_SECONDS, MAX_FILE_CHARS_IN_CONTEXT,
    MAX_FILES_IN_CONTEXT, MAX_LLM_CONCURRENCY, MAX_STEPS_PER_TRACE,
    WORKER_CAPABILITIES, WORKER_COUNT, WORKSPACES_DIR,
)
from core.tools import (
    TOOL_REGISTRY, ToolContext, get_workspace_dir, list_workspace_files,
    reset_run_as_node, run_as_node,
)
from core.workflow import (
    TERMINAL, WorkflowError, WorkflowSpec, load_workflow,
    match_route, plan_transitions, render_context,
)

_ENGINE_STARTED = False

# 同时在跑的 LLM 节点上限：多 Worker 时把并发压在这里，避免把 provider 打爆（429）
_LLM_SEM = threading.Semaphore(max(1, int(MAX_LLM_CONCURRENCY)))

# 同一个 trace 的落盘/执行阶段必须串行：
# 工作区是按 trace 共享的，而 execute_spec 固定入口是 main.py，
# 并行支线同时跑沙箱会互相覆盖入口文件。LLM 调用仍然并行，只有"写+跑+验"这段加锁。
_TOOL_LOCKS: dict[str, threading.Lock] = {}
_TOOL_LOCKS_GUARD = threading.Lock()


def _trace_tool_lock(trace_id: str) -> threading.Lock:
    with _TOOL_LOCKS_GUARD:
        if len(_TOOL_LOCKS) > 256:            # 简单上限，避免长跑进程无限增长
            for key in list(_TOOL_LOCKS)[:128]:
                if key != trace_id and not _TOOL_LOCKS[key].locked():
                    _TOOL_LOCKS.pop(key, None)
        return _TOOL_LOCKS.setdefault(trace_id, threading.Lock())

# Worker 运行时画像（认领数 / 当前任务 / 能力集），前端「调度器」卡展示用
WORKER_STATE: dict[str, dict] = {}


# ---------------------------------------------------------------- 工作流装载

def get_workflow(name: str | None = None) -> WorkflowSpec:
    """按名字取工作流定义（带 mtime 缓存，改 YAML 无需重启即可生效）。"""
    return load_workflow(name or DEFAULT_WORKFLOW)


# ---------------------------------------------------------------- 对外入口

def start_workflow(workflow_name: str, prompt: str, trace_id: str | None = None) -> str:
    """启动一条工作流，返回追踪 ID。

    开场先校验 YAML，定义写坏时立刻报错，不会把半截流程写进黑板。
    """
    spec = load_workflow(workflow_name)
    trace_id = trace_id or uuid.uuid4().hex[:8]
    db.set_var(trace_id, "prompt", prompt)
    db.set_var(trace_id, "workflow", spec.name)
    _new_task(spec, trace_id, spec.entry, prompt, 0)
    db.log_event(trace_id, "INFO",
                 f"🚀 工作流 [{spec.name}] v{spec.version} 启动，入口节点 [{spec.entry}]")
    return trace_id


def resume_with_advice(trace_id: str, task_id: str, advice: str) -> str:
    """人工建议下发：关掉 waiting_human 任务，按 human 节点的 on_resume 恢复，并重置修复预算。"""
    waiting = db.get_waiting_human(trace_id)
    wf_name = db.get_var(trace_id, "workflow", DEFAULT_WORKFLOW)
    if not waiting:
        raise WorkflowError(wf_name, ["没有等待人工介入的任务（可能已经被恢复过）"])
    node_id = waiting[1]
    spec = load_workflow(wf_name)
    if not spec.has(node_id):
        raise WorkflowError(spec.name, [f"人工介入节点 [{node_id}] 不在工作流定义里"])
    target = spec.node(node_id).on_resume
    if not target:
        raise WorkflowError(spec.name, [f"人工节点 [{node_id}] 没有声明 on_resume"])

    db.finish_task(task_id, "done", "人工建议已采纳")
    db.set_var(trace_id, "advice", advice)
    # retry 归零 = 修复预算重置；人工已介入，给最高优先级优先跑
    _new_task(spec, trace_id, target, advice, 0, priority_boost=100)
    db.log_event(trace_id, "HUMAN",
                 f"🩹 收到人工建议，按 on_resume 恢复 -> [{target}]（修预算已重置）")
    return target


def start_engine(workers: int | None = None, capabilities=None) -> None:
    """启动 Worker。

    workers       线程数（默认 WORKER_COUNT）；认领是数据库层原子操作，多 Worker 天然安全
    capabilities  能力分区：None = 每个 Worker 全能力；给列表则每项是一个 Worker 的能力集合
                  （例：一个专职跑 tool/join、一个专职跑 llm，用于演示能力匹配调度）
    """
    global _ENGINE_STARTED
    if _ENGINE_STARTED:
        return
    _ENGINE_STARTED = True
    count = max(1, int(workers or WORKER_COUNT))
    plan = capabilities if capabilities is not None else WORKER_CAPABILITIES
    for i in range(count):
        caps = None
        if plan:
            caps = set(plan[i]) if i < len(plan) else set()
        name = f"worker-{i}"
        WORKER_STATE[name] = {
            "caps": sorted(caps) if caps else ["*"],
            "claimed": 0,
            "last_task": None,
        }
        threading.Thread(target=_worker_loop, args=(name, caps),
                         name=f"aegis-{name}", daemon=True).start()
    detail = "，".join(f"{k}({len(v['caps']) if v['caps'] != ['*'] else '全能力'})"
                       for k, v in WORKER_STATE.items())
    print(f"🔥 [引擎] 已启动 {count} 个 Worker 线程：{detail}，正在监听任务黑板")


# ---------------------------------------------------------------- Worker

def _worker_loop(worker_name="worker-0", capabilities=None):
    try:
        recovered = db.recover_stale_tasks()
        if recovered:
            print(f"♻️ [引擎] 启动时回收了 {recovered} 个卡死任务")
    except Exception as e:
        print(f"⚠️ [引擎] 启动回收失败: {e}")

    while True:
        try:
            _step(capabilities, worker_name)
        except Exception:
            print(f"🚨 [引擎] 调度异常\n{traceback.format_exc()}")
            try:
                db.recover_stale_tasks()
            except Exception:
                pass
        time.sleep(1)


def _step(capabilities=None, worker_name="worker-0"):
    """单步调度：认领 -> 执行 -> 算下一步 -> 落库。全程无业务分支硬编码。

    capabilities 限定这个 Worker 能认领哪些任务（能力匹配），None = 不限。
    """
    task = db.claim_task(capabilities)
    if not task:
        return
    task_id, trace_id, node_id, context, retry, workflow_name = task
    retry = int(retry or 0)

    state = WORKER_STATE.setdefault(worker_name, {"caps": ["*"], "claimed": 0, "last_task": None})
    state["claimed"] = int(state.get("claimed", 0)) + 1
    state["last_task"] = f"{node_id}@{trace_id}"

    # 兜底：防止 YAML 里的环没有预算限制
    if db.count_tasks(trace_id) > MAX_STEPS_PER_TRACE:
        db.finish_task(task_id, "failed", "超过单 trace 最大步数")
        db.log_event(trace_id, "ERROR", f"🛑 超过最大步数 {MAX_STEPS_PER_TRACE}，强制终止")
        return

    try:
        spec = get_workflow(workflow_name)
    except WorkflowError as e:
        db.finish_task(task_id, "failed", str(e))
        db.log_event(trace_id, "ERROR", f"❌ 工作流定义加载失败: {e}")
        return

    node = spec.nodes.get(node_id)
    if node is None:
        db.finish_task(task_id, "failed", f"工作流 [{spec.name}] 未定义节点: {node_id}")
        db.log_event(trace_id, "ERROR", f"❓ 未定义节点: {node_id}")
        return

    print(f"🎯 [引擎] 认领任务: {node.id} (task={task_id}, retry={retry}, wf={spec.name})")
    db.log_event(trace_id, "RUNNING", f"▶️ 开始执行: {node.title}（{node.id}/{node.type}）")

    # ---- 执行 ----
    if node.type == "join":
        state = _join_state(spec, node, trace_id)
        if state == "already":
            db.finish_task(task_id, "done", "并行分支已汇合（重复汇合任务忽略）")
            db.log_event(trace_id, "INFO", f"↩️ [{node.id}] 已汇合过，忽略重复任务")
            return
        if state == "wait":
            db.finish_task(task_id, "done", "等待并行分支汇合")
            _new_task(spec, trace_id, node.id, context, retry, delay=JOIN_POLL_SECONDS)
            db.log_event(trace_id, "INFO",
                         f"⏳ [{node.id}] 并行分支尚未全部完成，{JOIN_POLL_SECONDS}s 后再检查")
            return
        ok = state == "done"
        report = "并行分支全部完成" if ok else f"并行分支未全部成功（依赖: {', '.join(node.join)}）"
        output = report
    else:
        try:
            ok, output, report = _execute(node, trace_id, context, retry)
        except Exception as e:
            ok, output, report = False, "", f"{type(e).__name__}: {e}"
            db.log_event(trace_id, "ERROR",
                         f"🚨 [{node.id}] 执行异常，按 on_failure 处理: {_short(report)}")

    # ---- 转移 ----
    _advance(spec, node, trace_id, task_id, ok, output, report, retry)


# ---------------------------------------------------------------- 节点执行

def _execute(node, trace_id, context, retry):
    """按节点类型执行，返回 (ok, output, report)。"""
    # sub_workflow 只做了解析与校验，展开执行尚未实现。
    # 必须在执行前明确拒绝——否则会退化成"按节点类型跑一遍并返回成功"，
    # 让用户以为子流程真的执行了（静默假成功比报错危险得多）。
    if getattr(node, "sub_workflow", None):
        return False, "", (f"sub_workflow 展开执行尚未实现（引用了 [{node.sub_workflow}]）；"
                           "请把子流程节点显式写进当前工作流，或改用独立工作流单独运行")
    variables = _variables_for(trace_id, node, context, retry)
    prompt_ctx = render_context(node.context, variables)

    if node.type == "human":
        return True, "", "等待人工介入"

    if node.type == "llm":
        # Agent 热加载：新增/修改 agents/*.yaml 后不需要重启引擎（与工作流 mtime 热加载对齐）。
        # 只有解析不到时才重扫一次，正常路径零额外开销。
        try:
            agent = get_agent(node.agent)
        except AgentError:
            reload_agents()
            agent = get_agent(node.agent)
        # 只有 YAML 里声明了 tools 的 Agent 才挂工具上下文（省的给每个 trace 建空目录）
        ctx = None
        on_tool = None
        if agent.tools:
            ctx = ToolContext(trace_id=trace_id, workspace=get_workspace_dir(trace_id))

            def on_tool(name, args, observation, _agent=agent, _trace=trace_id):
                db.log_event(_trace, "TOOL",
                             f"🔧 [{_agent.name}] {name}({_args_hint(args)}) "
                             f"-> {_short(observation, 180)}")

        with _LLM_SEM:      # 多 Worker 时限制同时在跑的 LLM 节点数，防 429
            text = ask_agent_spec(
                agent, prompt_ctx, ctx=ctx, on_tool=on_tool,
                on_usage=lambda u: _record_usage(trace_id, agent, u),
            )
        return True, text, text

    if node.type == "tool":
        fn = TOOL_REGISTRY.get(node.tool or "")
        if fn is None:
            return False, "", f"未注册的工具: {node.tool}（可用: {', '.join(TOOL_REGISTRY)}）"
        # 标记"这些产物算哪个节点的"：同一节点重试只清自己的产物，跨节点保留
        # 同 trace 内串行执行：防止并行支线同时覆盖同一个入口文件
        with _trace_tool_lock(trace_id):
            token = run_as_node(node.id)
            try:
                result = fn(trace_id, prompt_ctx)
            finally:
                reset_run_as_node(token)
        return bool(result.ok), (result.report or ""), (result.report or "")

    return False, "", f"未知节点类型: {node.type}"


def _variables_for(trace_id, node, context, retry):
    """组装上下文模板的占位符：trace 级工作记忆 + 本次任务上下文。"""
    variables = db.get_vars(trace_id)
    variables.setdefault("prompt", context or "")
    variables["prev_output"] = context or ""
    variables["retry"] = retry
    if "{files}" in (node.context or ""):
        variables["files"] = _files_context(trace_id)
    return variables


def _files_context(trace_id):
    """把工作区当前代码拼进上下文（fixer 需要看到完整现状）。"""
    ws = WORKSPACES_DIR / trace_id
    chunks = []
    for name in list_workspace_files(trace_id)[:MAX_FILES_IN_CONTEXT]:
        try:
            text = (ws / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(f"=== FILE: {name} ===\n{text[:MAX_FILE_CHARS_IN_CONTEXT]}")
    return "\n\n".join(chunks) if chunks else "（工作区暂无文件）"


def _join_state(spec: WorkflowSpec, node, trace_id):
    """并行汇合判定：done / already / failed / wait"""
    statuses = db.node_statuses(trace_id, node.join)
    if any(statuses.get(dep) == "failed" for dep in node.join):
        return "failed"
    if all(statuses.get(dep) == "done" for dep in node.join):
        # 每个分支完成时都会各自生成一个汇合任务，这里只放行第一个
        if db.get_var(trace_id, f"__joined:{node.id}"):
            return "already"
        db.set_var(trace_id, f"__joined:{node.id}", "1")
        db.set_var(trace_id, f"__join_wait:{node.id}", "0")
        return "done"

    key = f"__join_wait:{node.id}"
    waits = int(db.get_var(trace_id, key, "0") or 0)
    if waits * JOIN_POLL_SECONDS >= node.wait_timeout:
        db.log_event(trace_id, "ERROR", f"⌛ [{node.id}] 等待并行分支超时（>{node.wait_timeout}s）")
        return "failed"
    db.set_var(trace_id, key, str(waits + 1))
    return "wait"


# ---------------------------------------------------------------- 转移

def _advance(spec, node, trace_id, task_id, ok, output, report, retry):
    """按节点定义决定下一步，并落库。所有控制流都来自 YAML 字段。"""

    # 人工节点：停在等待态，等外部输入
    if node.type == "human":
        db.finish_task(task_id, "waiting_human", _store(report))
        db.log_event(trace_id, "HUMAN", "⚠️ 触发熔断，等待人工介入！")
        return

    if ok and node.publish:
        db.set_var(trace_id, node.publish, output or "")
    if not ok:
        db.set_var(trace_id, "last_error", report or "")

    # 1) 条件分支（routes）
    if ok and node.routes:
        target = match_route(node, output)
        db.finish_task(task_id, "done", _store(report))
        if target in (None, "done"):
            db.log_event(trace_id, "SUCCESS", f"✅ [{node.id}] 完成，工作流结束")
            return
        if target == "failed":
            db.log_event(trace_id, "ERROR", f"❌ [{node.id}] 路由到 failed")
            return
        db.log_event(trace_id, "INFO", f"🧠 [{node.id}] 判定 -> {target}")
        _new_task(spec, trace_id, target, output, retry)
        return

    # 2) 顺序 / 循环回边 / 预算兜底 / 并行扇出
    targets, next_retry = plan_transitions(node, ok, retry)
    real = [t for t in targets if t not in TERMINAL]
    terminal = next((t for t in targets if t in TERMINAL), None)

    db.finish_task(task_id, "done" if ok else "failed", _store(report))
    if ok:
        db.log_event(trace_id, "SUCCESS", f"✅ [{node.id}] 通过{_hint(real, terminal)}")
    else:
        db.log_event(trace_id, "ERROR", f"❌ [{node.id}] 未通过{_hint(real, terminal)}: {_short(report)}")

    if not real:
        if not ok:
            db.log_event(trace_id, "ERROR",
                         f"🛑 工作流 [{spec.name}] 终止：节点 [{node.id}] 失败且无兜底")
        return
    for target in real:
        _new_task(spec, trace_id, target, output, next_retry)


def _record_usage(trace_id, agent, usage):
    """把大模型用量累加进 trace 工作记忆，并在日志里留一行。

    累加值落在 trace_vars["llm_usage"]（JSON），前端据此展示"这条 trace 烧了多少 token、
    调了几次工具"——这也是「AI 使用充分性」最直接的取证材料。
    """
    data = {}
    raw = db.get_var(trace_id, "llm_usage", "") or ""
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
    for key in ("calls", "tools", "prompt", "completion", "total"):
        try:
            data[key] = int(data.get(key, 0) or 0) + int(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            data[key] = int(data.get(key, 0) or 0)
    db.set_var(trace_id, "llm_usage", json.dumps(data, ensure_ascii=False))

    tools_note = f"，工具 {usage.get('tools', 0)} 次" if usage.get("tools") else ""
    db.log_event(trace_id, "INFO",
                 f"📊 LLM 用量 [{agent.name}/{agent.model}]: 本轮 {int(usage.get('total', 0))} tokens"
                 f"{tools_note}；累计 {data['total']} tokens / {data['calls']} 次调用")


def _args_hint(args, limit=80):
    """工具入参的单行预览（日志用）。"""
    try:
        text = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(args)
    return text if len(text) <= limit else text[:limit] + "…"


def _store(text, limit=4000):
    """落库前截断：llm 节点的 output 是整份代码规格，全文塞进 SQLite 没有必要。

    完整内容留在 workspace 与 trace 变量里，任务表只留可读摘要。
    """
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"…（已截断，共 {len(text)} 字）"


def _hint(real, terminal):
    if real:
        return "，下一步: " + ", ".join(real)
    if terminal:
        return f"，终态: {terminal}"
    return "（工作流结束）"


def _short(text, n=300):
    """压缩日志可读性：单行 + 截断"""
    text = (text or "").strip().replace("\n", " ⏎ ")
    return text[:n] + ("…" if len(text) > n else "")


# ---------------------------------------------------------------- 调度：任务画像
# 编排层决定"下一步是谁"，调度层决定"什么时候、交给谁"。这里把节点的语义
# 翻译成调度器需要的两样东西：能力标签（谁能跑）与基础优先级（谁先跑）。

def _capability(node) -> str:
    """节点 -> 能力标签，用于 Worker 能力匹配。

    llm:<agent> 只有加载了该 Agent 的 Worker 会认领（Agent 定义决定模型与工具）；
    tool:<tool> / join / human 同理。
    """
    if node.type == "llm":
        return f"llm:{node.agent or 'any'}"
    if node.type == "tool":
        return f"tool:{node.tool or 'any'}"
    return node.type


def _priority(node) -> int:
    """节点 -> 基础优先级：越"卡住整条流程"的节点越先跑。

    human  100  人工已介入的 trace 要立刻推进
    fixer   60  修复决定这条 trace 能不能救回来
    verify  50  验收决定成败
    coder   30  生成
    router  20  分诊
    join    10  轮询型任务，别占着调度位
    """
    if node.type == "join":
        return 10
    if node.type == "human":
        return 100
    if node.type == "tool":
        return 50
    agent = (node.agent or "").lower()
    if agent.startswith("fixer"):
        return 60
    if agent.startswith("coder"):
        return 30
    return 20


def _new_task(spec, trace_id, node_id, context, retry, *, delay=0, priority_boost=0):
    """统一建任务入口：先把节点语义翻译成能力标签与优先级，再交给黑板。"""
    node = spec.nodes.get(node_id)
    if node is None:
        # 目标节点不在定义里（YAML 写坏）：照旧下发，认领时会报"未定义节点"
        return db.create_task(trace_id, node_id, context, retry,
                              workflow=spec.name, delay=delay)
    return db.create_task(trace_id, node_id, context, retry, workflow=spec.name,
                          capability=_capability(node),
                          priority=_priority(node) + priority_boost,
                          delay=delay)
