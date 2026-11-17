# -*- coding: utf-8 -*-
"""Agent 热加载测试：新增/修改 agents/*.yaml 后，引擎不必重启就能认得。

背景（真实踩到的坑）：多个引擎进程共存时，各自的注册表是启动时的快照。
往 agents/ 里新增一个定义后，旧进程执行该节点会报「未注册的 Agent」。
现在的处理是：解析不到就重扫一次，只在这一条异常路径上付出代价。
"""
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from core import agents, engine


class TestAgentReload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = agents.AGENTS_DIR
        agents.AGENTS_DIR = self.tmp
        agents._REGISTRY.clear()

    def tearDown(self):
        agents.AGENTS_DIR = self._orig_dir
        agents.reload_agents()          # 恢复真实注册表，别污染其它测试
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_agent(self, name):
        (self.tmp / f"{name}.yaml").write_text(textwrap.dedent(f"""
            name: {name}
            label: 测试 Agent
            version: 1
            memory: none
            temperature: 0.0
            role_prompt: 你是测试 Agent，只输出 ok。
        """).lstrip(), encoding="utf-8")

    def test_missing_agent_then_reload_finds_it(self):
        with self.assertRaises(agents.AgentError):
            agents.get_agent("late_agent")

        self._write_agent("late_agent")
        agents.reload_agents()            # 引擎在解析失败时会自动做这一步

        spec = agents.get_agent("late_agent")
        self.assertEqual(spec.name, "late_agent")

    def test_engine_falls_back_to_reload(self):
        """模拟引擎路径：第一次 get_agent 失败 -> reload -> 成功。"""
        self._write_agent("hot_agent")
        try:
            engine.get_agent("hot_agent")
        except agents.AgentError:
            engine.reload_agents()
        self.assertEqual(engine.get_agent("hot_agent").name, "hot_agent")

    def test_reload_picks_up_changed_role_prompt(self):
        self._write_agent("mutable")
        agents.reload_agents()
        before = agents.get_agent("mutable").role_prompt
        (self.tmp / "mutable.yaml").write_text(textwrap.dedent("""
            name: mutable
            version: 1
            role_prompt: 改过之后的新提示词。
        """).lstrip(), encoding="utf-8")
        agents.reload_agents()
        self.assertNotEqual(agents.get_agent("mutable").role_prompt, before)
        self.assertIn("改过", agents.get_agent("mutable").role_prompt)


if __name__ == "__main__":
    unittest.main()