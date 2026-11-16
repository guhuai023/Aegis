"""离线测试：不联网、不调大模型、不执行真实代码。

做法：把 ask_agent_spec 和 TOOL_REGISTRY["execute_spec"] 换成假实现，
然后直接驱动 core.engine._step() 一步步行进，断言编排行为。

覆盖：
    · 工作流定义校验（正常 / 悬空节点 / 不可达节点）
    · Agent 注册表
    · 转移规则（顺序 / 重试预算 / 兜底）
    · code_gen 成功路径
    · code_gen 失败-修复-成功（循环回边）
    · 修复预算耗尽 -> 熔断 -> 人工建议 -> 恢复
    · 大模型异常按 on_failure 转移（不再静默判死）
    · parallel_demo 并行扇出 + join 汇合去重
    · join 状态机（wait / done / already / failed）
    · 僵尸任务自动续跑
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from core import db, engine, tools, workflow
from core.agents import get_agent, list_agents
from core.tools import ExecResult
from core.workflow import WorkflowError, plan_transitions, render_context

SPEC_TEXT = """=== REQUIREMENTS ===
=== ASSERTIONS ===
CONTAINS: ok
=== FILE: main.py ===
print("ok")
"""


def _ok(trace_id="", ctx=""):
    return ExecResult(True, "验收通过：1 条断言全部满足", {"main.py": "print('ok')"}, "ok", [])


def _fail(trace_id="", ctx=""):
    return ExecResult(False, "验收不通过，失败断言:\n  ✗ CONTAINS: ok",
                      {"main.py": "print('x')"}, "x", [])


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aegis-test-"))
        self._orig = (db.DB_PATH, tools.WORKSPACES_DIR, engine.WORKSPACES_DIR,
                      engine.JOIN_POLL_SECONDS)
        db.DB_PATH = str(self.tmp / "aegis.db")
        db.init_db()
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        tools.WORKSPACES_DIR = self.ws
        engine.WORKSPACES_DIR = self.ws
        engine.JOIN_POLL_SECONDS = 1
        self.llm_calls = []
        self._patch_llm(lambda name, ctx: "")
        self._patch_tool(_ok)

    def tearDown(self):
        db.DB_PATH, tools.WORKSPACES_DIR, engine.WORKSPACES_DIR, engine.JOIN_POLL_SECONDS = self._orig
        tools.TOOL_REGISTRY["execute_spec"] = tools.execute_spec
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------- 打桩 ----------------
    def _patch_llm(self, fn):
        def fake(spec, prompt, **kw):        # 引擎现在会额外传 ctx / on_tool / on_usage
            self.llm_calls.append((spec.name, prompt))
            return fn(spec.name, prompt)
        engine.ask_agent_spec = fake

    def _patch_tool(self, fn):
        tools.TOOL_REGISTRY["execute_spec"] = fn

    # ---------------- 驱动 ----------------
    def _pending(self, trace_id):
        conn = db.get_conn()
        n = conn.execute("SELECT COUNT(*) FROM tasks WHERE trace_id=? AND status='pending'",
                         (trace_id,)).fetchone()[0]
        conn.close()
        return n

    def _freeze_ready_at(self, trace_id):
        """把延迟任务的时间锁提前放开，测试里不必真等 2 秒"""
        conn = db.get_conn()
        conn.execute("UPDATE tasks SET ready_at=NULL WHERE trace_id=? AND status='pending'",
                     (trace_id,))
        conn.commit()
        conn.close()

    def drain(self, trace_id, limit=60):
        for _ in range(limit):
            if not self._pending(trace_id):
                return
            self._freeze_ready_at(trace_id)
            engine._step()
        self.fail(f"drain 超过 {limit} 步，工作流可能在死循环")

    def tasks(self, trace_id):
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT node_id, status, retry_count FROM tasks WHERE trace_id=? ORDER BY rowid",
            (trace_id,)).fetchall()
        conn.close()
        return rows

    def nodes(self, trace_id):
        return [r[0] for r in self.tasks(trace_id)]


# ---------------------------------------------------------------- 定义层

class TestWorkflowDefinition(unittest.TestCase):
    def test_code_gen_valid(self):
        spec = workflow.load_workflow("code_gen")
        self.assertEqual(spec.entry, "router")
        self.assertEqual(set(spec.nodes), {"router", "coder", "verify", "fixer", "chat_reply", "human"})
        self.assertEqual(spec.nodes["verify"].max_retries, 2)
        self.assertEqual(spec.nodes["verify"].on_exhausted, "human")
        self.assertEqual(spec.nodes["human"].on_resume, "fixer")
        self.assertEqual(list(spec.nodes["router"].routes), ["[CODE]"])
        self.assertEqual(spec.nodes["coder"].publish, "spec")

    def test_parallel_demo_valid(self):
        spec = workflow.load_workflow("parallel_demo")
        self.assertEqual(spec.nodes["split"].parallel, ["branch_a", "branch_b"])
        self.assertEqual(spec.nodes["merge"].join, ["branch_a", "branch_b"])
        self.assertEqual(spec.nodes["merge"].type, "join")

    def test_all_definitions_load(self):
        specs = workflow.list_workflows()
        self.assertIn("code_gen", specs)
        self.assertIn("parallel_demo", specs)

    def test_dangling_target_rejected(self):
        p = Path(tempfile.mkdtemp()) / "bad.yaml"
        p.write_text(yaml.safe_dump({
            "name": "bad", "entry": "a",
            "nodes": [{"id": "a", "type": "llm", "agent": "chat", "next": "nope"}],
        }), encoding="utf-8")
        with self.assertRaises(WorkflowError) as cm:
            workflow.load_workflow(p)
        self.assertIn("不存在的节点", str(cm.exception))
        shutil.rmtree(p.parent, ignore_errors=True)

    def test_unreachable_node_rejected(self):
        p = Path(tempfile.mkdtemp()) / "bad2.yaml"
        p.write_text(yaml.safe_dump({
            "name": "bad2", "entry": "a",
            "nodes": [
                {"id": "a", "type": "llm", "agent": "chat"},
                {"id": "island", "type": "llm", "agent": "chat"},
            ],
        }), encoding="utf-8")
        with self.assertRaises(WorkflowError) as cm:
            workflow.load_workflow(p)
        self.assertIn("不可达", str(cm.exception))
        shutil.rmtree(p.parent, ignore_errors=True)

    def test_budget_requires_on_failure(self):
        p = Path(tempfile.mkdtemp()) / "bad3.yaml"
        p.write_text(yaml.safe_dump({
            "name": "bad3", "entry": "a",
            "nodes": [{"id": "a", "type": "llm", "agent": "chat", "max_retries": 2,
                       "on_exhausted": "done"}],
        }), encoding="utf-8")
        with self.assertRaises(WorkflowError) as cm:
            workflow.load_workflow(p)
        self.assertIn("on_failure", str(cm.exception))
        shutil.rmtree(p.parent, ignore_errors=True)


class TestAgents(unittest.TestCase):
    def test_registry_loaded(self):
        names = {a["name"] for a in list_agents()}
        for expected in ("router", "coder", "fixer", "chat"):
            self.assertIn(expected, names)

    def test_agent_spec_fields(self):
        coder = get_agent("coder")
        self.assertTrue(coder.role_prompt.startswith("你是资深Python开发专家"))
        self.assertEqual(coder.memory, "trace")
        self.assertEqual(coder.version, 1)
        self.assertNotIn("role_prompt", coder.public())  # 对外只暴露能力画像

    def test_unknown_agent_raises(self):
        with self.assertRaises(Exception):
            get_agent("does_not_exist")


class TestTransitions(unittest.TestCase):
    def setUp(self):
        self.spec = workflow.load_workflow("code_gen")

    def test_verify_budget_counts_down(self):
        verify = self.spec.nodes["verify"]
        self.assertEqual(plan_transitions(verify, False, 0), (["fixer"], 1))
        self.assertEqual(plan_transitions(verify, False, 1), (["fixer"], 2))
        self.assertEqual(plan_transitions(verify, False, 2), (["human"], 0))
        self.assertEqual(plan_transitions(verify, True, 0), (["done"], 0))

    def test_retry_carried_through_success(self):
        """成功不重置循环计数，否则 修复-验收 对会永远打转"""
        verify = self.spec.nodes["verify"]
        self.assertEqual(plan_transitions(verify, True, 2), (["done"], 2))

    def test_router_route_match_and_fallback(self):
        router = self.spec.nodes["router"]
        self.assertEqual(workflow.match_route(router, "好的 [CODE] 开始"), "coder")
        self.assertEqual(workflow.match_route(router, "你好啊"), "chat_reply")

    def test_render_context(self):
        self.assertEqual(render_context("{prompt} | {retry}", {"prompt": "写代码"}),
                         "写代码 | 0")
        # 缺失变量保留占位符，便于排查而不是静默变成空串
        self.assertEqual(render_context("x={missing}", {}), "x={missing}")


# ---------------------------------------------------------------- 编排行为

class TestCodeGenFlow(EngineTestBase):
    def test_success_path(self):
        self._patch_llm(lambda name, ctx: "[CODE]" if name == "router" else SPEC_TEXT)
        trace = engine.start_workflow("code_gen", "写一个除法函数")
        self.drain(trace)

        self.assertEqual(self.nodes(trace), ["router", "coder", "verify"])
        self.assertTrue(all(s == "done" for _, s, _ in self.tasks(trace)))
        # 分诊 -> 开发 -> 验收，正文由 YAML 决定
        self.assertEqual([n for n, _ in self.llm_calls], ["router", "coder"])

    def test_chat_branch(self):
        self._patch_llm(lambda name, ctx: "你好，我是助手" if name == "router" else "嗨")
        trace = engine.start_workflow("code_gen", "在吗")
        self.drain(trace)
        self.assertEqual(self.nodes(trace), ["router", "chat_reply"])
        self.assertTrue(all(s == "done" for _, s, _ in self.tasks(trace)))

    def test_fail_fix_success(self):
        """验收失败两次后修复成功：3 次验收 + 2 次修复"""
        results = [_fail, _fail, _ok]
        self._patch_tool(lambda tid, ctx: results.pop(0)())
        self._patch_llm(lambda name, ctx: "[CODE]" if name == "router" else SPEC_TEXT)

        trace = engine.start_workflow("code_gen", "写一个除法函数")
        self.drain(trace)

        nodes = self.nodes(trace)
        self.assertEqual(nodes, ["router", "coder", "verify", "fixer", "verify", "fixer", "verify"])
        self.assertEqual(nodes.count("verify"), 3)
        self.assertEqual(nodes.count("fixer"), 2)
        rows = self.tasks(trace)
        self.assertEqual(rows[-1][1], "done")
        # 循环计数随「验收-修复」对传递
        verify_retries = [r for n, _, r in rows if n == "verify"]
        self.assertEqual(verify_retries, [0, 1, 2])

    def test_circuit_breaker_and_human_resume(self):
        """预算耗尽 -> 熔断等人工 -> 建议下发 -> 恢复并重置预算"""
        self._patch_tool(_fail)
        self._patch_llm(lambda name, ctx: "[CODE]" if name == "router" else SPEC_TEXT)

        trace = engine.start_workflow("code_gen", "写一个除法函数")
        self.drain(trace)

        nodes = self.nodes(trace)
        self.assertEqual(nodes,
                         ["router", "coder", "verify", "fixer", "verify", "fixer", "verify", "human"])
        self.assertEqual(nodes.count("verify"), 3)
        self.assertEqual(nodes.count("fixer"), 2)
        waiting = db.get_waiting_human(trace)
        self.assertIsNotNone(waiting)
        self.assertEqual(waiting[1], "human")
        self.assertEqual(self.tasks(trace)[-1][0], "human")
        self.assertEqual(self.tasks(trace)[-1][1], "waiting_human")

        # 人工下发建议 + 恢复
        self._patch_tool(_ok)
        target = engine.resume_with_advice(trace, waiting[0], "捕获 ZeroDivisionError 并打印提示")
        self.assertEqual(target, "fixer")
        self.assertIn("ZeroDivisionError", db.get_var(trace, "advice"))
        self.drain(trace)

        rows = self.tasks(trace)
        self.assertEqual(rows[-1][0], "verify")
        self.assertEqual(rows[-1][1], "done")
        # 恢复后 fixer 预算归零
        last_fixer = [r for n, _, r in rows if n == "fixer"][-1]
        self.assertEqual(last_fixer, 0)

    def test_llm_exception_follows_on_failure(self):
        """coder 大模型异常不再让整条流程静默死掉，而是进修复分支"""
        def llm(name, ctx):
            if name == "router":
                return "[CODE]"
            if name == "coder":
                raise RuntimeError("Connection reset by peer")
            return SPEC_TEXT

        self._patch_llm(llm)
        trace = engine.start_workflow("code_gen", "写一个除法函数")
        self.drain(trace)

        nodes = self.nodes(trace)
        self.assertIn("coder", nodes)
        self.assertIn("fixer", nodes)          # 异常后仍然继续
        self.assertEqual(nodes[-1], "verify")
        self.assertEqual(self.tasks(trace)[-1][1], "done")
        # coder 被标记失败，但流程没死
        status = {r[0]: r[1] for r in self.tasks(trace)}
        self.assertEqual(status["coder"], "failed")


class TestParallelFlow(EngineTestBase):
    def test_parallel_join_dedupe(self):
        def llm(name, ctx):
            # 两个分支用同一个 Agent，靠节点自己的 context 模板区分任务
            if "可行性" in ctx:
                return "A 结论：可行"
            if "风险" in ctx:
                return "B 结论：有风险"
            return f"{name} 的结论"

        self._patch_llm(llm)
        trace = engine.start_workflow("parallel_demo", "做一个数据分析平台")
        self.drain(trace)

        nodes = self.nodes(trace)
        for expected in ("split", "branch_a", "branch_b", "merge", "summarize"):
            self.assertIn(expected, nodes)
        # 两个分支各生成一个汇合任务，但只能汇总一次
        self.assertEqual(nodes.count("summarize"), 1)
        self.assertGreaterEqual(nodes.count("merge"), 1)
        self.assertTrue(all(s == "done" for _, s, _ in self.tasks(trace)))
        # 分支输出通过 publish 进 trace 记忆，汇总结论能拼上
        self.assertEqual(db.get_var(trace, "branch_a"), "A 结论：可行")
        self.assertEqual(db.get_var(trace, "branch_b"), "B 结论：有风险")

    def test_join_state_machine(self):
        spec = workflow.load_workflow("parallel_demo")
        merge = spec.nodes["merge"]
        trace = "join-unit"

        # 分支还没跑 -> wait
        self.assertEqual(engine._join_state(spec, merge, trace), "wait")

        db.create_task(trace, "branch_a", "", 0, workflow=spec.name)
        db.create_task(trace, "branch_b", "", 0, workflow=spec.name)
        for nid in ("branch_a", "branch_b"):
            conn = db.get_conn()
            conn.execute("UPDATE tasks SET status='done' WHERE trace_id=? AND node_id=?", (trace, nid))
            conn.commit()
            conn.close()
        self.assertEqual(engine._join_state(spec, merge, trace), "done")
        self.assertEqual(engine._join_state(spec, merge, trace), "already")

    def test_join_failed_when_branch_fails(self):
        spec = workflow.load_workflow("parallel_demo")
        merge = spec.nodes["merge"]
        trace = "join-fail"
        db.create_task(trace, "branch_a", "", 0, workflow=spec.name)
        db.create_task(trace, "branch_b", "", 0, workflow=spec.name)
        conn = db.get_conn()
        conn.execute("UPDATE tasks SET status='done' WHERE trace_id=? AND node_id='branch_a'", (trace,))
        conn.execute("UPDATE tasks SET status='failed' WHERE trace_id=? AND node_id='branch_b'", (trace,))
        conn.commit()
        conn.close()
        self.assertEqual(engine._join_state(spec, merge, trace), "failed")


# ---------------------------------------------------------------- 黑板与自愈

class TestBlackboard(EngineTestBase):
    def test_trace_vars_roundtrip(self):
        db.set_var("t1", "prompt", "原始需求")
        db.set_var("t1", "prompt", "覆盖后的需求")
        db.set_var("t1", "spec", "规格")
        self.assertEqual(db.get_var("t1", "prompt"), "覆盖后的需求")
        self.assertEqual(db.get_var("t1", "missing", "默认值"), "默认值")
        self.assertEqual(db.get_vars("t1"), {"prompt": "覆盖后的需求", "spec": "规格"})

    def test_priority_ordering(self):
        db.create_task("p1", "low", "", 0, priority=0)
        db.create_task("p1", "high", "", 0, priority=9)
        claimed = db.claim_task()
        self.assertEqual(claimed[2], "high")
        claimed = db.claim_task()
        self.assertEqual(claimed[2], "low")

    def test_delayed_task_not_claimable(self):
        db.create_task("d1", "later", "", 0, delay=60)
        self.assertIsNone(db.claim_task())

    def test_stale_task_requeued_not_killed(self):
        db.create_task("s1", "coder", "ctx", 0, workflow="code_gen")
        task = db.claim_task()
        self.assertIsNotNone(task)
        conn = db.get_conn()
        conn.execute("UPDATE tasks SET started_at='2000-01-01 00:00:00' WHERE task_id=?", (task[0],))
        conn.commit()
        conn.close()

        db.recover_stale_tasks()
        rows = self.tasks("s1")
        self.assertEqual(rows[0][1], "pending")   # 重新入队而不是判死
        self.assertEqual(rows[0][2], 1)           # retry 计数 +1

    def test_stale_task_gives_up_after_max_recovery(self):
        db.create_task("s2", "coder", "ctx", 0, workflow="code_gen")
        task = db.claim_task()
        conn = db.get_conn()
        conn.execute("UPDATE tasks SET started_at='2000-01-01 00:00:00', retry_count=? WHERE task_id=?",
                     (db.MAX_RECOVERY, task[0]))
        conn.commit()
        conn.close()
        db.recover_stale_tasks()
        self.assertEqual(self.tasks("s2")[0][1], "failed")

    def test_max_steps_guard(self):
        """YAML 写出无边界的环也不会把 Worker 卡死"""
        wf_dir = self.tmp / "wf"
        wf_dir.mkdir()
        (wf_dir / "loop.yaml").write_text(yaml.safe_dump({
            "name": "loop", "entry": "a",
            "nodes": [
                {"id": "a", "type": "llm", "agent": "chat", "next": "b"},
                {"id": "b", "type": "llm", "agent": "chat", "on_success": "a"},
            ],
        }), encoding="utf-8")
        orig_dir = workflow.WORKFLOWS_DIR
        workflow.WORKFLOWS_DIR = wf_dir
        try:
            self._patch_llm(lambda name, ctx: "loop")
            trace = engine.start_workflow("loop", "死循环测试")
            for _ in range(engine.MAX_STEPS_PER_TRACE + 5):
                if not self._pending(trace):
                    break
                self._freeze_ready_at(trace)
                engine._step()
        finally:
            workflow.WORKFLOWS_DIR = orig_dir

        rows = self.tasks(trace)
        self.assertLessEqual(len(rows), engine.MAX_STEPS_PER_TRACE + 3)
        self.assertTrue(any(r[1] == "failed" for r in rows))


class TestRendering(unittest.TestCase):
    def test_mermaid_from_spec(self):
        spec = workflow.load_workflow("code_gen")
        text = workflow.to_mermaid(spec, {"router": "done", "verify": "running"})
        self.assertIn("graph LR", text)
        self.assertIn('"[CODE]"', text)
        self.assertIn(":::done", text)
        self.assertIn(":::running", text)
        self.assertIn("classDef failed", text)
        # 图直接从 YAML 生成：每个节点都在图里
        for nid in spec.nodes:
            self.assertIn(nid, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)