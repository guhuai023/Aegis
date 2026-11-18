"""工具层：结构化规格解析、任务工作区、依赖安装、物理断言式验收。

安全说明：生成的代码仍以当前用户权限运行（不是隔离沙箱）。
生产环境请改用 Docker / 受限进程执行不可信代码。
"""
import contextvars
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from core.config import SANDBOX_TIMEOUT, INSTALL_TIMEOUT, WORKSPACES_DIR

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"   # 入参文件根目录

# 当前正在执行哪个节点（引擎设置，用来把产物归属到节点上）
_RUN_NODE: contextvars.ContextVar = contextvars.ContextVar("aegis_run_node", default=None)

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

    兼容三种形态：完整格式 / 缺少某节 / 完全无标记的纯代码（整体视为 main.py）。
    注意：**断言节先解析**，没有断言的规格由 execute_spec 拒绝执行，
    避免"模型吐了一段说明文字"被当成代码写进工作区（曾经真实发生过）。
    """
    spec = CodeSpec()
    text = (text or "").replace("```python", "").replace("```py", "").replace("```", "").strip()
    if not text:
        return spec

    def _section(header):
        m = re.search(re.escape(header) + r"(.*?)(?=\n===|\Z)", text, re.S)
        return m.group(1).strip() if m else ""

    spec.requirements = [ln.strip() for ln in _section(SEC_REQUIREMENTS).splitlines() if ln.strip()]
    spec.assertions = [_clean_assertion_line(ln)
                       for ln in _section(SEC_ASSERTIONS).splitlines() if ln.strip()]

    # 从正文里剥掉两个非文件节，剩下的才是代码
    body = re.sub(re.escape(SEC_REQUIREMENTS) + r".*?(?=\n===|\Z)", "", text, flags=re.S)
    body = re.sub(re.escape(SEC_ASSERTIONS) + r".*?(?=\n===|\Z)", "", body, flags=re.S)
    body = body.strip()

    if "=== FILE:" in body:
        parts = FILE_MARKER.split(body)  # [前导, 名1, 内容1, 名2, 内容2, ...]
        for i in range(1, len(parts), 2):
            name, content = parts[i].strip(), parts[i + 1].strip()
            if name and content:
                spec.files[name] = content
    elif body:
        spec.files["main.py"] = body
    return spec


def _clean_assertion_line(line):
    """容忍模型把断言写成 markdown 列表（`- CONTAINS: x` / `* CONTAINS: x`）。"""
    return re.sub(r"^\s*[-*+]\s+", "", str(line).strip())


# ---------------- 任务工作区 ----------------

def get_workspace_dir(trace_id) -> Path:
    """每个 trace 一个工作区：workspace/<trace_id>/，代码、依赖、交付物都在这里"""
    ws = WORKSPACES_DIR / trace_id
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def seed_workspace(trace_id, rel_paths, examples_dir=None):
    """把 examples/ 下声明的入参文件复制进 trace 工作区（保留相对路径）。

    真实场景必须有固定输入才谈得上复现：日志、脏数据表、待修脚本都走这里进工作区，
    模型在 context 里看到的就是"工作区里有什么"，不用猜。
    返回 [(相对路径, 字节数)]。
    """
    base = Path(examples_dir or EXAMPLES_DIR).resolve()
    ws = get_workspace_dir(trace_id)
    copied = []
    for rel in rel_paths or []:
        rel = str(rel).replace("\\", "/").strip()
        if not rel:
            continue
        src = (base / rel).resolve()
        if not str(src).startswith(str(base) + os.sep) or not src.is_file():
            raise ValueError(f"入参文件非法或不存在: examples/{rel}")
        dst = ws / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append((rel, dst.stat().st_size))
    return copied


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


_MANIFEST = ".aegis_nodes.json"


def _load_manifests(ws: Path) -> dict:
    path = ws / _MANIFEST
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_manifests(ws: Path, data: dict) -> None:
    try:
        (ws / _MANIFEST).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _current_node():
    """当前正在执行的节点 id（由引擎通过 run_as_node 设置），没有则 None。"""
    ctx = _RUN_NODE.get()
    return ctx[0] if ctx else None


def run_as_node(node_id):
    """引擎在调用 tool 节点前后用它标记"这些产物算哪个节点的"。

    返回 token，调用方用 reset_run_as_node 复位（contextvar，线程安全）。
    """
    return _RUN_NODE.set((str(node_id),))


def reset_run_as_node(token):
    _RUN_NODE.reset(token)


def _clean_node_files(ws: Path, entry: dict) -> None:
    for rel in (entry.get("spec") or []) + (entry.get("artifacts") or []):
        p = _safe_relpath(rel, ws)
        if p and p.is_file():
            p.unlink()


def prepare_workspace(trace_id, files):
    """把规格里的文件写入工作区。返回 (ws, written, skipped)

    产物隔离粒度 = **节点级**（不是轮次级）：
      · 同一个节点重试时，清掉它自己上一轮写下的规格文件与产物
        —— 这样 FILE_EXISTS / REGEX_FILE 只认本轮真实生成的文件，不会被上次残留骗过；
      · 不同节点的产物跨节点保留
        —— 数据管线需要「A 支线清洗 -> 汇合 -> B 节点合并」，不能互相清掉。
    没有节点上下文（直接调用 / 老数据）时退回全量清理语义。
    """
    ws = get_workspace_dir(trace_id)
    manifests = _load_manifests(ws)
    node_id = _current_node()

    if node_id:
        if node_id in manifests:
            _clean_node_files(ws, manifests[node_id])
    else:
        _cleanup_manifest_files(ws, ".aegis_manifest.json")   # 兼容旧版清单
        _cleanup_manifest_files(ws, ".aegis_artifacts.json")
        for entry in manifests.values():
            _clean_node_files(ws, entry)

    written, skipped = [], []
    for name, content in files.items():
        p = _safe_relpath(name, ws)
        if p is None:
            skipped.append(name)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(p.relative_to(ws).as_posix())

    if node_id:
        manifests.setdefault(node_id, {})["spec"] = written
        _save_manifests(ws, manifests)
    else:
        (ws / ".aegis_manifest.json").write_text(json.dumps(written, ensure_ascii=False), encoding="utf-8")
    return ws, written, skipped


def _record_artifacts(ws: Path, artifacts) -> None:
    """登记本轮新增产物，供同一节点下次重试时清理。"""
    node_id = _current_node()
    if not node_id:                       # 直接调用（测试/旧路径）：沿用单文件清单
        art_path = ws / ".aegis_artifacts.json"
        try:
            prev = json.loads(art_path.read_text(encoding="utf-8")) if art_path.exists() else []
        except (json.JSONDecodeError, OSError):
            prev = []
        art_path.write_text(json.dumps(sorted(set(prev) | set(artifacts)), ensure_ascii=False),
                            encoding="utf-8")
        return
    manifests = _load_manifests(ws)
    entry = manifests.setdefault(node_id, {})
    entry["artifacts"] = sorted(set(entry.get("artifacts") or []) | set(artifacts))
    _save_manifests(ws, manifests)


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


MAX_PIP_RETRY = 6        # 整批失败后最多逐个重试的包数，防依赖过多时无限拖长


def _pip_install(vpy: Path, args, env, flags):
    """跑一次 pip install（args 可以是 -r 文件，也可以是单个包名）。返回 (ok, detail)。"""
    cmd = [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "--no-input"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=INSTALL_TIMEOUT,
                           env=env, creationflags=flags)
    except subprocess.TimeoutExpired:
        return False, f"pip 安装超时（>{INSTALL_TIMEOUT}s，可在 core/config.py 调整）"
    except Exception as e:
        return False, f"pip 执行异常: {e}"
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout or "")[-800:]


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
        ok, detail = _pip_install(vpy, ["-r", str(ws / "requirements.txt")], env, flags)
        if not ok:
            # 整批失败时逐个重装：模型偶尔写错包名/版本号，一行非法 requirements
            # 不该把整条 trace 判死，也不该让其它正常依赖一起失败。
            good, bad, bad_detail = [], [], detail
            for pkg in reqs[:MAX_PIP_RETRY]:
                pkg_ok, pkg_detail = _pip_install(vpy, [pkg], env, flags)
                (good if pkg_ok else bad).append(pkg)
                if not pkg_ok:
                    bad_detail = pkg_detail
            if not good:
                return False, f"pip 安装失败（{len(reqs)} 个包）:\n{bad_detail}"
            hash_file.write_text(req_hash, encoding="utf-8")
            msg = f"依赖部分安装成功: {', '.join(good)}"
            if bad:
                msg += f"；以下包失败已跳过: {', '.join(bad)}"
            return True, msg
        hash_file.write_text(req_hash, encoding="utf-8")
        return True, f"依赖安装完成: {', '.join(reqs)}"
    except subprocess.TimeoutExpired:
        return False, f"依赖安装超时（>{INSTALL_TIMEOUT}s，可在 core/config.py 调整）"
    except Exception as e:
        return False, f"依赖安装异常: {e}"


# ---------------- 物理验收执行 ----------------

# 断言词汇表：验收能力的边界就是平台的适用边界（能写出的断言 = 能接的场景）。
# 新增一类断言只需要在这里登记 + 在 _check_assertion 里实现，工作流不用改。
ASSERTION_KINDS = {
    "CONTAINS": "输出包含该文本",
    "NOT_CONTAINS": "输出不包含该文本",
    "REGEX": "输出匹配该正则（按行匹配，^ $ 作用于每一行）",
    "FILE_EXISTS": "工作区里存在该文件且非空",
    "FILE_MIN_SIZE": "FILE_MIN_SIZE: 文件名, 字节数 —— 产物大小下限（防只有表头的空 CSV）",
    "REGEX_FILE": "REGEX_FILE: 文件名 :: 正则 —— 直接校验收产物内容（按行匹配）",
    "EXIT_CODE": "EXIT_CODE: 0 —— 校验进程退出码（不写这条时非 0 直接判失败）",
    "NEAR": "NEAR: 期望值, 容差 —— 输出中任一数值落在容差内（科研/统计类任务）",
}

_ASSERT_RE = re.compile(r"^([A-Za-z_]{2,})\s*:\s*(.*)$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _parse_assertion(line):
    """把一行断言解析成 (kind, arg)。

    兼容三种写法：
      标准   CONTAINS: 9
      拼错   CONTANIS: 9      -> ("INVALID", ...) 明确判失败，不静默降级成 CONTAINS
      旧写法 总金额: 100       -> 中文标签按 CONTAINS 整行匹配（向后兼容）
    """
    text = (line or "").strip()
    m = _ASSERT_RE.match(text)
    if not m:
        return "CONTAINS", text
    kind, arg = m.group(1).upper(), m.group(2).strip()
    if kind in ASSERTION_KINDS:
        return kind, arg
    if re.fullmatch(r"[A-Z][A-Z_]+", m.group(1)):
        return "INVALID", text
    return "CONTAINS", text


def _check_assertion(kind, arg, stdout, ws, returncode):
    """校验单条断言，返回 (是否通过, 失败细节)。"""
    if kind == "CONTAINS":
        return arg in stdout, None
    if kind == "NOT_CONTAINS":
        return arg not in stdout, None
    if kind == "REGEX":
        try:
            # 按行匹配：文本/CSV 场景里 ^ $ 想表达的几乎总是"某一行"
            return bool(re.search(arg, stdout, re.MULTILINE)), None
        except re.error as e:
            return False, f"正则非法: {e}"
    if kind == "FILE_EXISTS":
        fp = _safe_relpath(arg, ws)
        return bool(fp and fp.is_file() and fp.stat().st_size > 0), None
    if kind == "FILE_MIN_SIZE":
        name, _, size = arg.partition(",")
        try:
            need = int(size.strip())
        except ValueError:
            return False, "格式应为 FILE_MIN_SIZE: 文件名, 字节数"
        fp = _safe_relpath(name.strip(), ws)
        if not (fp and fp.is_file()):
            return False, f"文件不存在: {name.strip()}"
        got = fp.stat().st_size
        return got >= need, (f"实际 {got} B < 要求 {need} B" if got < need else None)
    if kind == "REGEX_FILE":
        name, sep, pattern = arg.partition("::")
        if not sep:
            return False, "格式应为 REGEX_FILE: 文件名 :: 正则"
        fp = _safe_relpath(name.strip(), ws)
        if not (fp and fp.is_file()):
            return False, f"文件不存在: {name.strip()}"
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return False, f"读取失败: {e}"
        try:
            return bool(re.search(pattern.strip(), content, re.MULTILINE)), None
        except re.error as e:
            return False, f"正则非法: {e}"
    if kind == "EXIT_CODE":
        try:
            want = int(arg)
        except ValueError:
            return False, "格式应为 EXIT_CODE: 0"
        return returncode == want, (f"实际退出码 {returncode}" if returncode != want else None)
    if kind == "NEAR":
        want_s, _, tol_s = arg.partition(",")
        try:
            want = float(want_s.strip())
            tol = abs(float(tol_s.strip())) if tol_s.strip() else 1e-6
        except ValueError:
            return False, "格式应为 NEAR: 期望值, 容差"
        nums = [float(x) for x in _NUM_RE.findall(stdout)]
        if any(abs(n - want) <= tol for n in nums):
            return True, None
        if not nums:
            return False, f"输出里没有数值，期望 {want}±{tol}"
        nearest = min(nums, key=lambda n: abs(n - want))
        return False, f"没有任何数值落在 {want}±{tol}，最接近的是 {nearest}"
    if kind == "INVALID":
        return False, "未知断言类型（拼写错误？可用: " + ", ".join(sorted(ASSERTION_KINDS)) + "）"
    return False, f"未知断言类型: {kind}"


def run_workspace(trace_id, assertions, timeout=None):
    """在工作区执行 main.py 并做断言式验收，返回 (ok, report, stdout)。

    断言每行一条，类型见 ASSERTION_KINDS。
    无断言时退回默认验收：退出码 0 且产生非空输出。
    只有显式写了 EXIT_CODE 断言，非 0 退出才不直接判失败。
    """
    ws = WORKSPACES_DIR / trace_id
    if not (ws / "main.py").exists():
        return False, "入口文件 main.py 不存在", ""
    timeout = timeout or SANDBOX_TIMEOUT
    vpy = _venv_python(ws)
    py = str(vpy) if vpy.exists() else sys.executable   # 没有 venv 就用当前解释器，避免依赖 PATH 上的 python
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

    parsed = [_parse_assertion(line) for line in (assertions or [])]
    has_exit_assert = any(kind == "EXIT_CODE" for kind, _ in parsed)

    if r.returncode != 0 and not has_exit_assert:
        return False, (f"程序异常退出（退出码 {r.returncode}）:\n"
                       f"{(r.stderr or r.stdout or '')[-1000:]}"), r.stdout

    stdout = r.stdout
    if not assertions:
        if stdout.strip():
            return True, "验收通过：程序正常退出且产生输出", stdout
        return False, "验收不通过：程序退出码为 0，但没有任何输出（可能只定义了函数未调用）", stdout

    fails, kinds_hit = [], {}
    for kind, arg in parsed:
        ok, detail = _check_assertion(kind, arg, stdout, ws, r.returncode)
        if ok:
            kinds_hit[kind] = kinds_hit.get(kind, 0) + 1
        else:
            fails.append(f"{kind}: {arg}" + (f"（{detail}）" if detail else ""))
    if not fails:
        breakdown = "，".join(f"{k}×{v}" for k, v in sorted(kinds_hit.items()))
        return True, f"验收通过：{len(parsed)} 条断言全部满足（{breakdown}）", stdout
    tail = f"\n实际输出（前 800 字）:\n{stdout[:800]}" if stdout.strip() else "\n（程序没有任何输出）"
    return False, ("验收不通过，失败断言:\n" + "\n".join(f"  ✗ {f}" for f in fails) + tail), stdout


def execute_spec(trace_id, spec_text):
    """引擎统一入口：解析规格 -> 写工作区 -> 装依赖 -> 执行验收。返回 ExecResult"""
    spec = parse_codegen_spec(spec_text)
    # 先卡格式：没有断言的规格无法验收，绝不能把散文/说明文字当代码写进工作区
    if not spec.assertions:
        return ExecResult(
            False,
            "规格缺少 === ASSERTIONS === 段（或该段为空）：没有可验收断言就拒绝执行。"
            "请按 REQUIREMENTS / ASSERTIONS / FILE 三段结构重新给出规格。",
            {}, "")
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
    # 运行期间新产生的文件 = 本轮任务产物（CSV/JSON/图片等），按节点登记，
    # 同一节点重试时只清自己的产物，不同节点的产物跨节点保留（数据管线要靠它传文件）
    artifacts = sorted(f for f in list_workspace_files(trace_id) if f not in before)
    _record_artifacts(ws, artifacts)
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
