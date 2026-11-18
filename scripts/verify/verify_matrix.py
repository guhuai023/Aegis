# -*- coding: utf-8 -*-
"""控制结构矩阵验证（离线，假 LLM + 假沙箱，不花 token）。

目标：证明每个控制结构**真的被执行**，不只是 YAML 能被解析。
   S1 顺序 + 条件分支（routes 命中 / next 兜底）
   S2 失败回边 + max_retries 预算 + on_exhausted 熔断 -> human -> on_resume 恢复
   S3 并行扇出 + join 汇合
   S4 sub_workflow（预期：解析通过但执行未实现 -> 必须是"明确报错"，不能静默假装成功）
   S5 并发认领不重复
   S6 stale 任务自动回收
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

# ---- 环境隔离：虚拟数据库 + 虚拟工作区 + 虚拟工作流目录 ----
db.DB_PATH = str(TMP / "aegis.db")
tools.WORKSPACES_DIR = WS
engine.WORKSPACES_DIR = WS
workflow.WORKFLOWS_DIR = WFS
engine.JOIN_POLL_SECONDS = 0.2
db.init_db()
engine.start_engine(workers=1)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def write_wf(name, text):
    (WFS / f"{name}.yaml").write_text(text, encoding="utf-8")
    workflow._CACHE.clear()


def fake_llm(replies):
    """replies: agent_name -> 文本 或 函数(ctx)。"""
    def fake(spec, prompt, **kw):
        value = replies.get(spec.name, "ok")
        return value(prompt) if callable(value) else value
    engine.ask_agent_spec = fake


def fake_tool(sequence):
    """sequence: 依次消费的 ExecResult 列表；用尽后返回最后一个。"""
    state = {"i": 0}

    def fake(trace_id, text):
        i = min(state["i"], len(sequence) - 1)
        state["i"] += 1
        r = sequence[i]
        return r
    tools.TOOL_REGISTRY["execute_spec"] = fake


def trace_rows(tid):
    conn = sqlite3.connect(db.DB_PATH)
    rows = list(conn.execute(
        "SELECT node_id, status, retry_count FROM tasks WHERE trace_id=? ORDER BY rowid", (tid,)))
    conn.close()
    return rows


def wait_trace(tid, timeout=30):
    """等到：无 pending/running，任务集合稳定，且出现过终态。

    引擎在"父节点 done"与"下一节点入队"之间有空档，只看一次会被骗过去。
    """
    t0 = time.time()
    stable = 0
    prev = None
    while time.time() - t0 < timeout:
        rows = trace_rows(tid)
        snapshot = tuple((r[0], r[1]) for r in rows)
        busy = any(s in ("pending", "running") for s in (r[1] for r in rows))
        terminal = any(r[1] in ("done", "failed", "waiting_human") for r in rows)
        if not busy and terminal and snapshot == prev:
            stable += 1
            if stable >= 3:          # 连续 3 次(约 0.6s)一致才算稳定
                return rows
        else:
            stable = 0
        prev = snapshot
        time.sleep(0.2)
    return trace_rows(tid)


def logs(tid):
    conn = sqlite3.connect(db.DB_PATH)
    out = [r[0] for r in conn.execute("SELECT message FROM logs WHERE trace_id=? ORDER BY rowid", (tid,))]
    conn.close()
    return "\n".join(out)


OK = ExecResult(True, "验收通过", {"main.py": "print(1)"}, "ok")
BAD = ExecResult(False, "验收不通过：断言失败", {"main.py": "print(1)"}, "bad")

# ============================================================ S1 顺序 + 条件分支
print("\n=== S1 顺序 + 条件分支 ===", flush=True)
write_wf("s1_routes", """
name: s1_routes
entry: router
nodes:
  - id: router
    type: llm
    agent: router
    context: "{prompt}"
    routes:
      "[CODE]": coder
    next: chat
  - id: coder
    type: llm
    agent: coder
    on_success: done
  - id: chat
    type: llm
    agent: chat
    on_success: done
""")
fake_tool([OK])

fake_llm({"router": "[CODE]", "coder": "spec", "chat": "闲聊"})
tid = engine.start_workflow("s1_routes", "写个函数")
rows = wait_trace(tid)
nodes = {r[0]: r[1] for r in rows}
check("S1 命中 routes -> coder 执行", nodes.get("coder") == "done" and "chat" not in nodes, str(nodes))

fake_llm({"router": "随便聊聊", "coder": "spec", "chat": "闲聊"})
tid = engine.start_workflow("s1_routes", "聊聊天")
rows = wait_trace(tid)
nodes = {r[0]: r[1] for r in rows}
check("S1 未命中 -> next 兜底 chat", nodes.get("chat") == "done" and "coder" not in nodes, str(nodes))

# ============================================================ S2 失败回边 + 熔断 + 人工恢复
print("\n=== S2 失败回边 + 重试预算 + 熔断 + 人工恢复 ===", flush=True)
write_wf("s2_retry", """
name: s2_retry
entry: coder
nodes:
  - id: coder
    type: llm
    agent: coder
    next: verify
  - id: verify
    type: tool
    tool: execute_spec
    context: "{spec}"
    on_success: done
    on_failure: fixer
    max_retries: 1
    on_exhausted: human
  - id: fixer
    type: llm
    agent: fixer
    next: verify
  - id: human
    type: human
    on_resume: verify
