import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# 数据库固定锚定在项目根目录，避免受启动时工作目录影响
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = str(BASE_DIR / "aegis_platform.db")

# running 卡死回收阈值等参数集中在 core/config.py
from core.config import STALE_SECONDS

# 僵尸任务最多自动续跑几次后判定为真失败
MAX_RECOVERY = 3


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
    # trace 级工作记忆：节点之间靠它传递 {prompt} / {spec} / {last_error} 等变量
    c.execute('''CREATE TABLE IF NOT EXISTS trace_vars (
        trace_id TEXT, key TEXT, value TEXT, updated_at TEXT,
        PRIMARY KEY (trace_id, key))''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_pending ON tasks(status) WHERE status='pending'")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_trace ON tasks(trace_id)")

    # 增量迁移：老库逐列补齐，重复执行安全
    for col, ddl in (
        ("started_at", "ALTER TABLE tasks ADD COLUMN started_at TEXT"),
        ("workflow", "ALTER TABLE tasks ADD COLUMN workflow TEXT"),          # 该任务属于哪个工作流
        ("priority", "ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 0"),  # 调度优先级
        ("ready_at", "ALTER TABLE tasks ADD COLUMN ready_at TEXT"),          # 最早可执行时间（join 轮询用）
    ):
        try:
            c.execute(ddl)
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


def create_task(trace_id, node_id, context="", retry_count=0, workflow=None,
                priority=0, delay=0):
    """把任务放上黑板（pending）。

    workflow  该任务所属工作流名（引擎据此取节点定义）
    priority  数值大的先执行（默认 0，等价于 FIFO）
    delay     延迟多少秒之后才可被认领（join 汇合等场景）
    """
    task_id = str(uuid.uuid4())[:8]
    ready_at = (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S") if delay else None
    conn = get_conn()
    conn.execute(
        "INSERT INTO tasks (task_id, trace_id, node_id, status, input_context, "
        "retry_count, workflow, priority, ready_at) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
        (task_id, trace_id, node_id, context, retry_count, workflow, priority, ready_at))
    conn.commit()
    conn.close()
    log_event(trace_id, "INFO", f"📝 节点 [{node_id}] 已加入任务黑板")
    return task_id


def claim_task():
    """原子认领一个可执行任务并置为 running。

    BEGIN IMMEDIATE 立即获取写锁：多个 Worker / 多个浏览器会话同时认领时
    在数据库层串行化，杜绝"先 SELECT 再 UPDATE"两步之间被抢走的重复消费。
    排序规则：priority 高的先跑，同优先级按入队顺序（FIFO）。
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT task_id, trace_id, node_id, input_context, retry_count, workflow "
            "FROM tasks WHERE status='pending' AND (ready_at IS NULL OR ready_at <= ?) "
            "ORDER BY priority DESC, rowid LIMIT 1", (_now(),)).fetchone()
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
    """自愈：把卡死在 running 的任务按「自动续跑」处理，而不是直接判死。

    旧版只是标 failed，整条工作流就断在那里；现在会重新入队（retry_count+1）继续跑，
    连续 MAX_RECOVERY 次仍然卡死才判定真失败。
    返回本次回收的任务数。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT task_id, trace_id, node_id, started_at, retry_count "
        "FROM tasks WHERE status='running'").fetchall()
    stale = []
    now_dt = datetime.now()
    for task_id, trace_id, node_id, started_at, retry_count in rows:
        try:
            is_stale = (now_dt - datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")).total_seconds() > STALE_SECONDS
        except (TypeError, ValueError):
            is_stale = True
        if is_stale:
            stale.append((task_id, trace_id, node_id, int(retry_count or 0)))

    for task_id, trace_id, node_id, retry_count in stale:
        if retry_count < MAX_RECOVERY:
            conn.execute(
                "UPDATE tasks SET status='pending', retry_count=?, started_at=NULL WHERE task_id=?",
                (retry_count + 1, task_id))
        else:
            conn.execute("UPDATE tasks SET status='failed' WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()

    for task_id, trace_id, node_id, retry_count in stale:
        if retry_count < MAX_RECOVERY:
            log_event(trace_id, "WARN",
                      f"♻️ 节点 [{node_id}] 长时间无响应，已自动重新入队（第 {retry_count + 1} 次）")
        else:
            log_event(trace_id, "ERROR",
                      f"♻️ 节点 [{node_id}] 连续 {MAX_RECOVERY} 次无响应，判定失败")
    return len(stale)


# ---------------------------------------------------------------- trace 级工作记忆

def set_var(trace_id, key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO trace_vars (trace_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(trace_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (trace_id, key, value, _now()))
    conn.commit()
    conn.close()


def get_var(trace_id, key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM trace_vars WHERE trace_id=? AND key=?",
                       (trace_id, key)).fetchone()
    conn.close()
    return row[0] if row else default


def get_vars(trace_id):
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM trace_vars WHERE trace_id=?", (trace_id,)).fetchall()
    conn.close()
    return {k: v for k, v in rows}


def get_waiting_human(trace_id):
    """当前 trace 里等待人工介入的任务：(task_id, node_id, input_context) 或 None"""
    conn = get_conn()
    row = conn.execute(
        "SELECT task_id, node_id, input_context FROM tasks "
        "WHERE trace_id=? AND status='waiting_human' ORDER BY rowid DESC LIMIT 1",
        (trace_id,)).fetchone()
    conn.close()
    return row


def count_tasks(trace_id):
    """某 trace 已产生的任务行数（引擎用来兜底防无边界死循环）"""
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) FROM tasks WHERE trace_id=?", (trace_id,)).fetchone()
    conn.close()
    return int(row[0] or 0)


def recent_traces(limit=6):
    """最近的任务追踪，供控制台侧边栏展示。

    返回 [(trace_id, 任务数, 工作流名, 最新状态, 最后一条日志时间)]，按最后活动倒序。
    只读查询，不参与调度。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT t.trace_id, COUNT(*), "
        "  (SELECT workflow FROM tasks WHERE trace_id=t.trace_id AND workflow IS NOT NULL "
        "   ORDER BY rowid DESC LIMIT 1), "
        "  (SELECT status FROM tasks WHERE trace_id=t.trace_id ORDER BY rowid DESC LIMIT 1), "
        "  (SELECT timestamp FROM logs WHERE trace_id=t.trace_id ORDER BY id DESC LIMIT 1) "
        "FROM tasks t GROUP BY t.trace_id ORDER BY MAX(t.rowid) DESC LIMIT ?",
        (limit,)).fetchall()
    conn.close()
    return rows


def node_statuses(trace_id, node_ids):
    """各节点的**最新**状态（供 UI 上色与 join 判定）。

    旧版是"历史上出现过 failed 就永远显示 failed"，
    循环重试最终成功之后，拓扑图还是一片红；现在按 rowid 取最后一次。
    """
    if not node_ids:
        return {}
    conn = get_conn()
    rows = conn.execute(
        "SELECT node_id, status FROM tasks WHERE trace_id=? ORDER BY rowid", (trace_id,)).fetchall()
    conn.close()
    latest = {}
    for node_id, status in rows:
        latest[node_id] = status          # 后面的行覆盖前面的，天然取最新
    return {nid: latest.get(nid, "pending") for nid in node_ids}