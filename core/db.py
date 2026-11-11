import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

# 数据库固定锚定在项目根目录，避免受启动时工作目录影响
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = str(BASE_DIR / "aegis_platform.db")

# running 卡死回收阈值等参数集中在 core/config.py
from core.config import STALE_SECONDS


def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    conn = get_conn()
    c = conn.cursor()
    # WAL 模式：UI 读与引擎写并发时不再互相阻塞（"database is locked" 概率大幅下降）
    c.execute("PRAGMA journal_mode=WAL")
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY, trace_id TEXT, node_id TEXT,
        status TEXT, input_context TEXT, output_result TEXT,
        retry_count INTEGER DEFAULT 0, started_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT,
        timestamp TEXT, level TEXT, message TEXT)''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_pending ON tasks(status) WHERE status='pending'")
    try:
        c.execute("ALTER TABLE tasks ADD COLUMN started_at TEXT")  # 老库无该列时迁移
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    conn.close()


def log_event(trace_id, level, message):
    conn = get_conn()
    conn.execute("INSERT INTO logs (trace_id, timestamp, level, message) VALUES (?, ?, ?, ?)",
                 (trace_id, datetime.now().strftime("%H:%M:%S"), level, message))
    conn.commit()
    conn.close()


def create_task(trace_id, node_id, context="", retry_count=0):
    task_id = str(uuid.uuid4())[:8]
    conn = get_conn()
    conn.execute("INSERT INTO tasks (task_id, trace_id, node_id, status, input_context, retry_count) VALUES (?, ?, ?, 'pending', ?, ?)",
                 (task_id, trace_id, node_id, context, retry_count))
    conn.commit()
    conn.close()
    log_event(trace_id, "INFO", f"📝 节点 [{node_id}] 已加入任务黑板")
    return task_id


def claim_task():
    """原子认领一个 pending 任务并置为 running。

    BEGIN IMMEDIATE 立即获取写锁：多个 Worker / 多个浏览器会话同时认领时
    在数据库层串行化，杜绝旧版"先 SELECT 再 UPDATE"两步之间被抢走的重复消费。
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT task_id, trace_id, node_id, input_context, retry_count "
            "FROM tasks WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
        if row is None:
            conn.rollback()
            return None
        conn.execute("UPDATE tasks SET status='running', started_at=? WHERE task_id=?",
                     (_now(), row[0]))
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_task(task_id, status, output=None):
    """统一收口：更新任务终态（done / failed / waiting_human）"""
    conn = get_conn()
    conn.execute("UPDATE tasks SET status=?, output_result=? WHERE task_id=?",
                 (status, output, task_id))
    conn.commit()
    conn.close()


def recover_stale_tasks():
    """自愈：把卡死在 running 的任务标记为 failed（进程崩溃 / 异常中断的遗留）。

    老版本没有 started_at 列，凡是 running 且无法解析开始时间的任务一律视为卡死。
    返回本次回收的任务数。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT task_id, trace_id, node_id, started_at FROM tasks WHERE status='running'").fetchall()
    stale = []
    now_dt = datetime.now()
    for task_id, trace_id, node_id, started_at in rows:
        try:
            is_stale = (now_dt - datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")).total_seconds() > STALE_SECONDS
        except (TypeError, ValueError):
            is_stale = True
        if is_stale:
            stale.append((task_id, trace_id, node_id))
    for task_id, _, _ in stale:
        conn.execute("UPDATE tasks SET status='failed' WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()
    for _, trace_id, node_id in stale:
        log_event(trace_id, "ERROR", f"♻️ 节点 [{node_id}] 长时间无响应，超时回收为 failed")
    return len(stale)
