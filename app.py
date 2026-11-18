"""Aegis 控制台。

职责边界：本文件只做**展示与下发**——任务怎么跑、什么时候停、算不算通过，
全部由 core/ 里的编排引擎、Agent 注册表与工具注册表决定。
视觉全部落在 ui/theme.py（原生 HTML/CSS），不依赖 Streamlit 内部 DOM 结构。
"""
import datetime
import html as _html
import json
import os

import streamlit as st

from core import db
from core.agents import list_agents
from core.config import DEFAULT_WORKFLOW, WORKER_COUNT
from core.engine import (
    EngineBusy, WORKER_STATE, get_workflow, resume_with_advice, start_engine, start_workflow,
)
from core.tools import get_workspace_dir, list_tools, list_workspace_files
from core.workflow import (
    NODE_TYPES, WorkflowError, blank_node, list_workflows, load_workflow,
    save_workflow_data, spec_to_dict, spec_to_yaml, to_dot, validate_data,
)
from ui import canvas, theme

st.set_page_config(
    page_title="Aegis 控制台",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(theme.CSS, unsafe_allow_html=True)
db.init_db()

DEMO_PROMPT = (
    "写一个函数，接收两个数字相除，并打印 10 / 0 的结果。"
    "注意：禁止使用 try/except 等异常捕获，必须让除法真实执行。"
)


def _fill_prompt(text: str) -> None:
    """示例需求回填。

    必须走 on_click 回调：脚本跑到按钮时，侧边栏的 text_area(key="prompt_box")
    早就实例化过了，此时直接写 st.session_state.prompt_box 会被 Streamlit 拒绝
    （cannot be modified after the widget is instantiated）。回调在下一次脚本运行
    之前执行，那时 widget 还不存在，赋值合法。
    """
    st.session_state.prompt_box = text


@st.cache_resource
def _boot_engine():
    """引擎全局单例：整个 Streamlit 进程只启动一组 Worker，
    避免每个浏览器会话各起一个线程互相抢任务。"""
    try:
        start_engine()
        return None
    except EngineBusy as exc:      # 另一个进程已在跑：不崩，明确提示
        return str(exc)


ENGINE_BUSY = _boot_engine()
if ENGINE_BUSY:                    # 引擎没抢到租约：界面照常可看历史，但不再派新任务
    st.warning("引擎未启动 —— " + ENGINE_BUSY, icon="⚠️")

# ---------------------------------------------------------------- 工作流目录
_wf_err = ""
try:
    _WORKFLOWS = list_workflows()
except Exception as exc:            # 定义写坏也不要把整页拖垮
    _WORKFLOWS = {}
    _wf_err = str(exc)
_wf_names = sorted(_WORKFLOWS) or [DEFAULT_WORKFLOW]

# ---------------------------------------------------------------- 侧边栏
with st.sidebar:
    st.markdown(theme.brand(), unsafe_allow_html=True)
    view = st.radio("视图", ["执行控制台", "编排画布"], horizontal=True,
                    label_visibility="collapsed", key="view_mode")

    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        st.markdown(theme.warn_card(
            "未检测到 DEEPSEEK_API_KEY —— 请在项目根目录按 .env.example 创建 .env 后重启。"
            "缺少密钥时大模型节点会全部失败。"), unsafe_allow_html=True)
    if _wf_err:
        st.markdown(theme.warn_card(f"工作流定义加载失败：{_wf_err}"), unsafe_allow_html=True)

    st.markdown('<div class="ag-sec">任务下发</div>', unsafe_allow_html=True)
    if len(_wf_names) > 1:
        wf_name = st.selectbox(
            "工作流",
            _wf_names,
            index=_wf_names.index(DEFAULT_WORKFLOW) if DEFAULT_WORKFLOW in _wf_names else 0,
            label_visibility="collapsed",
        )
    else:
        wf_name = _wf_names[0]
        st.caption(f"工作流：`{wf_name}`（目录下只有一个定义）")

    _sel_spec = _WORKFLOWS.get(wf_name)
    if _sel_spec is not None:
        st.markdown(theme.kv([("节点", len(_sel_spec.nodes)),
                              ("版本", f"v{_sel_spec.version}"),
                              ("入口", _sel_spec.entry)]), unsafe_allow_html=True)

    prompt = st.text_area("需求描述", DEMO_PROMPT, height=146, key="prompt_box")

    if st.button("启动任务", type="primary", width="stretch"):
        try:
            trace_id = start_workflow(wf_name, prompt)
            st.session_state.trace_id = trace_id
            st.session_state.wf_name = wf_name
            st.query_params["trace"] = trace_id
            st.success(f"已下发 · trace `{trace_id}`")
        except WorkflowError as exc:
            st.error(f"工作流定义有误，未下发：\n{exc}")

    # 最近任务（从任务黑板读，纯展示）
    try:
        _recent = db.recent_traces(6)
    except Exception:
        _recent = []
    if _recent:
        st.markdown('<div class="ag-sec">最近任务</div>', unsafe_allow_html=True)
        for _tid, _n, _wf, _stt, _ts in _recent:
            st.markdown(theme.trace_row(_tid, _stt or "pending", _ts or "—"),
                        unsafe_allow_html=True)
        if st.button("打开最近一条", width="stretch"):
            st.session_state.trace_id = _recent[0][0]
            st.session_state.pop("wf_name", None)
            st.query_params["trace"] = _recent[0][0]
            st.rerun()

    st.markdown(theme.footer("aegis · workflows/*.yaml"),
                unsafe_allow_html=True)


# ---------------------------------------------------------------- 编排画布（可视化编辑）
# 编辑对象就是 workflows/*.yaml 本身：界面改完 -> 校验 -> 原子写盘 -> 引擎按 mtime 热加载。
# 所有控件值每轮直接写回「草稿 dict」（草稿不是 widget key，改它合法），
# 加/删节点或还原时才 bump 一代 rev，让控件回到新默认值——避免 session_state 的老坑。

def _draft_of(name: str) -> dict:
    key = f"wf_draft::{name}"
    if key not in st.session_state:
        st.session_state[key] = spec_to_dict(load_workflow(name, use_cache=False))
    return st.session_state[key]


def _rev_of(name: str) -> int:
    return int(st.session_state.get(f"wf_rev::{name}", 0))


def _bump(name: str, pending: str | None = None) -> None:
    st.session_state[f"wf_rev::{name}"] = _rev_of(name) + 1
    if pending:
        st.session_state[f"wf_pending::{name}"] = pending


def _node_of(draft: dict, node_id: str) -> dict:
    for node in draft.get("nodes") or []:
        if node.get("id") == node_id:
            return node
    return {}


def _split(text: str) -> list:
    return [x.strip() for x in (text or "").replace("，", ",").split(",") if x.strip()]


def _delete_node(name: str, node_id: str) -> None:
    """回调里删节点：这里允许改 session_state（脚本尚未渲染控件）。"""
    draft = st.session_state.get(f"wf_draft::{name}") or {}
    draft["nodes"] = [n for n in draft.get("nodes") or [] if n.get("id") != node_id]
    st.session_state.pop(f"wf_pending::{name}", None)
    _bump(name)


def _render_node_editor(draft: dict, name: str) -> None:
    rev = _rev_of(name)
    nodes = draft.setdefault("nodes", [])
    ids = [str(n.get("id")) for n in nodes]

    st.markdown('<div class="ag-sec">节点</div>', unsafe_allow_html=True)
    if not ids:
        st.markdown(theme.warn_card("这份定义还没有节点，先在下面添加一个入口节点。"),
                    unsafe_allow_html=True)
        return

    pending = st.session_state.pop(f"wf_pending::{name}", None)
    index = ids.index(pending) if pending in ids else 0
    sel = st.selectbox("选择节点", ids, index=index, key=f"sel::{name}::{rev}")
    node = _node_of(draft, sel)
    k = f"{name}::{rev}::{sel}"          # 每个节点一套独立控件，切换节点不串值

    types = sorted(NODE_TYPES)
    ntype = st.selectbox("类型", types,
                         index=types.index(node.get("type", "llm")),
                         key=f"t::{k}")
    label = st.text_input("显示名", value=node.get("label", ""), key=f"lb::{k}")

    agent = tool = ""
    if ntype == "llm":
        names = [a["name"] for a in list_agents()]
        agent = st.selectbox("Agent", names,
                             index=names.index(node["agent"]) if node.get("agent") in names else 0,
                             key=f"ag::{k}")
    elif ntype == "tool":
        tool_names = list_tools()
        tool = st.selectbox("工具", tool_names,
                            index=tool_names.index(node["tool"]) if node.get("tool") in tool_names else 0,
                            key=f"tl::{k}")

    context = st.text_area("上下文模板", value=node.get("context", "{prompt}"),
                           height=64, key=f"cx::{k}")
    publish = st.text_input("成功后写入变量 publish", value=node.get("publish", ""), key=f"pb::{k}")

    c1, c2 = st.columns(2)
    with c1:
        nxt = st.text_input("next", value=node.get("next", "") or "", key=f"nx::{k}")
        on_success = st.text_input("on_success", value=node.get("on_success", "") or "", key=f"ok::{k}")
        on_failure = st.text_input("on_failure", value=node.get("on_failure", "") or "", key=f"ng::{k}")
    with c2:
        on_exhausted = st.text_input("on_exhausted", value=node.get("on_exhausted", "") or "", key=f"ex::{k}")
        on_resume = st.text_input("on_resume", value=node.get("on_resume", "") or "", key=f"rs::{k}")
        max_retries = st.number_input("max_retries", value=int(node.get("max_retries") or 0),
                                      min_value=0, step=1, key=f"mr::{k}")

    routes_text = st.text_area(
        "条件分支 routes（每行 标签=目标）",
        value="\n".join("%s=%s" % (a, b) for a, b in (node.get("routes") or {}).items()),
        height=64, key=f"rt::{k}")
    parallel_text = st.text_input("parallel（逗号分隔）",
                                  value=",".join(node.get("parallel") or []), key=f"pl::{k}")
    join_text = st.text_input("join（逗号分隔，仅 join 节点）",
                              value=",".join(node.get("join") or []), key=f"jn::{k}")

    # 控件值即真相：直接写回草稿（草稿是普通 dict，不是 widget key）
    fresh: dict = {"id": sel, "type": ntype}
    for field_name, value in (("label", label), ("agent", agent), ("tool", tool),
                              ("context", context), ("publish", publish), ("next", nxt),
                              ("on_success", on_success), ("on_failure", on_failure),
                              ("on_exhausted", on_exhausted), ("on_resume", on_resume)):
        if value:
            fresh[field_name] = value
    routes = {}
    for line in (routes_text or "").splitlines():
        label_part, sep, target = line.partition("=")
        if sep and label_part.strip() and target.strip():
            routes[label_part.strip()] = target.strip()
    if routes:
        fresh["routes"] = routes
    if max_retries:
        fresh["max_retries"] = int(max_retries)
    if _split(parallel_text):
        fresh["parallel"] = _split(parallel_text)
    if _split(join_text):
        fresh["join"] = _split(join_text)
    for i, item in enumerate(nodes):
        if item.get("id") == sel:
            nodes[i] = fresh
            break

    st.button("删除该节点", key=f"del::{k}", width="stretch",
              on_click=_delete_node, args=(name, sel))

    st.markdown('<div class="ag-sec">新增节点</div>', unsafe_allow_html=True)
    n1, n2 = st.columns([1.2, 1])
    with n1:
        new_id = st.text_input("节点 id", key=f"nid::{name}::{rev}", placeholder="如 analyze")
    with n2:
        new_type = st.selectbox("类型", types, key=f"ntp::{name}::{rev}")
    if st.button("添加节点", key=f"add::{name}::{rev}", width="stretch"):
        candidate = (new_id or "").strip()
        if not candidate:
            st.warning("节点 id 不能为空")
        elif candidate in ids:
            st.warning(f"节点 id 已存在：{candidate}")
        else:
            nodes.append(blank_node(candidate, new_type))
            _bump(name, pending=candidate)
            st.rerun()


def _render_workflow_actions(draft: dict, name: str) -> None:
    ids = [str(n.get("id")) for n in (draft.get("nodes") or [])]
    if ids:
        current = draft.get("entry")
        draft["entry"] = st.selectbox(
            "入口节点 entry", ids,
            index=ids.index(current) if current in ids else 0, key=f"entry::{name}")
    draft["version"] = st.number_input(
        "版本 version", value=int(draft.get("version") or 1), min_value=1, step=1,
        key=f"ver::{name}")
    draft["inputs"] = [line.strip() for line in str(st.text_area(
        "入参文件 inputs（每行一个，examples/ 下的相对路径）",
        value="\n".join(draft.get("inputs") or []),
        key=f"inputs::{name}", height=88)).splitlines() if line.strip()]
    if draft["inputs"]:
        st.caption("启动任务时这些文件会先复制进工作区（真实输入才谈得上复现）："
                   + "、".join(f"`{p}`" for p in draft["inputs"]))

    c1, c2 = st.columns(2)
    with c1:
        if st.button("保存到 workflows/", type="primary", width="stretch",
                     key=f"save::{name}"):
            try:
                path = save_workflow_data(name, draft)
                st.success(f"已写入 {path.name}；引擎按 mtime 热加载，无需重启。")
            except WorkflowError as exc:
                st.error("保存被拒，文件未改动：")
                for item in exc.errors:
                    st.text(f"· {item}")
        st.caption("保存会按定义重排 YAML：文件顶部注释保留，**逐节点注释不保留**。")
    with c2:
        if st.button("还原为文件内容", width="stretch", key=f"reload::{name}"):
            st.session_state[f"wf_draft::{name}"] = spec_to_dict(
                load_workflow(name, use_cache=False))
            _bump(name)
            st.rerun()


def _render_workflow_editor(wf_names: list) -> None:
    st.markdown(theme.panel_head(
        "编排画布", "改 YAML 的图形化入口：所见即所得，保存后引擎热加载"),
        unsafe_allow_html=True)

    name = st.selectbox("工作流文件", wf_names, key="edit_wf")
    try:
        draft = _draft_of(name)
    except WorkflowError as exc:
        st.markdown(theme.warn_card(f"读取失败：{exc}"), unsafe_allow_html=True)
        return

    # 画布先占位：编辑控件在下方，等草稿写完再回填，保证画布画的是"刚改完"的版本
    canvas_slot = st.empty()
    caption_slot = st.empty()

    edit_col, side_col = st.columns([1.25, 1], gap="large")
    with edit_col:
        st.markdown('<div class="ag-sec">节点编辑</div>', unsafe_allow_html=True)
        _render_node_editor(draft, name)
    with side_col:
        st.markdown('<div class="ag-sec">流程与保存</div>', unsafe_allow_html=True)
        _render_workflow_actions(draft, name)
        st.markdown('<div class="ag-sec">YAML 预览</div>', unsafe_allow_html=True)
        yaml_slot = st.empty()

    spec = None
    errors: list = []
    try:
        spec = validate_data(draft, name)
    except WorkflowError as exc:
        errors = exc.errors
    except Exception as exc:                      # 防御：任何异常都别把页面拖垮
        errors = [f"{type(exc).__name__}: {exc}"]

    if spec is not None:
        canvas_slot.markdown(canvas.render(spec), unsafe_allow_html=True)
        caption_slot.caption(
            f"{len(spec.nodes)} 节点 · {len(canvas.edges(spec))} 条转移 · 入口 {spec.entry} · "
            "画布随编辑实时更新，点「保存到 workflows/」才落盘")
        yaml_slot.code(spec_to_yaml(spec), language="yaml")
    else:
        canvas_slot.markdown(theme.warn_card(
            "当前定义校验未通过，画布暂不可渲染：\n" + "\n".join(f"· {e}" for e in errors)),
            unsafe_allow_html=True)
        yaml_slot.code("\n".join(errors) or "（无内容）", language="text")


if view == "编排画布":
    _render_workflow_editor(_wf_names)
    st.stop()


# ---------------------------------------------------------------- 数据读取
def _current_spec():
    """当前 trace 用的工作流定义（优先按 trace 记忆里的名字取）"""
    name = st.session_state.get("wf_name")
    trace_id = st.session_state.get("trace_id")
    if trace_id:
        name = db.get_var(trace_id, "workflow", name)
    try:
        return get_workflow(name)
    except WorkflowError as exc:
        st.error(f"工作流定义加载失败：{exc}")
        return None


def _fetch_state(trace_id, spec):
    conn = db.get_conn()
    try:
        node_ids = list(spec.nodes) if spec else []
        states = db.node_statuses(trace_id, node_ids)
        waiting = db.get_waiting_human(trace_id)
        logs = conn.execute(
            "SELECT timestamp, level, message FROM logs WHERE trace_id=? "
            "ORDER BY id DESC LIMIT 40", (trace_id,)).fetchall()
        stats = {
            row[0]: (int(row[1] or 0), int(row[2] or 0))
            for row in conn.execute(
                "SELECT node_id, MAX(retry_count), COUNT(*) FROM tasks "
                "WHERE trace_id=? GROUP BY node_id", (trace_id,)).fetchall()
        }
    finally:
        conn.close()
    return {"nodes": states, "human_task": waiting, "logs": logs, "stats": stats}


# ---------------------------------------------------------------- 拓扑图
def _render_graph(spec, states):
    """执行拓扑：DOT 由 YAML 生成。

    用 Streamlit 内置的 st.graphviz_chart，而不是 mermaid —— mermaid 要从 CDN 拉 JS，
    比赛现场或内网一断网，图上就只剩一段源码；viz.js 是打包在 Streamlit 前端资源里的，
    离线也能渲染。
    """
    dot = to_dot(spec, states)
    try:
        st.graphviz_chart(dot, width="stretch")
    except TypeError:                      # 老版本不支持该参数
        st.graphviz_chart(dot)


# ---------------------------------------------------------------- 面板
def _render_kpis(trace_id, spec):
    state = _fetch_state(trace_id, spec)
    states = list(state["nodes"].values())
    done = states.count("done")
    running = states.count("running")
    failed = states.count("failed")
    waiting = 1 if state["human_task"] else 0
    steps = db.count_tasks(trace_id)

    # LLM 用量：引擎把累计值写在 trace_vars["llm_usage"]
    usage = {}
    raw = db.get_var(trace_id, "llm_usage", "") or ""
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            usage = json.loads(raw)
        except json.JSONDecodeError:
            usage = {}

    st.markdown(
        theme.kpis([
            ("已完成节点", done, theme.GREEN),
            ("执行中", running, theme.BLUE2),
            ("失败", failed, theme.RED),
            ("待人工", waiting, theme.AMBER),
            ("执行步数", steps, theme.BLUE),
            ("工具调用", int(usage.get("tools", 0) or 0), theme.SLATE),
            ("LLM tokens", int(usage.get("total", 0) or 0), theme.SLATE),
        ]),
        unsafe_allow_html=True,
    )


def _render_live(trace_id, spec):
    state = _fetch_state(trace_id, spec)

    st.markdown(theme.panel_head("执行拓扑", f"{spec.path.name} · {len(spec.nodes)} 节点"),
                unsafe_allow_html=True)
    with st.container(border=True):
        _render_graph(spec, state["nodes"])


    st.markdown(
        theme.panel_head("节点明细", "最新状态")
        + '<div class="ag-card">'
        + theme.node_table(spec, state["nodes"], state["stats"])
        + "</div>",
        unsafe_allow_html=True,
    )

    st.markdown(theme.panel_head("执行日志", f"最近 {len(state['logs'])} 条"),
                unsafe_allow_html=True)
    st.markdown(theme.log_list(state["logs"]), unsafe_allow_html=True)


def _render_human(trace_id, spec):
    state = _fetch_state(trace_id, spec)
    human_task = state["human_task"]

    with st.container(border=True):
        st.markdown(theme.panel_head("人工介入中心", "熔断后自动接管"),
                    unsafe_allow_html=True)
        if human_task:
            task_id, node_id, ctx = human_task
            st.markdown(theme.warn_card(
                f"节点 [{node_id}] 连续自动修复失败，已熔断：工作流停在 waiting_human，"
                f"等待人类专家给出方向。"), unsafe_allow_html=True)
            with st.expander("查看完整失败历史"):
                st.code(ctx or "（无记录）", language="text")
            advice = st.text_area(
                "修复建议", height=110, key=f"advice_{task_id}",
                placeholder="例如：捕获 ZeroDivisionError，提示“除数不能为零”并返回 None",
            )
            if st.button("提交建议并恢复", key=f"resume_{task_id}",
                         type="primary", width="stretch"):
                if advice.strip():
                    try:
                        target = resume_with_advice(trace_id, task_id, advice.strip())
                    except WorkflowError as exc:
                        st.error(f"恢复失败：{exc}")
                        return
                    st.success(f"建议已下发 → 恢复至 [{target}]（修复预算已重置）")
                    if FRAGMENTS_OK:
                        st.rerun(scope="fragment")
                    else:
                        st.rerun()
                else:
                    st.error("请先填写修复建议再提交")
        else:
            st.markdown(
                '<div class="ag-card"><div style="padding:14px;color:#6B7788;font-size:.78rem;'
                'line-height:1.6">当前没有等待人工介入的任务。<br>'
                '当修复 Agent 连续失败达到 max_retries 预算时触发熔断，卡片会在几秒内自动出现。'
                "</div></div>",
                unsafe_allow_html=True,
            )

    # 交付物
    with st.container(border=True):
        files = list_workspace_files(trace_id)
        st.markdown(theme.panel_head("任务交付物", f"{len(files)} 个文件"),
                    unsafe_allow_html=True)
        if not files:
            st.markdown(theme.file_rows([], {}), unsafe_allow_html=True)
        else:
            sizes = {}
            for name in files:
                try:
                    sizes[name] = (get_workspace_dir(trace_id) / name).stat().st_size
                except OSError:
                    sizes[name] = 0
            st.markdown(theme.file_rows(files, sizes), unsafe_allow_html=True)
            sel = st.selectbox("预览", files, key=f"dsel_{trace_id}",
                               label_visibility="collapsed")
            try:
                data = (get_workspace_dir(trace_id) / sel).read_bytes()
            except OSError as exc:
                st.error(f"读取失败：{exc}")
            else:
                try:
                    st.code(data.decode("utf-8")[:2600], language="python")
                except UnicodeDecodeError:
                    st.caption(f"二进制文件，共 {len(data)} 字节")
                st.download_button("下载该文件", data=data, file_name=sel,
                                   key=f"dl_{trace_id}_{sel}", width="stretch")

    # 工作记忆
    vars_ = db.get_vars(trace_id)
    order = ["workflow", "prompt", "spec", "last_error", "advice"]
    pairs = [(k, vars_[k]) for k in order if k in vars_]
    pairs += [(k, v) for k, v in vars_.items() if k not in order]
    st.markdown(theme.panel_head("trace 工作记忆", f"{len(pairs)} 个变量"),
                unsafe_allow_html=True)
    st.markdown(theme.var_list(pairs), unsafe_allow_html=True)

    # 运行环境自证：同一个 trace 换台机器也该能复现
    import platform
    st.markdown(theme.panel_head("运行环境", "可复现性自证"), unsafe_allow_html=True)
    st.markdown(theme.kv([
        ("Python", platform.python_version()),
        ("Worker", WORKER_COUNT),
        ("Agent", len(list_agents())),
        ("工作流", len(_WORKFLOWS)),
        ("DB", "aegis_platform.db"),
    ]), unsafe_allow_html=True)

    # 调度器观测：能力分区 / 积压 / 最长等待——让"调度算法"在界面上可见
    snap = db.scheduler_snapshot()
    workers = [(name, w.get("caps", ["*"]), w.get("claimed", 0))
               for name, w in sorted(WORKER_STATE.items())] or [("worker-0", ["*"], 0)]
    st.markdown(theme.panel_head(
        "调度器", f"{len(workers)} 个 Worker · 在跑 {snap.get('running', 0)}"),
        unsafe_allow_html=True)
    st.markdown(theme.worker_list(workers), unsafe_allow_html=True)
    st.markdown(theme.sched_table(snap.get("buckets", [])), unsafe_allow_html=True)


# ---------------------------------------------------------------- 定时刷新
FRAGMENTS_OK = True
try:
    _kpis = st.fragment(run_every=3)(_render_kpis)
    _live = st.fragment(run_every=3)(_render_live)
    _human = st.fragment(run_every=5)(_render_human)
except AttributeError:                        # streamlit < 1.37 优雅降级
    FRAGMENTS_OK = False
    _kpis, _live, _human = _render_kpis, _render_live, _render_human


# ---------------------------------------------------------------- 主区
if "trace_id" not in st.session_state:
    # F5 刷新后从 URL 恢复正在看的那条 trace
    restored = st.query_params.get("trace")
    if restored:
        st.session_state.trace_id = restored
        st.rerun()
    _d = _WORKFLOWS.get(DEFAULT_WORKFLOW)
    st.markdown(theme.hero("", DEFAULT_WORKFLOW,
                           getattr(_d, "version", None),
                           len(getattr(_d, "nodes", {}) or {})),
                unsafe_allow_html=True)
    st.markdown(theme.empty_state(), unsafe_allow_html=True)
    st.markdown(theme.panel_head("示例需求", "填进左侧输入框后可直接启动"), unsafe_allow_html=True)
    _ex = [
        ("生成销售汇总 CSV",
         "生成一个 5 行的销售数据 CSV 到 sales.csv（列：日期,产品,数量,金额），并打印总金额。"),
        ("爬取书籍信息",
         "爬取 https://books.toscrape.com 首页的书名和价格，存成 books.csv，并打印抓到的条数。"),
        ("演示熔断与人工介入", DEMO_PROMPT),
    ]
    for _label, _text in _ex:
        _c1, _c2 = st.columns([4.8, 1], gap="small")
        with _c1:
            st.markdown(f'<div class="ag-ex"><b>{_label}</b><span>{_text}</span></div>',
                        unsafe_allow_html=True)
        with _c2:
            st.button("填入", key=f"ex_{_label}", width="stretch",
                      on_click=_fill_prompt, args=(_text,))
    st.stop()

trace_id = st.session_state.trace_id
spec = _current_spec()
if spec is None:
    st.stop()

st.markdown(theme.hero(trace_id, spec.name, spec.version, len(spec.nodes),
                       tick=datetime.datetime.now().strftime("%H:%M:%S")),
            unsafe_allow_html=True)

_kpis(trace_id, spec)

left, right = st.columns([1.58, 1], gap="medium")
with left:
    _live(trace_id, spec)
with right:
    _human(trace_id, spec)