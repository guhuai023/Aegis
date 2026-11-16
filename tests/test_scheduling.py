# -*- coding: utf-8 -*-
"""调度器测试：能力匹配 / 动态优先级 / 老化防饿死 / 并发认领 / 观测快照。

全部离线，用临时 SQLite，不碰真实黑板。
"""
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core import db, engine
from core.workflow import load_workflow


def _ago(seconds):
    return (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


class BaseDB(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_db = db.DB_PATH
        db.DB_PATH = str(self.tmp / "sched.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig_db
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestTaskProfile(BaseDB):
    """节点语义 -> 能力标签 / 优先级。"""

    def setUp(self):
        super().setUp()
        self.spec = load_workflow("code_gen")

    def test_capability_by_node_type(self):
        self.assertEqual(engine._capability(self.spec.node("coder")), "llm:coder")
        self.assertEqual(engine._capability(self.spec.node("verify")), "tool:execute_spec")
        self.assertEqual(engine._capability(self.spec.node("human")), "human")

    def test_priority_by_node_semantics(self):
        self.assertEqual(engine._priority(self.spec.node("human")), 100)
        self.assertEqual(engine._priority(self.spec.node("fixer")), 60)
        self.assertEqual(engine._priority(self.spec.node("verify")), 50)
        self.assertEqual(engine._priority(self.spec.node("coder")), 30)
        self.assertEqual(engine._priority(self.spec.node("router")), 20)

    def test_new_task_writes_capability_and_priority(self):
        task_id = engine._new_task(self.spec, "t1", "verify", "ctx", 0)
        row = self._row(task_id)
        self.assertEqual(row["capability"], "tool:execute_spec")
        self.assertEqual(row["priority"], 50)
        self.assertIsNotNone(row["created_at"])

    def test_human_resume_gets_top_priority(self):
        task_id = engine._new_task(self.spec, "t1", "fixer", "advice", 0, priority_boost=100)
        self.assertEqual(self._row(task_id)["priority"], 160)

    def _row(self, task_id):
        conn = db.get_conn()
        cur = conn.execute("SELECT capability, priority, created_at FROM tasks WHERE task_id=?",
                           (task_id,))
        cols = [c[0] for c in cur.description]
        row = dict(zip(cols, cur.fetchone()))
        conn.close()
        return row


class TestPriorityOrder(BaseDB):
    def _mk(self, node, priority, capability=None, age=0):
        tid = db.create_task("t1", node, "", 0, workflow="code_gen",
                             priority=priority, capability=capability)
        if age:
            conn = db.get_conn()
            conn.execute("UPDATE tasks SET created_at=? WHERE task_id=?", (_ago(age), tid))
            conn.commit()
            conn.close()
        return tid

    def test_higher_priority_claims_first(self):
        low = self._mk("coder", 30, "llm:coder")
        high = self._mk("fixer", 60, "llm:fixer")
        claimed = db.claim_task()
        self.assertEqual(claimed[0], high)
        self.assertEqual(db.claim_task()[0], low)

    def test_fifo_within_same_priority(self):
        first = self._mk("a", 30)
        second = self._mk("b", 30)
        self.assertEqual(db.claim_task()[0], first)
        self.assertEqual(db.claim_task()[0], second)

    def test_aging_lets_starved_task_win(self):
        """低优先级任务等久了，有效优先级反超刚入队的高优先级任务。"""
        starved = self._mk("join", 10, age=600)     # 等 10 分钟 -> 老化 +20 -> 有效 30
        fresh = self._mk("coder", 25)               # 刚入队 -> 有效 25
        self.assertEqual(db.claim_task()[0], starved)
        self.assertEqual(db.claim_task()[0], fresh)

    def test_aging_is_capped(self):
        """老化最多 +AGING_MAX，不会无限涨。"""
        old_low = self._mk("join", 10, age=10 ** 6)   # 等很久，但顶多 +20 -> 30
        high = self._mk("fixer", 60)
        self.assertEqual(db.claim_task()[0], high)
        self.assertEqual(db.claim_task()[0], old_low)


class TestCapabilityMatch(BaseDB):
    def test_worker_only_takes_matching_tasks(self):
        llm_task = db.create_task("t1", "coder", "", 0, workflow="code_gen",
                                  priority=90, capability="llm:coder")
        tool_task = db.create_task("t1", "verify", "", 0, workflow="code_gen",
                                   priority=10, capability="tool:execute_spec")
        got = db.claim_task(["tool:execute_spec"])
        self.assertEqual(got[0], tool_task)          # 优先级低，但能力匹配才拿得到
        self.assertEqual(db.claim_task(["tool:execute_spec"]), None)
        got = db.claim_task(["llm:coder"])
        self.assertEqual(got[0], llm_task)

    def test_unlabelled_task_claimable_by_anyone(self):
        tid = db.create_task("t1", "legacy", "", 0, workflow="code_gen", priority=5)
        self.assertEqual(db.claim_task(["tool:execute_spec"])[0], tid)

    def test_no_capability_filter_takes_everything(self):
        a = db.create_task("t1", "a", "", 0, workflow="code_gen", priority=1,
                           capability="llm:coder")
        self.assertEqual(db.claim_task(None)[0], a)


class TestConcurrentClaim(BaseDB):
    def test_four_workers_never_double_claim(self):
        total = 40
        for i in range(total):
            db.create_task("t1", "n%d" % i, "", 0, workflow="code_gen",
                           priority=(i % 5), capability="llm:coder")
        got, lock = [], threading.Lock()

        def worker():
            while True:
                row = db.claim_task(["llm:coder"])
                if row is None:
                    return
                with lock:
                    got.append(row[0])

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(got), total)
        self.assertEqual(len(set(got)), total)       # 无重复消费

    def test_ready_at_still_respected(self):
        db.create_task("t1", "delayed", "", 0, workflow="code_gen", delay=30)
        self.assertIsNone(db.claim_task())
        conn = db.get_conn()
        conn.execute("UPDATE tasks SET ready_at=NULL")
        conn.commit()
        conn.close()
        self.assertIsNotNone(db.claim_task())


class TestSchedulerSnapshot(BaseDB):
    def test_snapshot_groups_pending_by_capability(self):
        db.create_task("t1", "a", "", 0, workflow="code_gen", capability="llm:coder", priority=30)
        db.create_task("t2", "b", "", 0, workflow="code_gen", capability="llm:coder", priority=30)
        db.create_task("t3", "c", "", 0, workflow="code_gen", capability="tool:execute_spec",
                       priority=50)
        snap = db.scheduler_snapshot()
        self.assertEqual(snap["running"], 0)
        buckets = {b["capability"]: b for b in snap["buckets"]}
        self.assertEqual(buckets["llm:coder"]["pending"], 2)
        self.assertEqual(buckets["tool:execute_spec"]["pending"], 1)
        self.assertEqual(buckets["tool:execute_spec"]["top_priority"], 50)
        self.assertGreaterEqual(buckets["llm:coder"]["wait_s"], 0)

    def test_snapshot_counts_running(self):
        tid = db.create_task("t1", "a", "", 0, workflow="code_gen")
        db.claim_task()
        self.assertEqual(db.scheduler_snapshot()["running"], 1)


class TestEngineState(BaseDB):
    def test_worker_state_records_claims(self):
        engine.WORKER_STATE.clear()
        engine.WORKER_STATE["worker-0"] = {"caps": ["*"], "claimed": 0, "last_task": None}
        db.create_task("t1", "a", "", 0, workflow="code_gen")
        engine._step()
        self.assertEqual(engine.WORKER_STATE["worker-0"]["claimed"], 1)
        self.assertTrue(engine.WORKER_STATE["worker-0"]["last_task"].startswith("a@"))

    def test_step_with_capabilities_skips_others(self):
        engine.WORKER_STATE.clear()
        tid = db.create_task("t1", "a", "", 0, workflow="code_gen", priority=99,
                             capability="llm:coder")
        engine._step(["tool:execute_spec"])
        self.assertEqual(self._status(tid), "pending")      # 能力不匹配 -> 不动它
        engine._step(["llm:coder"])
        self.assertEqual(self._status(tid), "failed")       # 被认领并执行（节点未定义 -> 判失败）

    def _status(self, task_id):
        conn = db.get_conn()
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        conn.close()
        return row[0]


if __name__ == "__main__":
    unittest.main()