"""集中配置：超时、阈值、路径。调参改这里即可，不用翻业务代码。"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 单次运行 main.py 的超时（秒）：爬虫/数据处理类任务按需调大
SANDBOX_TIMEOUT = 30

# 依赖安装（venv 创建 + pip install）的超时（秒）
INSTALL_TIMEOUT = 180

# running 状态超过该秒数视为卡死，自动回收（进程崩溃/重启后的自愈）
# 注意：必须显著大于 SANDBOX_TIMEOUT + INSTALL_TIMEOUT，否则会误杀正在装依赖的任务
STALE_SECONDS = 600

# 每个任务的工作区根目录：workspace/<trace_id>/（代码文件、依赖、交付物都在这里）
WORKSPACES_DIR = BASE_DIR / "workspace"
