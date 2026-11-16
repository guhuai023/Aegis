"""工具层：结构化规格解析、任务工作区、依赖安装、物理断言式验收。

安全说明：生成的代码仍以当前用户权限运行（不是隔离沙箱）。
生产环境请改用 Docker / 受限进程执行不可信代码。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from core.config import SANDBOX_TIMEOUT, INSTALL_TIMEOUT, WORKSPACES_DIR

SEC_REQUIREMENTS = "=== REQUIREMENTS ==="
SEC_ASSERTIONS = "=== ASSERTIONS ==="
FILE_MARKER = re.compile(r"^===\s*FILE:\s*(.+?)\s*===$", re.MULTILINE)


@dataclass
class CodeSpec:
    files: dict = field(default_factory=dict)      # 相对路径 -> 文件内容
    requirements: list = field(default_factory=list)  # pip 包名
    assertions: list = field(default_factory=list)    # 验收断言行


@dataclass
class ExecResult:
    ok: bool
    report: str        # 人读的验收报告
    files: dict        # 本次写入工作区的文件
    stdout: str = ""
    artifacts: list = field(default_factory=list)  # 运行产生的新文件（交付物）


# ---------------- 规格解析 ----------------

def parse_codegen_spec(text):
    """解析模型输出的结构化规格。

    兼容三种形态：完整格式 / 缺少某节 / 完全无标记的纯代码（整体视为 main.py，
    与旧版行为向后兼容）。
    """
    text = (text or "").replace("```python", "").replace("```", "").strip()
    spec = CodeSpec()
    if not text:
        return spec
    if "=== FILE:" not in text:
        spec.files["main.py"] = text
        return spec

    def _section(header):
        m = re.search(re.escape(header) + r"(.*?)(?=\n===|\Z)", text, re.S)
        return m.group(1).strip() if m else ""

    spec.requirements = [ln.strip() for ln in _section(SEC_REQUIREMENTS).splitlines() if ln.strip()]
    spec.assertions = [ln.strip() for ln in _section(SEC_ASSERTIONS).splitlines() if ln.strip()]

    # 从正文里剥掉两个非文件节，避免被并入最后一个文件的内容
    body = re.sub(re.escape(SEC_REQUIREMENTS) + r".*?(?=\n===|\Z)", "", text, flags=re.S)
    body = re.sub(re.escape(SEC_ASSERTIONS) + r".*?(?=\n===|\Z)", "", body, flags=re.S)

    parts = FILE_MARKER.split(body)  # [前导, 名1, 内容1, 名2, 内容2, ...]
    for i in range(1, len(parts), 2):
        name, content = parts[i].strip(), parts[i + 1].strip()
        if name and content:
            spec.files[name] = content
    if not spec.files:
        spec.files["main.py"] = text
    return spec


# ---------------- 任务工作区 ----------------

def get_workspace_dir(trace_id) -> Path:
    """每个 trace 一个工作区：workspace/<trace_id>/，代码、依赖、交付物都在这里"""
    ws = WORKSPACES_DIR / trace_id
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _safe_relpath(name, ws: Path):
    """校验模型给出的文件名，防路径穿越/覆盖敏感目录；合法返回绝对 Path，否则 None"""
    name = name.replace("\\", "/").strip().lstrip("/")
    if (not name or ".." in name or ":" in name
            or name.lower().startswith(("venv", ".aegis", "requirements.txt"))):
        return None
    p = (ws / name).resolve()
    try:
        p.relative_to(ws.resolve())
    except ValueError:
        return None
    return p


def _cleanup_manifest_files(ws: Path, manifest_name):
    """按清单文件删除上一轮写入的文件（规格文件 / 运行产物），清单本身保留"""
    path = ws / manifest_name
    if not path.exists():
        return
    try:
        for old in json.loads(path.read_text(encoding="utf-8")):
            p = _safe_relpath(old, ws)
            if p and p.is_file():
                p.unlink()
    except (json.JSONDecodeError, OSError):
        pass


def prepare_workspace(trace_id, files):
    """把规格里的文件写入工作区。

    先清掉上一轮的规格文件和运行产物（跨轮隔离：FILE_EXISTS 等产物断言
    只认本轮真实生成的文件，不会被上一轮遗留物骗过），再写入本轮文件。
    返回 (ws, written, skipped)
    """
    ws = get_workspace_dir(trace_id)
    _cleanup_manifest_files(ws, ".aegis_manifest.json")   # 上一轮的规格文件
    _cleanup_manifest_files(ws, ".aegis_artifacts.json")  # 上一轮的运行产物
    written, skipped = [], []
    for name, content in files.items():
        p = _safe_relpath(name, ws)
        if p is None:
            skipped.append(name)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(p.relative_to(ws).as_posix())
    (ws / ".aegis_manifest.json").write_text(json.dumps(written, ensure_ascii=False), encoding="utf-8")
    return ws, written, skipped


def list_workspace_files(trace_id):
    """列出工作区交付物（排除 venv / 缓存 / 内部清单）"""
    ws = WORKSPACES_DIR / trace_id
    if not ws.exists():
        return []
    files = []
    for p in sorted(ws.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ws).as_posix()
        if rel.startswith(("venv/", "__pycache__", ".aegis")) or "__pycache__" in rel:
            continue
        files.append(rel)
    return files


# ---------------- 依赖安装 ----------------

def _venv_python(ws: Path) -> Path:
    return ws / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")


def ensure_venv(trace_id, requirements):
    """按需创建任务级 venv 并安装依赖（按内容哈希跳过重复安装）。

    返回 (ok, msg)。无依赖时直接用系统 Python，不建 venv。
    """
    reqs = sorted({r.strip() for r in requirements if r.strip()})
    if not reqs:
        return True, "无第三方依赖，使用系统 Python"
    ws = get_workspace_dir(trace_id)
    vpy = _venv_python(ws)
    req_hash = hashlib.sha1("\n".join(reqs).encode()).hexdigest()
    hash_file = ws / ".aegis_req_hash"
    if vpy.exists() and hash_file.exists() and hash_file.read_text() == req_hash:
        return True, "依赖未变化，复用已有虚拟环境"

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if not vpy.exists():
            r = subprocess.run([sys.executable, "-m", "venv", str(ws / "venv")],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=INSTALL_TIMEOUT,
                               env=env, creationflags=flags)
            if r.returncode != 0:
                return False, f"创建虚拟环境失败:\n{(r.stderr or '')[-800:]}"
        (ws / "requirements.txt").write_text("\n".join(reqs), encoding="utf-8")
        r = subprocess.run([str(vpy), "-m", "pip", "install", "--disable-pip-version-check",
                            "--no-input", "-r", str(ws / "requirements.txt")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=INSTALL_TIMEOUT,
                           env=env, creationflags=flags)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "")[-800:]
            return False, f"pip 安装失败（{len(reqs)} 个包）:\n{detail}"
        hash_file.write_text(req_hash, encoding="utf-8")
        return True, f"依赖安装完成: {', '.join(reqs)}"
    except subprocess.TimeoutExpired:
        return False, f"依赖安装超时（>{INSTALL_TIMEOUT}s，可在 core/config.py 调整）"
    except Exception as e:
        return False, f"依赖安装异常: {e}"


# ---------------- 物理验收执行 ----------------

def run_workspace(trace_id, assertions, timeout=None):
    """在工作区执行 main.py 并做断言式验收，返回 (ok, report, stdout)。

    断言格式：CONTAINS: 文本 / NOT_CONTAINS: 文本 / REGEX: 正则（每行一条）。
    无断言时退回默认验收：退出码 0 且产生非空输出。
    """
    ws = WORKSPACES_DIR / trace_id
    if not (ws / "main.py").exists():
        return False, "入口文件 main.py 不存在", ""
    timeout = timeout or SANDBOX_TIMEOUT
    vpy = _venv_python(ws)
    py = str(vpy) if vpy.exists() else "python"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONPATH", None)  # 避免宿主环境泄漏进任务
    try:
        r = subprocess.run([py, "main.py"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           cwd=str(ws), env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, f"执行超时（>{timeout}s，可在 core/config.py 调整 SANDBOX_TIMEOUT）", ""
    except Exception as e:
        return False, f"执行异常: {e}", ""

    if r.returncode != 0:
        return False, (f"程序异常退出（退出码 {r.returncode}）:\n"
                       f"{(r.stderr or r.stdout or '')[-1000:]}"), r.stdout

    stdout = r.stdout
    if not assertions:
        if stdout.strip():
            return True, "验收通过：程序正常退出且产生输出", stdout
        return False, "验收不通过：程序退出码为 0，但没有任何输出（可能只定义了函数未调用）", stdout

    fails, passes = [], 0
    for line in assertions:
        m = re.match(r"^(CONTAINS|NOT_CONTAINS|REGEX|FILE_EXISTS)\s*:\s*(.*)$", line, re.I)
        kind, arg = (m.group(1).upper(), m.group(2)) if m else ("CONTAINS", line)
        if kind == "CONTAINS":
            good = arg in stdout
        elif kind == "NOT_CONTAINS":
            good = arg not in stdout
        elif kind == "FILE_EXISTS":
            # 产物断言：文件必须存在于工作区且非空（CSV/JSON/图片等交付物）
            fp = _safe_relpath(arg, ws)
            good = bool(fp and fp.is_file() and fp.stat().st_size > 0)
        else:
            good = bool(re.search(arg, stdout))
        if good:
            passes += 1
        else:
            fails.append(line)
    if not fails:
        return True, f"验收通过：{len(assertions)} 条断言全部满足", stdout
    return False, ("验收不通过，失败断言:\n" + "\n".join(f"  ✗ {f}" for f in fails)
                   + f"\n实际输出（前 800 字）:\n{stdout[:800]}"), stdout


def execute_spec(trace_id, spec_text):
    """引擎统一入口：解析规格 -> 写工作区 -> 装依赖 -> 执行验收。返回 ExecResult"""
    spec = parse_codegen_spec(spec_text)
    if not spec.files:
        return ExecResult(False, "模型返回了空代码，验证锚点拒绝放行", {}, "")
    ws, written, skipped = prepare_workspace(trace_id, spec.files)
    note = f"工作区 workspace/{trace_id}；写入文件: {', '.join(written) or '无'}"
    if skipped:
        note += f"；已跳过非法文件名: {', '.join(skipped)}"
    if not written:
        return ExecResult(False, note + "\n没有可执行文件（文件名全部非法？）", spec.files)

    ok, dep_msg = ensure_venv(trace_id, spec.requirements)
    if not ok:
        return ExecResult(False, note + f"\n依赖安装失败:\n{dep_msg}", spec.files)
    if not dep_msg.startswith(("无第三方依赖", "依赖未变化")):
        note += f"；{dep_msg}"

    before = set(list_workspace_files(trace_id))
    ok, report, stdout = run_workspace(trace_id, spec.assertions)
    # 运行期间新产生的文件 = 本轮任务产物（CSV/JSON/图片等）。
    # 产物清单累积登记（含历史轮次），保证下一轮 prepare_workspace 能全部清干净
    artifacts = sorted(f for f in list_workspace_files(trace_id) if f not in before)
    art_path = ws / ".aegis_artifacts.json"
    try:
        prev = json.loads(art_path.read_text(encoding="utf-8")) if art_path.exists() else []
    except (json.JSONDecodeError, OSError):
        prev = []
    art_path.write_text(json.dumps(sorted(set(prev) | set(artifacts)), ensure_ascii=False), encoding="utf-8")
    if artifacts:
        sizes = ", ".join(f"{a}({_fmt_size((WORKSPACES_DIR / trace_id / a).stat().st_size)})"
                          for a in artifacts)
        report += f"\n本次运行新增产物: {sizes}"
    return ExecResult(ok, note + "\n" + report, spec.files, stdout, artifacts)


def _fmt_size(n):
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


# ---------------- 工具注册表 ----------------
# 工作流里的 tool 节点按名字取用：node.tool -> TOOL_REGISTRY[name]
# 统一签名：fn(trace_id, input_text) -> ExecResult(ok / report / files / stdout)
# 新增工具只需在这里注册，然后在 YAML 里写 tool: 名字，引擎不用改。
TOOL_REGISTRY = {
    "execute_spec": execute_spec,   # 解析规格 -> 写工作区 -> 装依赖 -> 真执行 -> 断言验收
}


def list_tools():
    """给前端/文档用的工具清单"""
    return sorted(TOOL_REGISTRY)


# ---------------- Agent 可调用工具（Function Calling） ----------------
# 与上面的 TOOL_REGISTRY 是两回事，别混：
#   TOOL_REGISTRY  -> 工作流里的 **tool 节点**，由 YAML 指定、引擎无条件执行（如 execute_spec）
#   AGENT_TOOLS    -> 暴露给 **模型本身**，模型在推理中自己决定调哪个、传什么参数，
#                     引擎执行后把观察结果回灌，模型再继续推。
# 这里只放"读代码 / 试跑代码"这类**探查**动作；落盘与验收仍然由 tool 节点的
# execute_spec 统一负责，保证"通过"永远有物理依据，模型不能自己宣布成功。

@dataclass
class ToolContext:
    """工具执行的边界：只能落到本 trace 的工作区里。"""
    trace_id: str
    workspace: Path


TOOL_OUTPUT_LIMIT = 4000


def _clip(text, limit=TOOL_OUTPUT_LIMIT):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"\n…（已截断，共 {len(text)} 字）"


def _tool_list_files(ctx: ToolContext, args: dict) -> str:
    files = list_workspace_files(ctx.trace_id)
    if not files:
        return "（工作区暂无文件）"
    return "工作区文件：\n" + "\n".join(files)


def _tool_read_file(ctx: ToolContext, args: dict) -> str:
    name = str(args.get("path") or "").strip()
    p = _safe_relpath(name, ctx.workspace)
    if p is None:
        return f"错误：非法路径 {name!r}（只允许工作区内的相对路径）"
    if not p.is_file():
        return f"错误：文件不存在 {name}"
    return _clip(p.read_text(encoding="utf-8", errors="replace"))


def _tool_run_python(ctx: ToolContext, args: dict) -> str:
    code = str(args.get("code") or "")
    if not code.strip():
        return "错误：code 为空"
    ws = ctx.workspace
    script = ws / ".aegis_tool_run.py"
    script.write_text(code, encoding="utf-8")
    vpy = _venv_python(ws)
    py = str(vpy) if vpy.exists() else sys.executable
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONPATH", None)
    try:
        r = subprocess.run([py, script.name], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=SANDBOX_TIMEOUT, cwd=str(ws), env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return f"错误：执行超时（>{SANDBOX_TIMEOUT}s）"
    except Exception as e:
        return f"错误：执行异常 {type(e).__name__}: {e}"
    parts = [f"退出码 {r.returncode}"]
    if (r.stdout or "").strip():
        parts.append(r.stdout.rstrip())
    if (r.stderr or "").strip():
        parts.append("[stderr]\n" + r.stderr.rstrip())
    if len(parts) == 1:
        parts.append("(无输出)")
    return _clip("\n".join(parts))


AGENT_TOOLS = {
    "list_files": {
        "description": "列出当前任务工作区里的文件（相对路径），用于了解已有产物。",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "fn": _tool_list_files,
    },
    "read_file": {
        "description": "读取工作区内某个文件的完整内容。路径必须是相对路径。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "工作区内的相对路径，如 main.py"}},
            "required": ["path"],
        },
        "fn": _tool_read_file,
    },
    "run_python": {
        "description": ("在工作区里真实执行一段 Python，返回退出码与 stdout/stderr。"
                        "可用于复现报错、验证片段是否可跑。每次执行有超时限制。"),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "要执行的 Python 源码"}},
            "required": ["code"],
        },
        "fn": _tool_run_python,
    },
}


def tool_schemas(names):
    """把 Agent 声明里的工具名翻译成 OpenAI tools 格式；未注册的名字直接忽略。"""
    out = []
    for name in names or []:
        spec = AGENT_TOOLS.get(str(name))
        if not spec:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        })
    return out


def call_tool(name, ctx: ToolContext, args: dict) -> str:
    """执行一个工具调用。任何异常都转成文本返回给模型，绝不让引擎崩掉。"""
    spec = AGENT_TOOLS.get(str(name))
    if spec is None:
        return f"错误：未注册的工具 {name}（可用: {', '.join(sorted(AGENT_TOOLS))}）"
    if not isinstance(args, dict):
        args = {}
    try:
        return str(spec["fn"](ctx, args))
    except Exception as e:
        return f"错误：工具 {name} 执行异常 {type(e).__name__}: {e}"


def list_agent_tools():
    """给前端/文档用：模型可调用的工具清单"""
    return sorted(AGENT_TOOLS)
