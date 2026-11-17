# -*- coding: utf-8 -*-
"""工作区产物隔离粒度测试。

规则（数据管线能跑通的前提）：
  · 同一节点重试 -> 清掉它自己上一轮的产物（FILE_EXISTS 不被上次残留骗过）
  · 不同节点      -> 产物跨节点保留（A 支线清洗 -> 汇合 -> B 节点合并）
  · 无节点上下文  -> 退回全量清理（兼容直接调用/旧数据）
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from core import tools


def spec_text(files, assertions, requirements=()):
    parts = ["=== REQUIREMENTS ===", "\n".join(requirements), "",
             "=== ASSERTIONS ===", "\n".join(assertions), ""]
    for name, code in files.items():
        parts += [f"=== FILE: {name} ===", code, ""]
    return "\n".join(parts)


class BaseWs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = tools.WORKSPACES_DIR
        tools.WORKSPACES_DIR = self.tmp
        self.ws = self.tmp / "t1"
        self.ws.mkdir(parents=True)

    def tearDown(self):
        tools.WORKSPACES_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_node(self, node_id, files, assertions):
        token = tools.run_as_node(node_id)
        try:
            return tools.execute_spec("t1", spec_text(files, assertions))
        finally:
            tools.reset_run_as_node(token)


class TestNodeScopedArtifacts(BaseWs):
    CODE_A = ("import csv\n"
              "with open('a.csv','w',newline='',encoding='utf-8') as f:\n"
              "    w = csv.writer(f); w.writerow(['渠道','金额']); w.writerow(['A',100])\n"
              "print('a done')\n")

    def test_other_node_artifacts_survive(self):
        r = self.run_node("verify_a", {"main.py": self.CODE_A},
                          ["FILE_EXISTS: a.csv", "CONTAINS: a done"])
        self.assertTrue(r.ok, r.report)
        self.assertTrue((self.ws / "a.csv").exists())

        # 另一个节点跑完，不能把 a.csv 清掉
        r2 = self.run_node("verify_b", {"main.py": "print('b done')"},
                           ["CONTAINS: b done"])
        self.assertTrue(r2.ok, r2.report)
        self.assertTrue((self.ws / "a.csv").exists(), "跨节点产物被误删了")

    def test_same_node_retry_cleans_own_artifacts(self):
        self.run_node("verify_a", {"main.py": self.CODE_A},
                      ["FILE_EXISTS: a.csv"])
        self.assertTrue((self.ws / "a.csv").exists())

        # 同一节点重试：这次不产出 a.csv，上一轮的 a.csv 必须已被清掉，
        # 否则新断言 FILE_EXISTS 会被残留文件骗过（假通过）
        r = self.run_node("verify_a", {"main.py": "print('nothing')"},
                          ["FILE_EXISTS: a.csv"])
        self.assertFalse(r.ok, "残留产物让 FILE_EXISTS 假通过了")
        self.assertFalse((self.ws / "a.csv").exists())

    def test_downstream_node_can_read_upstream_artifact(self):
        """数据管线的核心：合并节点要读得到上游清洗产物。"""
        self.run_node("verify_a", {"main.py": self.CODE_A}, ["FILE_EXISTS: a.csv"])
        code = ("import csv\n"
                "rows = list(csv.reader(open('a.csv', encoding='utf-8')))\n"
                "with open('merged.csv','w',newline='',encoding='utf-8') as f:\n"
                "    csv.writer(f).writerows(rows)\n"
                "print('rows=%d' % len(rows))\n")
        r = self.run_node("verify_merge", {"main.py": code},
                          ["FILE_EXISTS: merged.csv", "CONTAINS: rows=2",
                           "REGEX_FILE: merged.csv :: ^渠道,金额$"])
        self.assertTrue(r.ok, r.report)

    def test_no_context_still_cleans_everything(self):
        self.run_node("verify_a", {"main.py": self.CODE_A}, ["FILE_EXISTS: a.csv"])
        self.assertTrue((self.ws / "a.csv").exists())
        # 没有节点上下文（直接调用）：保持旧语义，全量清理
        r = tools.execute_spec("t1", spec_text({"main.py": "print('x')"}, ["CONTAINS: x"]))
        self.assertTrue(r.ok, r.report)
        self.assertFalse((self.ws / "a.csv").exists())

    def test_manifest_is_hidden_from_deliverables(self):
        self.run_node("verify_a", {"main.py": self.CODE_A}, ["FILE_EXISTS: a.csv"])
        files = tools.list_workspace_files("t1")
        self.assertIn("a.csv", files)
        self.assertFalse(any(".aegis" in f for f in files), files)


class TestTraceToolLock(unittest.TestCase):
    """同一 trace 的落盘/执行必须串行：并行支线共享工作区与固定入口 main.py。"""

    def setUp(self):
        from core import engine
        self.engine = engine

    def test_same_trace_shares_lock(self):
        a = self.engine._trace_tool_lock("t-same")
        b = self.engine._trace_tool_lock("t-same")
        self.assertIs(a, b)

    def test_different_traces_have_different_locks(self):
        a = self.engine._trace_tool_lock("t-one")
        b = self.engine._trace_tool_lock("t-two")
        self.assertIsNot(a, b)

    def test_lock_serializes_two_threads(self):
        import threading
        import time

        lock = self.engine._trace_tool_lock("t-serial")
        events = []

        def worker(name):
            with lock:
                events.append(f"{name}+")
                time.sleep(0.05)
                events.append(f"{name}-")

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 串行执行时成对出现的顺序必须是 a+b+a-b- 或 b+a-b+a-，不会交叉
        self.assertIn(events, (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"]))


if __name__ == "__main__":
    unittest.main()