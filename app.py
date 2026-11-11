import os
import uuid

import streamlit as st
import streamlit.components.v1 as components

from core.db import init_db, create_task, get_conn, finish_task, log_event
from core.engine import start_engine
from core.tools import get_workspace_dir, list_workspace_files

st.set_page_config(page_title="Aegis 控制台", layout="wide")

# 初始化
init_db()


@st.cache_resource
def _boot_engine():
    """引擎全局单例：整个 Streamlit 进程只启动一个 Worker，
    避免旧版每个浏览器会话各起一个线程互相抢任务。"""
    start_engine()
    return True


_boot_engine()

st.title("🛡️ Aegis: 多 Agent 智能体协同与可信交付平台")

if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
    st.sidebar.warning("⚠️ 未检测到 DEEPSEEK_API_KEY：请在项目根目录创建 .env（参考 .env.example）后重启应用")

# 侧边栏下发任务
with st.sidebar:
    st.header("任务下发")
    prompt = st.text_area(
        "输入开发需求：",
        "写一个函数，接收两个数字相除，并打印 10 / 0 的结果。"
        "注意：禁止使用 try/except 等异常捕获，必须让除法真实执行。",
    )
    if st.button("🚀 启动工作流"):
        trace_id = str(uuid.uuid4())[:8]
        st.session_state.trace_id = trace_id
        st.query_params["trace"] = trace_id  # 写入 URL，F5 刷新后追踪不丢失
        create_task(trace_id, "router", prompt)
        st.success(f"任务已下发！追踪ID: {trace_id}")

C_MAP = {"done": "#4CAF50", "running": "#FFC107", "failed": "#F44336",
         "waiting_human": "#9C27B0", "pending": "#E0E0E0"}


def _fetch_state(trace_id):
    conn = get_conn()

    def get_status(nid):
        rows = conn.execute("SELECT status FROM tasks WHERE trace_id=? AND node_id=?",
                            (trace_id, nid)).fetchall()
        statuses = [r[0] for r in rows]
        if "waiting_human" in statuses: return "waiting_human"
        if "failed" in statuses: return "failed"
        if "running" in statuses or "pending" in statuses: return "running"
        if "done" in statuses: return "done"
        return "pending"

    state = {
        "router": get_status("router"),
        "coder": get_status("coder"),
        "fixer": get_status("fixer"),
        "human": get_status("human"),
        "human_task": conn.execute(
            "SELECT task_id, input_context FROM tasks "
            "WHERE trace_id=? AND status='waiting_human' ORDER BY rowid DESC LIMIT 1",
            (trace_id,)).fetchone(),
        "logs": conn.execute(
            "SELECT timestamp, level, message FROM logs WHERE trace_id=? ORDER BY id DESC LIMIT 20",
            (trace_id,)).fetchall(),
    }
    conn.close()
    return state


def _render_live_panel(trace_id):
    """实时面板：自动刷新拓扑图与执行日志"""
    state = _fetch_state(trace_id)
    st.subheader("📊 实时执行拓扑")

    mermaid_str = f"""
            graph LR
                R[分诊台 Router] --> C[开发 Agent]
                C --> T{{物理验证锚点}}
                T -->|报错| F[修复 Agent]
                F --> T
                F -.-> H{{人工介入}}
                H -.-> F

                style R fill:{C_MAP[state['router']]},color:#000
                style C fill:{C_MAP[state['coder']]},color:#000
                style T fill:{C_MAP[state['coder']]},color:#000
                style F fill:{C_MAP[state['fixer']]},color:#000
                style H fill:{C_MAP[state['human']]},color:#000
            """

    html_code = f"""
            <html>
            <body>
                <script type="module">
                    import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
                    mermaid.initialize({{ startOnLoad: true, theme: 'default' }});
                </script>
                <pre class="mermaid" style="text-align: center;">
                    {mermaid_str}
                </pre>
            </body>
            </html>
            """
    # 用 iframe 嵌入网页，彻底避免 Streamlit 原生 markdown 渲染失败的问题
    components.html(html_code, height=380)

    st.subheader("📜 执行日志（最近 20 条）")
    log_text = "\n".join(f"{ts} [{level}] {msg}" for ts, level, msg in state["logs"])
    st.code(log_text if log_text else "暂无日志", language="text")

    _render_deliverables(trace_id)


