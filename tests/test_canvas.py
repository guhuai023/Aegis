# -*- coding: utf-8 -*-
"""编排画布与定义序列化测试（离线）。

覆盖：分层布局、边语义、SVG 渲染、YAML 往返一致、校验拒绝非法定义、原子保存不脏写。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from core import workflow
from core.workflow import (
    WorkflowError, blank_node, load_workflow, save_workflow_data, spec_to_dict,
    spec_to_yaml, validate_data,
)
from ui import canvas


class TestLayout(unittest.TestCase):
    def setUp(self):
        self.code = load_workflow("code_gen")
        self.par = load_workflow("parallel_demo")

    def _col(self, pos, node_id):
        return (pos[node_id][0] - canvas.PAD_X) // canvas.COL_W

    def test_entry_is_first_column(self):
        pos = canvas.layout(self.code)
        self.assertEqual(self._col(pos, self.code.entry), 0)

    def test_successors_are_to_the_right(self):
        pos = canvas.layout(self.code)
        for edge in canvas.edges(self.code):
            if edge["dst"] in self.code.nodes and edge["kind"] in ("next", "on_success", "route"):
                self.assertLessEqual(pos[edge["src"]][0], pos[edge["dst"]][0])

    def test_join_node_sinks_below_its_deps(self):
        pos = canvas.layout(self.par)
        for node in self.par.nodes.values():
            for dep in node.join:
                self.assertGreater(pos[node.id][0], pos[dep][0])

    def test_terminal_pseudo_nodes(self):
        pos = canvas.layout(self.code)
        self.assertIn("__done", pos)
        par = canvas.layout(self.par)
        self.assertIn("__done", par)
        self.assertIn("__failed", par)

    def test_all_nodes_have_positions(self):
        for spec in (self.code, self.par):
            pos = canvas.layout(spec)
            for node_id in spec.nodes:
                self.assertIn(node_id, pos)


class TestEdges(unittest.TestCase):
    def setUp(self):
        self.code = load_workflow("code_gen")
        self.par = load_workflow("parallel_demo")

    def test_edge_kinds_cover_control_structures(self):
        kinds = {e["kind"] for e in canvas.edges(self.code)}
        self.assertIn("route", kinds)          # 条件分支
        self.assertIn("on_failure", kinds)     # 失败回边
        self.assertIn("on_resume", kinds)      # 人工恢复

    def test_join_dependency_direction(self):
        joins = [e for e in canvas.edges(self.par) if e["kind"] == "join"]
        self.assertTrue(joins)
        self.assertTrue(all(e["dst"] == "merge" for e in joins))
        self.assertEqual({e["src"] for e in joins}, {"branch_a", "branch_b"})

    def test_parallel_edges(self):
        par = [e for e in canvas.edges(self.par) if e["kind"] == "parallel"]
        self.assertEqual(len(par), 2)

    def test_failure_label_mentions_retry_budget(self):
        edges = [e for e in canvas.edges(self.code) if e["kind"] == "on_failure"]
        self.assertTrue(any("重试" in e["label"] for e in edges))

    def test_no_edge_points_to_unknown_node(self):
        for spec in (self.code, self.par):
            known = set(spec.nodes) | set(workflow.TERMINAL)
            for edge in canvas.edges(spec):
                self.assertIn(edge["dst"], known)


class TestRender(unittest.TestCase):
    def test_svg_contains_every_node(self):
        spec = load_workflow("code_gen")
        html = canvas.render(spec)
        self.assertTrue(html.startswith('<div class="ag-canvas"'))
        self.assertIn("<svg", html)
        for node in spec.nodes.values():
            self.assertIn(node.id, html)

    def test_render_has_no_broken_numbers(self):
        html = canvas.render(load_workflow("parallel_demo"))
        self.assertNotIn("NaN", html)
        self.assertNotIn("None", html)
        self.assertIn("marker-end", html)

    def test_legend_can_be_disabled(self):
        spec = load_workflow("code_gen")
        self.assertIn("成功/顺序", canvas.render(spec))
        self.assertNotIn("成功/顺序", canvas.render(spec, show_legend=False))

    def test_escapes_route_labels(self):
        """路由标签里有 [CODE] 这类字符，不能被当成 HTML。"""
        spec = load_workflow("code_gen")
        self.assertIn("[CODE]", canvas.render(spec))


class TestSerialization(unittest.TestCase):
    def test_roundtrip_both_workflows(self):
        for name in ("code_gen", "parallel_demo"):
            spec = load_workflow(name)
            data = spec_to_dict(spec)
            reloaded = validate_data(yaml.safe_load(spec_to_yaml(spec)))
            self.assertEqual(spec_to_dict(reloaded), data, name)

    def test_roundtrip_preserves_control_fields(self):
        data = spec_to_dict(load_workflow("code_gen"))
        by_id = {n["id"]: n for n in data["nodes"]}
        self.assertEqual(by_id["router"]["routes"], {"[CODE]": "coder"})
        # 重试预算挂在验收节点上：验收失败 -> 修复，连续失败 2 次 -> 熔断转人工
        self.assertEqual(by_id["verify"]["max_retries"], 2)
        self.assertEqual(by_id["verify"]["on_failure"], "fixer")
        self.assertEqual(by_id["verify"]["on_exhausted"], "human")
        self.assertEqual(by_id["human"]["on_resume"], "fixer")

    def test_yaml_is_readable(self):
        text = spec_to_yaml(load_workflow("code_gen"))
        self.assertIn("name: code_gen", text)
        self.assertIn("entry: router", text)
        self.assertNotIn("\\u", text)          # 中文不能被转义

    def test_validate_rejects_dangling_target(self):
        data = {"name": "x", "entry": "a",
                "nodes": [{"id": "a", "type": "llm", "agent": "router", "next": "ghost"}]}
        with self.assertRaises(WorkflowError) as ctx:
            validate_data(data, "x")
        self.assertTrue(any("不存在" in e for e in ctx.exception.errors))

    def test_validate_rejects_unreachable_node(self):
        data = {"name": "x", "entry": "a",
                "nodes": [{"id": "a", "type": "llm", "agent": "router"},
                          {"id": "b", "type": "llm", "agent": "coder"}]}
        with self.assertRaises(WorkflowError) as ctx:
            validate_data(data, "x")
        self.assertTrue(any("不可达" in e for e in ctx.exception.errors))

    def test_validate_rejects_retry_without_on_failure(self):
        data = {"name": "x", "entry": "a",
                "nodes": [{"id": "a", "type": "llm", "agent": "router", "max_retries": 2}]}
        with self.assertRaises(WorkflowError) as ctx:
            validate_data(data, "x")
        self.assertTrue(any("on_failure" in e for e in ctx.exception.errors))

    def test_blank_node_defaults_are_valid_shape(self):
        self.assertEqual(blank_node("n") ["agent"], "coder")
        self.assertEqual(blank_node("n", "tool")["tool"], "execute_spec")
        self.assertEqual(blank_node("n", "join")["join"], [])


class TestSaveWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = workflow.WORKFLOWS_DIR
        workflow.WORKFLOWS_DIR = self.tmp
        workflow._CACHE.clear()

    def tearDown(self):
        workflow.WORKFLOWS_DIR = self._orig_dir
        workflow._CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _good(self):
        return {"name": "tiny", "version": 1, "entry": "a", "nodes": [
            {"id": "a", "type": "llm", "agent": "router", "next": "b"},
            {"id": "b", "type": "llm", "agent": "chat"},
        ]}

    def test_save_writes_and_reloads(self):
        path = save_workflow_data("tiny", self._good())
        self.assertTrue(path.exists())
        spec = load_workflow("tiny", use_cache=False)
        self.assertEqual(sorted(spec.nodes), ["a", "b"])
        self.assertEqual(spec.entry, "a")

    def test_save_rejected_definition_leaves_file_untouched(self):
        path = save_workflow_data("tiny", self._good())
        before = path.read_text(encoding="utf-8")
        broken = self._good()
        broken["nodes"][0]["next"] = "ghost"
        with self.assertRaises(WorkflowError):
            save_workflow_data("tiny", broken)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_saved_file_is_valid_yaml(self):
        path = save_workflow_data("tiny", self._good())
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "tiny")
        self.assertEqual(len(data["nodes"]), 2)

    def test_no_tmp_file_left_behind(self):
        save_workflow_data("tiny", self._good())
        leftovers = list(self.tmp.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_save_keeps_leading_comment_block(self):
        """编辑保存会重排 YAML，但文件头的说明注释必须保留（文档资产）。"""
        path = self.tmp / "tiny.yaml"
        path.write_text("# 编排说明：为什么这么拆\n# 字段约定：entry 必须是 llm 节点\n\n"
                        "name: tiny\nentry: a\nnodes: []\n", encoding="utf-8")
        save_workflow_data("tiny", self._good())
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 编排说明"), text[:80])
        self.assertIn("# 字段约定", text)
        self.assertIn("name: tiny", text)

    def test_save_without_existing_file_has_no_header(self):
        save_workflow_data("fresh", self._good())
        text = (self.tmp / "fresh.yaml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("name:"), text[:40])

    def test_save_keeps_header_even_with_bom(self):
        """Windows 记事本/PowerShell 写的文件带 UTF-8 BOM，注释块也要认出来。"""
        path = self.tmp / "bom.yaml"
        path.write_text("\ufeff# 文件头注释\nname: tiny\nentry: a\nnodes: []\n",
                        encoding="utf-8")
        save_workflow_data("tiny", self._good())
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 文件头注释", text)
        self.assertIn("name: tiny", text)


if __name__ == "__main__":
    unittest.main()