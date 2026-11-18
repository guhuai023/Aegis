# -*- coding: utf-8 -*-
"""工作流入参文件（inputs）验证。

企业场景要能"复现"，前提是输入固定：日志、脏数据、待修脚本都从 examples/ 复制进工作区，
而不是让模型现编一份。这组测试覆盖解析、校验（不存在 / 路径逃逸）与落地。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from core import tools, workflow


def _wf_yaml(entry, nodes_yaml, inputs=None):
    extra = ""
    if inputs is not None:
        extra = "inputs:\n" + "".join(f"  - {p}\n" for p in inputs)
    return f"name: probe\nversion: 1\n{extra}entry: {entry}\nnodes:\n{nodes_yaml}"


NODES = """
  - id: a
    type: llm
    agent: coder
    on_success: done
"""


class BaseTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.examples = self.tmp / "examples"
        (self.examples / "data").mkdir(parents=True)
        (self.examples / "data" / "orders.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        self._orig_examples = workflow.EXAMPLES_DIR
        self._orig_tools_examples = tools.EXAMPLES_DIR
        self._orig_ws = tools.WORKSPACES_DIR
        self._orig_wf_dir = workflow.WORKFLOWS_DIR
        workflow.EXAMPLES_DIR = self.examples
        tools.EXAMPLES_DIR = self.examples
        tools.WORKSPACES_DIR = self.tmp / "workspace"
        workflow.WORKFLOWS_DIR = self.tmp / "workflows"
        workflow.WORKFLOWS_DIR.mkdir()
        workflow._CACHE.clear()

    def tearDown(self):
        workflow.EXAMPLES_DIR = self._orig_examples
        tools.EXAMPLES_DIR = self._orig_tools_examples
        tools.WORKSPACES_DIR = self._orig_ws
        workflow.WORKFLOWS_DIR = self._orig_wf_dir
        workflow._CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text, name="probe"):
        (workflow.WORKFLOWS_DIR / f"{name}.yaml").write_text(text, encoding="utf-8")
        workflow._CACHE.clear()


class TestInputsParsing(BaseTmp):
    def test_inputs_parsed(self):
        self.write(_wf_yaml("a", NODES, inputs=["data/orders.csv"]))
        spec = workflow.load_workflow("probe")
        self.assertEqual(spec.inputs, ("data/orders.csv",))

    def test_inputs_survive_roundtrip(self):
        self.write(_wf_yaml("a", NODES, inputs=["data/orders.csv"]))
        spec = workflow.load_workflow("probe")
        data = workflow.spec_to_dict(spec)
        self.assertEqual(data["inputs"], ["data/orders.csv"])
        spec2 = workflow.validate_data(data, "probe")
        self.assertEqual(spec2.inputs, ("data/orders.csv",))

    def test_missing_input_rejected(self):
        self.write(_wf_yaml("a", NODES, inputs=["data/nope.csv"]))
        with self.assertRaises(workflow.WorkflowError) as ctx:
            workflow.load_workflow("probe")
        self.assertIn("不存在", str(ctx.exception))

    def test_traversal_rejected(self):
        self.write(_wf_yaml("a", NODES, inputs=["../etc/passwd"]))
        with self.assertRaises(workflow.WorkflowError) as ctx:
            workflow.load_workflow("probe")
        self.assertIn("..", str(ctx.exception))

    def test_absolute_path_rejected(self):
        self.write(_wf_yaml("a", NODES, inputs=["C:/Windows/win.ini"]))
        with self.assertRaises(workflow.WorkflowError):
            workflow.load_workflow("probe")

    def test_no_inputs_is_fine(self):
        self.write(_wf_yaml("a", NODES))
        spec = workflow.load_workflow("probe")
        self.assertEqual(spec.inputs, ())
        self.assertNotIn("inputs", workflow.spec_to_dict(spec))


class TestSeedWorkspace(BaseTmp):
    def test_copies_and_preserves_relative_path(self):
        copied = tools.seed_workspace("t1", ["data/orders.csv"])
        ws = tools.get_workspace_dir("t1")
        target = ws / "data" / "orders.csv"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "a,b\n1,2\n")
        self.assertEqual(copied, [("data/orders.csv", target.stat().st_size)])

    def test_seed_rejects_traversal(self):
        with self.assertRaises(ValueError):
            tools.seed_workspace("t1", ["../../evil.txt"])

    def test_seed_missing_file_raises(self):
        with self.assertRaises(ValueError):
            tools.seed_workspace("t1", ["data/nope.csv"])

    def test_seeded_file_visible_to_assertions(self):
        tools.seed_workspace("t1", ["data/orders.csv"])
        ws = tools.get_workspace_dir("t1")
        token = tools.run_as_node("n1")
        try:
            (ws / "main.py").write_text(
                "print(open('data/orders.csv').read().strip())", encoding="utf-8")
            ok, report, _ = tools.run_workspace("t1", ["CONTAINS: 1,2"])
        finally:
            tools.reset_run_as_node(token)
        self.assertTrue(ok, report)


if __name__ == "__main__":
    unittest.main()