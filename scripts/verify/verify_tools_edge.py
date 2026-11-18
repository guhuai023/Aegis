# -*- coding: utf-8 -*-
"""工具层边界验证（真实沙箱，不假造）。

T1 沙箱超时：死循环任务必须被 SANDBOX_TIMEOUT 掐断，而不是挂住
T2 依赖安装：声明第三方依赖时会建 venv 并安装（无网络则如实报"安装失败"，不能假装成功）
T3 路径逃逸：产物断言 / Agent 工具都不允许跳出工作区
T4 Agent 工具：list_files / read_file / run_python 真能读到工作区文件
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import shutil
import sys
import tempfile
import time
from pathlib import Path

from core import tools

TMP = Path(tempfile.mkdtemp())
WS = TMP / "workspace"
WS.mkdir(parents=True)
tools.WORKSPACES_DIR = WS
tid = "edge01"
(WS / tid).mkdir()

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def run_main(code, assertions, timeout=None):
    tok = tools.run_as_node("edge_node")
    try:
        (WS / tid / "main.py").write_text(code, encoding="utf-8")
        return tools.run_workspace(tid, assertions, timeout=timeout)
    finally:
        tools.reset_run_as_node(tok)


print("\n=== T1 沙箱超时 ===", flush=True)
t0 = time.time()
ok, report, _ = run_main("while True:\n    pass\n", [], timeout=3)
elapsed = time.time() - t0
check("死循环被超时掐断", not ok and "超时" in report, report[:120])
check("超时耗时接近上限（没挂住）", elapsed < 15, f"{elapsed:.1f}s")

print("\n=== T3 路径逃逸 ===", flush=True)
tok = tools.run_as_node("edge_node")
try:
    (WS / tid / "main.py").write_text("print('ok')", encoding="utf-8")
    ok, report, _ = tools.run_workspace(tid, ["FILE_EXISTS: ../outside.txt"])
    check("产物断言拒绝跳出工作区", not ok, report[:100])
    ok2, report2, _ = tools.run_workspace(tid, [r"REGEX_FILE: ../../aegis_platform.db :: ."])
    check("REGEX_FILE 拒绝跳出工作区", not ok2, report2[:100])
finally:
    tools.reset_run_as_node(tok)

print("\n=== T4 Agent 工具 ===", flush=True)
ws = tools.get_workspace_dir(tid)
(ws / "data.csv").write_text("日期,渠道,金额\n2024-01-01,线上,1200\n", encoding="utf-8")
ctx = tools.ToolContext(trace_id=tid, workspace=ws)

obs = tools.call_tool("list_files", ctx, {})
check("list_files 能看到工作区文件", "data.csv" in obs, obs[:160])

obs = tools.call_tool("read_file", ctx, {"path": "data.csv"})
check("read_file 能读到内容", "线上" in obs, obs[:160])

obs = tools.call_tool("read_file", ctx, {"path": "../../etc/passwd"})
check("read_file 拒绝路径逃逸", ("拒绝" in obs) or ("非法" in obs) or ("不允许" in obs) or ("错误" in obs), obs[:160])

obs = tools.call_tool("run_python", ctx, {"code": "print(6*7)"})
check("run_python 真能执行代码", "42" in obs, obs[:160])

obs = tools.call_tool("run_python", ctx, {"code": "import os; print(open('data.csv').read())"})
check("run_python 在 trace 工作区内执行", "线上" in obs, obs[:160])

print("\n=== T2 依赖安装（需要网络） ===", flush=True)
ok, msg = tools.ensure_venv(tid, ["requests"])
print("     ensure_venv ->", ok, "|", msg[:120], flush=True)
if ok:
    vpy = tools._venv_python(ws)
    check("venv 已创建", vpy.exists(), str(vpy))
    ok2, report2, _ = run_main("import requests; print('requests', requests.__version__)", ["CONTAINS: requests"])
    check("第三方依赖在沙箱里可用", ok2, report2[:200])
else:
    print("     ⚠️ 依赖安装失败（多为无网络），如实上报不假装成功", flush=True)
    check("安装失败时给出可读原因（不静默通过）", "失败" in msg or "超时" in msg, msg[:120])

print("\n" + "=" * 60, flush=True)
bad = [r for r in RESULTS if not r[1]]
print(f"工具边界验证：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过", flush=True)
for name, ok, detail in bad:
    print("  ❌", name, "|", detail, flush=True)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)
