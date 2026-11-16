# -*- coding: utf-8 -*-
"""Agent 工具调用面（Function Calling）的离线测试。

不联网：把 core.agents._CLIENT 换成假客户端，按预设脚本返回 tool_calls / 最终文本。
覆盖：schema 过滤、沙箱路径校验、run_python 真实执行、工具结果回灌、
用量累加、轮数上限兜底、YAML 声明到 schema 的链路。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core import agents, tools


def _tc(call_id, name, args):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name,
                                                    arguments=json.dumps(args)))


def _resp(content=None, tool_calls=None, usage=(10, 5, 15)):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    u = None
    if usage:
        u = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1],
                            total_tokens=usage[2])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=u)


class FakeClient:
    """按脚本顺序返回响应，并记录每次请求参数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            return _resp(content="（脚本用尽）", usage=None)
        return self.script.pop(0)


class BaseTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_ws = tools.WORKSPACES_DIR
        self._orig_client = agents._CLIENT
        self._orig_key = agents.API_KEY
        agents.API_KEY = "test-key"            # 测试不依赖真实 .env
        tools.WORKSPACES_DIR = self.tmp
        (self.tmp / "t1").mkdir()
        self.ctx = tools.ToolContext(trace_id="t1", workspace=self.tmp / "t1")

    def tearDown(self):
        tools.WORKSPACES_DIR = self._orig_ws
        agents._CLIENT = self._orig_client
        agents.API_KEY = self._orig_key
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use(self, script):
        client = FakeClient(script)
        agents._CLIENT = client
        return client


class TestToolRegistry(BaseTmp):
    def test_schema_filters_unknown_names(self):
        got = [s["function"]["name"] for s in tools.tool_schemas(["read_file", "nope", "run_python"])]
        self.assertEqual(got, ["read_file", "run_python"])

    def test_schema_shape(self):
        spec = tools.tool_schemas(["read_file"])[0]
        self.assertEqual(spec["type"], "function")
        self.assertIn("path", spec["function"]["parameters"]["properties"])

    def test_read_file_ok(self):
        (self.ctx.workspace / "main.py").write_text("print(1)", encoding="utf-8")
        out = tools.call_tool("read_file", self.ctx, {"path": "main.py"})
        self.assertIn("print(1)", out)

    def test_read_file_truncates_huge_file(self):
        (self.ctx.workspace / "big.txt").write_text("x" * (tools.TOOL_OUTPUT_LIMIT + 500),
                                                    encoding="utf-8")
        out = tools.call_tool("read_file", self.ctx, {"path": "big.txt"})
        self.assertIn("已截断", out)

    def test_path_escape_blocked(self):
        self.assertIn("非法路径", tools.call_tool("read_file", self.ctx, {"path": "../o.txt"}))
        self.assertIn("非法路径", tools.call_tool("read_file", self.ctx, {"path": "C:/Windows/win.ini"}))

    def test_missing_file_reported(self):
        self.assertIn("不存在", tools.call_tool("read_file", self.ctx, {"path": "none.py"}))

    def test_list_files(self):
        (self.ctx.workspace / "a.py").write_text("1", encoding="utf-8")
        self.assertIn("a.py", tools.call_tool("list_files", self.ctx, {}))

    def test_run_python_success(self):
        out = tools.call_tool("run_python", self.ctx, {"code": "print(6*7)"})
        self.assertIn("42", out)
        self.assertIn("退出码 0", out)

    def test_run_python_error_captured(self):
        out = tools.call_tool("run_python", self.ctx, {"code": "1/0"})
        self.assertIn("ZeroDivisionError", out)
        self.assertIn("退出码 1", out)

    def test_run_python_empty_code(self):
        self.assertIn("code 为空", tools.call_tool("run_python", self.ctx, {"code": "   "}))

    def test_unknown_tool(self):
        self.assertIn("未注册", tools.call_tool("nope", self.ctx, {}))