def _render_deliverables(trace_id):
    """任务交付物：工作区文件预览与下载"""
    files = list_workspace_files(trace_id)
    with st.expander(f"📦 任务交付物（{len(files)} 个文件）", expanded=bool(files)):
        if not files:
            st.caption("工作区还没有产出文件（验收通过后代码会保存在 workspace/<追踪ID>/ 下）")
            return
        sel = st.selectbox("选择文件", files, key=f"dsel_{trace_id}")
        try:
            data = (get_workspace_dir(trace_id) / sel).read_bytes()
        except OSError as e:
            st.error(f"读取失败: {e}")
            return
        try:
            st.code(data.decode("utf-8")[:3000], language="python")
        except UnicodeDecodeError:
            st.caption(f"二进制文件，共 {len(data)} 字节")
        st.download_button("⬇️ 下载该文件", data=data, file_name=sel,
                           key=f"dl_{trace_id}_{sel}")


# streamlit >= 1.37 支持 fragment 定时自动局部刷新；老版本优雅降级为手动刷新
FRAGMENTS_OK = True
try:
    _live_panel = st.fragment(run_every=3)(_render_live_panel)
except AttributeError:
    _live_panel = _render_live_panel
    FRAGMENTS_OK = False


def _render_human_center(trace_id):
    """人工介入中心：熔断后卡片自动出现在这里（随 fragment 定时刷新）"""
    state = _fetch_state(trace_id)
    human_task = state["human_task"]
    if human_task:
        st.warning("🚨 Agent 多次自动修复失败，已熔断，等待人类专家建议！")
        with st.expander("查看完整失败历史"):
            st.code(human_task[1] or "（无记录）", language="text")
        advice = st.text_area("你的修复建议：",
                              placeholder="例如：捕获 ZeroDivisionError，打印'除数不能为零'",
                              key=f"advice_{human_task[0]}")
        if st.button("🩹 提交建议并恢复工作流", key=f"resume_{human_task[0]}"):
            if advice.strip():
                finish_task(human_task[0], "done", "人工建议已采纳")
                resume_ctx = (f"{human_task[1]}\n\n【人工专家修复建议】：{advice.strip()}"
                              "\n请严格按照人类建议修复代码。")
                # 人工介入后重置修复预算：fixer 重新获得 3 次机会
                create_task(trace_id, "fixer", resume_ctx, retry_count=0)
                log_event(trace_id, "HUMAN", "🩹 收到人工建议，工作流已恢复（修复预算已重置）")
                st.success("建议已下发，修复 Agent 已重新启动！")
                if FRAGMENTS_OK:
                    st.rerun(scope="fragment")
                else:
                    st.rerun()
            else:
                st.error("请先输入修复建议再提交")
    else:
        st.info("当前没有等待人工介入的任务。"
                "当修复 Agent 连续失败达到上限触发熔断时，卡片会在几秒内自动出现在这里。")


if FRAGMENTS_OK:
    _human_center = st.fragment(run_every=5)(_render_human_center)
else:
    _human_center = _render_human_center


if "trace_id" in st.session_state:
    trace_id = st.session_state.trace_id
    st.caption(f"当前追踪ID: `{trace_id}`")

    left, right = st.columns([1, 1])

    with left:
        _live_panel(trace_id)

    with right:
        _human_center(trace_id)
else:
    # F5 刷新后从 URL 恢复追踪 ID，避免丢失正在执行的工作流
    restored = st.query_params.get("trace")
    if restored:
        st.session_state.trace_id = restored
        st.rerun()
    st.info("👈 在左侧输入开发需求并点击「启动工作流」开始体验")
