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


def prepare_workspace(trace_id, files):
    """把规格里的文件写入工作区（先按清单清掉上一轮的旧文件），返回 (ws, written, skipped)"""
    ws = get_workspace_dir(trace_id)
    manifest_path = ws / ".aegis_manifest.json"
    if manifest_path.exists():
        try:
            for old in json.loads(manifest_path.read_text(encoding="utf-8")):
                p = ws / old
                if p.is_file():
                    p.unlink()
        except (json.JSONDecodeError, OSError):
            pass
    written, skipped = [], []
    for name, content in files.items():
        p = _safe_relpath(name, ws)
        if p is None:
            skipped.append(name)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(p.relative_to(ws).as_posix())
    manifest_path.write_text(json.dumps(written, ensure_ascii=False), encoding="utf-8")
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
        m = re.match(r"^(CONTAINS|NOT_CONTAINS|REGEX)\s*:\s*(.*)$", line, re.I)
        kind, arg = (m.group(1).upper(), m.group(2)) if m else ("CONTAINS", line)
        if kind == "CONTAINS":
            good = arg in stdout
        elif kind == "NOT_CONTAINS":
            good = arg not in stdout
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

    ok, report, stdout = run_workspace(trace_id, spec.assertions)
    return ExecResult(ok, note + "\n" + report, spec.files, stdout)


# ==========================================
# 🌟 亮点预留：未来的 MCP / SAG-Wiki 扩展接口
# ==========================================
def search_local_knowledge(query):
    """模拟接入外挂知识库（RAG/MCP）"""
    return True, f"【知识库检索结果】：关于 '{query}' 的参考内容..."
