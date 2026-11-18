# -*- coding: utf-8 -*-
"""引擎单写者租约（避免多个引擎进程抢任务）。

真实踩过的坑：一个 16:46 启动的 Streamlit 进程自带引擎，Agent 注册表是它启动时的快照；
我 16:57 改了 analyst 的提示词，之后新跑的任务被这个旧进程认领，
于是"新工作流用旧提示词"——排查了三轮才定位。
现在启动时抢租约：同库已有心跳新鲜的引擎就拒绝启动，僵尸租约 90 秒后自动让位。
"""
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core import db


class TestEngineLease(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = db.DB_PATH
        db.DB_PATH = str(self.tmp / "lease.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _age_lease(self, pid, seconds):
        conn = db.get_conn()
        conn.execute("UPDATE engine_lease SET heartbeat_at=?, started_at=? WHERE id=1",
                     ((datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S"),) * 2)
        conn.commit()
        conn.close()

    def test_first_acquire_ok(self):
        ok, info = db.acquire_engine_lease(111)
        self.assertTrue(ok)
        self.assertEqual(info["pid"], 111)

    def test_second_process_refused(self):
        db.acquire_engine_lease(111)
        ok, info = db.acquire_engine_lease(222)
        self.assertFalse(ok)
        self.assertEqual(info["pid"], 111)

    def test_same_pid_reacquire_ok(self):
        db.acquire_engine_lease(111)
        ok, _ = db.acquire_engine_lease(111)
        self.assertTrue(ok)

    def test_stale_lease_taken_over(self):
        db.acquire_engine_lease(111)
        self._age_lease(111, db.ENGINE_LEASE_TTL + 30)     # 模拟旧进程已死
        ok, info = db.acquire_engine_lease(222)
        self.assertTrue(ok, info)
        self.assertEqual(info["pid"], 222)

    def test_heartbeat_keeps_lease_alive(self):
        db.acquire_engine_lease(111)
        self._age_lease(111, 60)
        db.heartbeat_engine_lease(111)                     # 续期后应当仍占用
        ok, info = db.acquire_engine_lease(222)
        self.assertFalse(ok)
        self.assertEqual(info["pid"], 111)

    def test_heartbeat_by_wrong_pid_does_nothing(self):
        db.acquire_engine_lease(111)
        self._age_lease(111, db.ENGINE_LEASE_TTL + 5)
        db.heartbeat_engine_lease(999)                     # 不是持有者，不该续期
        ok, _ = db.acquire_engine_lease(222)
        self.assertTrue(ok)

    def test_release_frees_lease(self):
        db.acquire_engine_lease(111)
        db.release_engine_lease(111)
        ok, _ = db.acquire_engine_lease(222)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()