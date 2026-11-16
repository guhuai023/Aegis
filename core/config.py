"""集中配置：超时、阈值、路径、并发。调参改这里即可，不用翻业务代码。"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 单次运行 main.py 的超时（秒）：爬虫/数据处理类任务按需调大
SANDBOX_TIMEOUT = 30

# 依赖安装（venv 创建 + pip install）的超时（秒）
INSTALL_TIMEOUT = 180

# running 状态超过该秒数视为卡死，自动回收（进程崩溃/重启后的自愈）
# 注意：必须显著大于 SANDBOX_TIMEOUT + INSTALL_TIMEOUT，否则会误杀正在装依赖的任务
STALE_SECONDS = 600

# ---------------- 编排引擎 ----------------

# 工作流定义目录：workflows/*.yaml
WORKFLOWS_DIR = BASE_DIR / "workflows"

# Agent 定义目录：agents/*.yaml
AGENTS_DIR = BASE_DIR / "agents"

# 默认工作流（历史任务没有 workflow 字段时回退到它）
DEFAULT_WORKFLOW = "code_gen"

# Worker 线程数。认领是数据库层原子操作，多 Worker 天然安全；
# 想演示并发调度把它调大即可（每个 Worker 各自认领不同任务）。
WORKER_COUNT = 1

# join 节点轮询间隔（秒）：并行分支未全部完成时，隔多久回来再看一次
JOIN_POLL_SECONDS = 2

# 单条 trace 允许的最大节点执行次数，防止 YAML 写出无边界的死循环
MAX_STEPS_PER_TRACE = 200

# 拼进上下文的文件数上限（fixer 需要看到当前代码）
MAX_FILES_IN_CONTEXT = 20
MAX_FILE_CHARS_IN_CONTEXT = 3000

# 每个任务的工作区根目录：workspace/<trace_id>/（代码文件、依赖、交付物都在这里）
WORKSPACES_DIR = BASE_DIR / "workspace"