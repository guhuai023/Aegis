# -*- coding: utf-8 -*-
"""验证过程中修掉的两个真实缺陷的回归测试。

缺陷 A（依赖安装）：模型写错一行 requirements（非法包名/版本），整批 pip 安装失败，
   整条 trace 被判死。现在整批失败会逐个重装，失败的那个跳过并如实报告。
缺陷 B（工具循环）：工具轮数用尽后直接抛 AgentError，等于"查得挺好但没来得及说话"。
   现在会再做一次**不带工具**的收尾调用，拿到最终答复。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core import agents, tools


def _resp(content=None, tool_calls=None, usage=(10, 5, 15)):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    u = None
    if usage:
        u = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1],
                            total_tokens=usage[2])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=u)


def _tc(call_id, name, args):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name,
                                                    arguments=json.dumps(args)))


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0) if self.script else _resp(content="", usage=None)


class TestPipFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_ws = tools.WORKSPACES_DIR
        self._orig_pip = tools._pip_install
        tools.WORKSPACES_DIR = self.tmp
        self.ws = self.tmp / "t1"
        self.ws.mkdir(parents=True)
        # 预置 venv，跳过真实创建（测试不装环境）
        vpy = tools._venv_python(self.ws)
        vpy.parent.mkdir(parents=True, exist_ok=True)
        vpy.write_text("", encoding="utf-8")
        self.calls = []

    def tearDown(self):
        tools.WORKSPACES_DIR = self._orig_ws
        tools._pip_install = self._orig_pip
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_pip(self, batch_ok, per_pkg):
        def fake(vpy, args, env, flags):
            self.calls.append(list(args))
            if "-r" in args:
                return batch_ok, ("" if batch_ok else "ERROR: Invalid requirement: 'bad pkg'")
            pkg = args[0]
            return per_pkg.get(pkg, (True, ""))
        tools._pip_install = fake

    def test_batch_failure_retries_per_package(self):
        self._patch_pip(False, {"requests": (True, ""), "bad_pkg": (False, "No matching distribution")})
        ok, msg = tools.ensure_venv("t1", ["requests", "bad_pkg"])
        self.assertTrue(ok, msg)
        self.assertIn("部分安装成功", msg)
        self.assertIn("requests", msg)
        self.assertIn("失败已跳过", msg)
        self.assertIn("bad_pkg", msg)
        # 1 次整批 + 2 次逐个
        self.assertEqual(len(self.calls), 3)

    def test_all_packages_failing_still_reports_failure(self):
        self._patch_pip(False, {"a": (False, "boom"), "b": (False, "boom")})
        ok, msg = tools.ensure_venv("t1", ["a", "b"])
        self.assertFalse(ok)
        self.assertIn("pip 安装失败", msg)

    def test_batch_success_skips_retry(self):
        self._patch_pip(True, {})
        ok, msg = tools.ensure_venv("t1", ["requests"])
        self.assertTrue(ok, msg)
        self.assertEqual(len(self.calls), 1)

    def test_no_requirements_no_venv_needed(self):
        ok, msg = tools.ensure_venv("t1", [])
        self.assertTrue(ok)
        self.assertIn("无第三方依赖", msg)


class TestToolRoundExhaustion(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_ws = tools.WORKSPACES_DIR
        self._orig_client = agents._CLIENT
        self._orig_key = agents.API_KEY
        agents.API_KEY = "test-key"
        tools.WORKSPACES_DIR = self.tmp
        (self.tmp / "t1").mkdir()
        (self.tmp / "t1" / "note.txt").write_text("内容", encoding="utf-8")
        self.ctx = tools.ToolContext(trace_id="t1", workspace=self.tmp / "t1")

    def tearDown(self):
        tools.WORKSPACES_DIR = self._orig_ws
        agents._CLIENT = self._orig_client
        agents.API_KEY = self._orig_key
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_last_call_without_tools_recovers_answer(self):
        # 第 1 轮执行工具，第 2 轮（round == rounds）模型仍只给工具调用，
        # 于是触发"不带工具"的收尾调用并拿到最终答复。
        client = FakeClient([
            _resp(tool_calls=[_tc("c1", "read_file", {"path": "note.txt"})]),
            _resp(tool_calls=[_tc("c2", "read_file", {"path": "note.txt"})]),
            _resp(content="最终答案：文件里写的是 内容"),
        ])
        agents._CLIENT = client
        text = agents.ask_agent("role", "user", tools=["read_file"], tool_ctx=self.ctx,
                                max_rounds=1)
        self.assertIn("最终答案", text)
        # 收尾那次请求不应带工具
        self.assertNotIn("tools", client.calls[-1])

    def test_empty_final_answer_raises(self):
        client = FakeClient([
            _resp(tool_calls=[_tc("c1", "read_file", {"path": "note.txt"})]),
            _resp(tool_calls=[_tc("c2", "read_file", {"path": "note.txt"})]),
        ])                                     # 进收尾调用后没有再返回内容
        agents._CLIENT = client
        with self.assertRaises(agents.AgentError):
            agents.ask_agent("role", "user", tools=["read_file"], tool_ctx=self.ctx,
                             max_rounds=1)


if __name__ == "__main__":
    unittest.main()
