# -*- coding: utf-8 -*-
"""调度器真机验证：2 个 Worker + 能力分区（离线，假 LLM / 假沙箱）。

要证明的：
  W1 两个 Worker 各自只认领自己能力范围内的任务
  W2 WORKER_STATE 能如实反映"谁认领了什么"
  W3 scheduler_snapshot 能给出积压与等待
  W4 llm 并发闸门生效（同时最多 MAX_LLM_CONCURRENCY 个 llm 节点在跑）
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from core import db, engine, tools, workflow
from core.tools import ExecResult

TMP = Path(tempfile.mkdtemp())
WFS = TMP / "workflows"
WFS.mkdir(parents=True)
WS = TMP / "workspace"
WS.mkdir(parents=True)

db.DB_PATH = str(TMP / "aegis.db")
tools.WORKSPACES_DIR = WS
engine.WORKSPACES_DIR = WS
workflow.WORKFLOWS_DIR = WFS
engine.JOIN_POLL_SECONDS = 0.2
db.init_db()

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


WFS.joinpath("w_two.yaml").write_text("""
name: w_two
entry: coder
nodes:
  - id: coder
    type: llm
    agent: coder
    next: verify
  - id: verify
    type: tool
    tool: execute_spec
    on_success: done
""", encoding="utf-8")
workflow._CACHE.clear()

# 假 LLM：记录并发峰值
ACTIVE = {"now": 0, "peak": 0}
LOCK = threading.Lock()


def fake_llm(spec, prompt, **kw):
    with LOCK:
        ACTIVE["now"] += 1
        ACTIVE["peak"] = max(ACTIVE["peak"], ACTIVE["now"])
    time.sleep(0.4)
    with LOCK:
        ACTIVE["now"] -= 1
    return "spec"


engine.ask_agent_spec = fake_llm
tools.TOOL_REGISTRY["execute_spec"] = lambda tid, text: ExecResult(True, "ok", {"main.py": "print(1)"}, "ok")

# ---- 起两个能力分区的 Worker ----
CAPS_A = {"llm:coder"}
CAPS_B = {"tool:execute_spec"}
engine.start_engine(workers=2, capabilities=[CAPS_A, CAPS_B])
time.sleep(0.5)

print("\n=== W1 Worker 能力分区 ===", flush=True)
state = engine.WORKER_STATE
check("起了 2 个 Worker", len(state) == 2, str(list(state)))
caps = {name: set(info.get("caps") or []) for name, info in state.items()}
check("Worker 能力分区正确", {frozenset(v) for v in caps.values()} == {frozenset(CAPS_A), frozenset(CAPS_B)}, str(caps))

# ---- 跑 3 条工作流（每条都需要 llm + tool 两类任务） ----
tids = [engine.start_workflow("w_two", f"任务{i}") for i in range(3)]

print("\n=== W2/W3 认领与快照 ===", flush=True)
deadline = time.time() + 30
while time.time() < deadline:
    conn = sqlite3.connect(db.DB_PATH)
    busy = conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('pending','running')").fetchone()[0]
    conn.close()
    if busy == 0:
        break
    time.sleep(0.2)

conn = sqlite3.connect(db.DB_PATH)
rows = conn.execute("SELECT node_id, status FROM tasks ORDER BY rowid").fetchall()
conn.close()
by_node = {}
for node, status in rows:
    by_node.setdefault(node, []).append(status)
check("coder 节点全部完成", all(s == "done" for s in by_node.get("coder", [])), str(by_node))
check("verify 节点全部完成", all(s == "done" for s in by_node.get("verify", [])), str(by_node))
check("llm 与 tool 任务都被执行（跨 Worker 协作）",
      len(by_node.get("coder", [])) == 3 and len(by_node.get("verify", [])) == 3, str(by_node))

snap = db.scheduler_snapshot()
check("scheduler_snapshot 结构完整", isinstance(snap, dict) and "running" in snap and "buckets" in snap, str(snap))

claimed = sum(int(info.get("claimed", 0)) for info in state.values())
check("WORKER_STATE 累计认领 6 个任务（3 coder + 3 verify）", claimed == 6, f"claimed={claimed}")
print(f"     并发峰值 LLM 节点数 = {ACTIVE['peak']}", flush=True)

print("\n=== W4 llm 并发闸门 ===", flush=True)
# 直接并发压 6 个 llm 节点，峰值不得超过 MAX_LLM_CONCURRENCY
from core.config import MAX_LLM_CONCURRENCY
ACTIVE["peak"] = 0
gate = engine._LLM_SEM


def one_llm():
    with gate:
        with LOCK:
            ACTIVE["now"] += 1
            ACTIVE["peak"] = max(ACTIVE["peak"], ACTIVE["now"])
        time.sleep(0.2)
        with LOCK:
            ACTIVE["now"] -= 1


ts = [threading.Thread(target=one_llm) for _ in range(6)]
t0 = time.time()
for t in ts:
    t.start()
for t in ts:
    t.join()
elapsed = time.time() - t0
check(f"并发的 llm 节点数不超过 {MAX_LLM_CONCURRENCY}", ACTIVE["peak"] <= MAX_LLM_CONCURRENCY,
      f"peak={ACTIVE['peak']}")
check("闸门确实产生了排队（耗时 > 单批）", elapsed >= 0.35, f"{elapsed:.2f}s")

print("\n" + "=" * 60, flush=True)
bad = [r for r in RESULTS if not r[1]]
print(f"调度器验证：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过", flush=True)
for name, ok, detail in bad:
    print("  ❌", name, "|", detail, flush=True)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)