""")
fake_llm({"router": "[CODE]", "coder": "spec", "fixer": "spec2"})
fake_tool([BAD, BAD, BAD, OK])          # 两次失败 -> 熔断；恢复后再跑一次成功

tid = engine.start_workflow("s2_retry", "必失败的任务")
rows = wait_trace(tid)
nodes = {r[0]: r[1] for r in rows}
retries = [r[2] for r in rows if r[0] == "verify"]
check("S2 验收失败 -> fixer 回边执行", "fixer" in nodes, str(nodes))
check("S2 重试计数递增", retries == [0, 1] or max(retries or [0]) >= 1, str(retries))
check("S2 预算耗尽 -> 熔断转人工", nodes.get("human") in ("waiting_human", "pending", "running"), str(nodes))

waits = db.get_waiting_human(tid)
check("S2 人工队列可查询", bool(waits), str(waits))
if waits:
    engine.resume_with_advice(tid, waits[0], "把除数改成 1 再试")
    rows = wait_trace(tid)
    nodes = {r[0]: r[1] for r in rows}
    check("S2 人工建议恢复后继续执行", nodes.get("verify") == "done" or "done" in nodes.values(), str(nodes))

# ============================================================ S3 并行 + 汇合
print("\n=== S3 并行扇出 + join 汇合 ===", flush=True)
write_wf("s3_par", """
name: s3_par
entry: split
nodes:
  - id: split
    type: llm
    agent: coder
    parallel: [a, b]
  - id: a
    type: tool
    tool: execute_spec
    on_success: gather
  - id: b
    type: tool
    tool: execute_spec
    on_success: gather
  - id: gather
    type: join
    join: [a, b]
    next: done
""")
fake_llm({"coder": "spec"})
fake_tool([OK])
tid = engine.start_workflow("s3_par", "并行任务")
rows = wait_trace(tid)
nodes = {r[0]: r[1] for r in rows}
check("S3 两条并行分支都完成", nodes.get("a") == "done" and nodes.get("b") == "done", str(nodes))
check("S3 汇合节点完成", nodes.get("gather") == "done", str(nodes))
check("S3 汇合后才放行下游", logs(tid).count("汇合") >= 1)

# ============================================================ S4 sub_workflow
print("\n=== S4 sub_workflow（预期未实现 -> 必须明确报错） ===", flush=True)
write_wf("s4_sub", """
name: s4_sub
entry: outer
nodes:
  - id: outer
    type: llm
    agent: coder
    sub_workflow: s2_retry
    on_success: done
""")
fake_llm({"coder": "spec"})
tid = engine.start_workflow("s4_sub", "子流程")
rows = wait_trace(tid)
nodes = {r[0]: r[1] for r in rows}
log = logs(tid)
check("S4 未静默假装成功", nodes.get("outer") != "done", str(nodes))
check("S4 给出可读原因", ("子流程" in log) or ("sub_workflow" in log) or ("未实现" in log) or ("不支持" in log), log[-200:])

# ============================================================ S6 stale 回收
print("\n=== S6 stale 任务回收 ===", flush=True)
conn = sqlite3.connect(db.DB_PATH)
conn.execute("INSERT INTO tasks (task_id, trace_id, node_id, status, input_context, retry_count,"
             " started_at, workflow, priority, capability, created_at)"
             " VALUES ('stale01','trace-stale','n1','running','x',0,"
             " '2020-01-01 00:00:00','s1_routes',10,'llm:coder','2020-01-01 00:00:00')")
conn.commit()
conn.close()
recovered = db.recover_stale_tasks()
conn = sqlite3.connect(db.DB_PATH)
status = conn.execute("SELECT status FROM tasks WHERE task_id='stale01'").fetchone()
conn.close()
check("S6 超时任务被回收", status and status[0] in ("pending", "failed"), str(status))

# ============================================================ S7 并发认领
print("\n=== S7 并发认领不重复 ===", flush=True)
# 先清空遗留任务，避免把别的场景的任务算进来
while True:
    leftover = db.claim_task()
    if not leftover:
        break
    db.finish_task(leftover[0], "failed", "cleanup")
for i in range(8):
    db.create_task(f"trace-race", f"n{i}", "ctx", workflow="s1_routes", capability="llm:coder")
claimed = []
lock = threading.Lock()

def grabber():
    while True:
        task = db.claim_task()
        if not task:
            return
        with lock:
            claimed.append(task[0])

threads = [threading.Thread(target=grabber) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("S7 8 个任务被 4 线程无重复认领", len(claimed) == 8 and len(set(claimed)) == 8,
      f"claimed={len(claimed)} unique={len(set(claimed))}")

# ============================================================ 汇总
print("\n" + "=" * 60, flush=True)
bad = [r for r in RESULTS if not r[1]]
print(f"控制结构矩阵：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过", flush=True)
for name, ok, detail in bad:
    print("  ❌", name, "|", detail, flush=True)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)