class TestToolLoop(BaseTmp):
    def test_tool_round_then_final_answer(self):
        client = self.use([
            _resp(tool_calls=[_tc("c1", "list_files", {})], usage=(10, 5, 15)),
            _resp(content="最终答案", usage=None),
        ])
        observed = []
        out = agents.ask_agent(
            "sys", "user",
            tools=tools.tool_schemas(["list_files"]),
            tool_ctx=self.ctx,
            on_tool=lambda n, a, o: observed.append((n, o)),
            on_usage=lambda u: observed.append(("usage", dict(u))),
        )
        self.assertEqual(out, "最终答案")

        # 工具真的被执行，且结果回灌给了模型
        self.assertEqual(observed[0][0], "list_files")
        self.assertIn("工作区", observed[0][1])
        messages = client.calls[1]["messages"]
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual(messages[-1]["tool_call_id"], "c1")
        self.assertIn("tools", client.calls[0])

        usage = observed[-1][1]
        self.assertEqual(usage["calls"], 2)
        self.assertEqual(usage["tools"], 1)
        self.assertEqual(usage["total"], 15)      # 第二次响应没有 usage，不累加

    def test_no_tools_means_no_tools_param(self):
        client = self.use([_resp(content="ok")])
        self.assertEqual(agents.ask_agent("s", "u"), "ok")
        self.assertNotIn("tools", client.calls[0])

    def test_tool_ctx_without_tools_is_ignored(self):
        client = self.use([_resp(content="ok")])
        agents.ask_agent("s", "u", tools=None, tool_ctx=self.ctx)
        self.assertNotIn("tools", client.calls[0])

    def test_round_limit_raises(self):
        endless = [_resp(tool_calls=[_tc("c%d" % i, "list_files", {})], usage=None)
                   for i in range(10)]
        self.use(endless)
        with self.assertRaises(agents.AgentError):
            agents.ask_agent("s", "u", tools=tools.tool_schemas(["list_files"]),
                             tool_ctx=self.ctx, max_rounds=2)

    def test_empty_content_raises(self):
        self.use([_resp(content="   ", usage=None)])
        old = agents.MAX_ATTEMPTS
        agents.MAX_ATTEMPTS = 1                   # 免得退避重试拖慢测试
        try:
            with self.assertRaises(agents.AgentError):
                agents.ask_agent("s", "u")
        finally:
            agents.MAX_ATTEMPTS = old


class TestSpecToSchema(unittest.TestCase):
    """YAML 里的 tools 声明必须真的变成模型的工具清单。"""

    def test_fixer_declares_runtime_tools(self):
        spec = agents.get_agent("fixer")
        names = [s["function"]["name"] for s in tools.tool_schemas(spec.tools)]
        self.assertIn("run_python", names)
        self.assertIn("read_file", names)

    def test_coder_is_read_only(self):
        spec = agents.get_agent("coder")
        names = [s["function"]["name"] for s in tools.tool_schemas(spec.tools)]
        self.assertIn("read_file", names)
        self.assertNotIn("run_python", names)

    def test_router_has_no_tools(self):
        self.assertEqual(tools.tool_schemas(agents.get_agent("router").tools), [])


class TestEngineWiring(unittest.TestCase):
    """引擎真的把工具上下文传给了声明了 tools 的 Agent。"""

    def setUp(self):
        from core import db, engine, workflow
        self.db, self.engine, self.workflow = db, engine, workflow
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = (db.DB_PATH, tools.WORKSPACES_DIR, engine.WORKSPACES_DIR,
                      engine.ask_agent_spec)
        db.DB_PATH = str(self.tmp / "t.db")
        db.init_db()
        tools.WORKSPACES_DIR = self.tmp
        engine.WORKSPACES_DIR = self.tmp
        self.captured = {}

        def fake(spec, prompt, **kw):
            self.captured[spec.name] = dict(kw)
            return "stub-output"

        engine.ask_agent_spec = fake
        self.spec = workflow.load_workflow("code_gen")

    def tearDown(self):
        (self.db.DB_PATH, tools.WORKSPACES_DIR, self.engine.WORKSPACES_DIR,
         self.engine.ask_agent_spec) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fixer_node_gets_tool_context(self):
        node = self.spec.node("fixer")
        ok, out, _ = self.engine._execute(node, "t1", "ctx", 1)
        self.assertTrue(ok)
        kw = self.captured["fixer"]
        self.assertIsInstance(kw["ctx"], tools.ToolContext)
        self.assertEqual(kw["ctx"].trace_id, "t1")
        self.assertTrue(callable(kw["on_tool"]))
        self.assertTrue(callable(kw["on_usage"]))

    def test_router_node_gets_no_tool_context(self):
        node = self.spec.node("router")
        self.engine._execute(node, "t1", "写个函数", 0)
        kw = self.captured["router"]
        self.assertIsNone(kw["ctx"])          # 没声明 tools → 不挂工具
        self.assertIsNone(kw["on_tool"])

    def test_on_tool_writes_log_and_usage_accumulates(self):
        node = self.spec.node("fixer")
        self.engine._execute(node, "t1", "ctx", 1)
        on_tool = self.captured["fixer"]["on_tool"]
        on_tool("read_file", {"path": "main.py"}, "print(1)")
        conn = self.db.get_conn()
        rows = conn.execute("SELECT level, message FROM logs WHERE trace_id='t1'").fetchall()
        conn.close()
        self.assertTrue(any(lv == "TOOL" and "read_file" in msg for lv, msg in rows))

        on_usage = self.captured["fixer"]["on_usage"]
        on_usage({"calls": 1, "tools": 1, "prompt": 10, "completion": 5, "total": 15})
        on_usage({"calls": 1, "tools": 0, "prompt": 20, "completion": 8, "total": 28})
        data = json.loads(self.db.get_var("t1", "llm_usage"))
        self.assertEqual(data["calls"], 2)
        self.assertEqual(data["total"], 43)


if __name__ == "__main__":
    unittest.main()