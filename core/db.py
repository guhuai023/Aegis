import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# 数据库固定锚定在项目根目录，避免受启动时工作目录影响
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = str(BASE_DIR / "aegis_platform.db")

# running 卡死回收阈值等参数集中在 core/config.py
from core.config import AGING_MAX, AGING_STEP, STALE_SECONDS

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
    # 引擎单写者租约：同一个库只允许一个引擎主进程认领任务。
    # 多个引擎共存时，旧进程会用它启动时那份 Agent 定义抢走新任务（真实踩过三次），
    # "任务由谁执行"不该取决于谁开机早。
    c.execute('''CREATE TABLE IF NOT EXISTS engine_lease (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        pid INTEGER, started_at TEXT, heartbeat_at TEXT)''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_pending ON tasks(status) WHERE status='pending'")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_trace ON tasks(trace_id)")

    # 增量迁移：老库逐列补齐，重复执行安全
    for col, ddl in (
        ("started_at", "ALTER TABLE tasks ADD COLUMN started_at TEXT"),
        ("workflow", "ALTER TABLE tasks ADD COLUMN workflow TEXT"),          # 该任务属于哪个工作流
        ("priority", "ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 0"),  # 调度优先级
        ("ready_at", "ALTER TABLE tasks ADD COLUMN ready_at TEXT"),          # 最早可执行时间（join 轮询用）
        ("capability", "ALTER TABLE tasks ADD COLUMN capability TEXT"),      # 能力标签：谁能跑这个任务
        ("created_at", "ALTER TABLE tasks ADD COLUMN created_at TEXT"),      # 入队时间（优先级老化用）
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
                priority=0, delay=0, capability=None):
    """把任务放上黑板（pending）。

    workflow    该任务所属工作流名（引擎据此取节点定义）
    priority    基础优先级，数值大的先执行（引擎按节点语义给：人工恢复 100 / 修复 60 / 验收 50 …）
    delay       延迟多少秒之后才可被认领（join 汇合等场景）
    capability  能力标签（llm:fixer / tool:execute_spec / join / human），
                调度器据此把任务只派给会跑的 Worker；None 表示谁都能跑
    """
    task_id = str(uuid.uuid4())[:8]
    now = datetime.now()
    ready_at = (now + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S") if delay else None
    conn = get_conn()
    conn.execute(
        "INSERT INTO tasks (task_id, trace_id, node_id, status, input_context, "
        "retry_count, workflow, priority, ready_at, capability, created_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
        (task_id, trace_id, node_id, context, retry_count, workflow, priority,
         ready_at, capability, now.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    log_event(trace_id, "INFO", f"📝 节点 [{node_id}] 已加入任务黑板")
    return task_id


def claim_task(capabilities=None):
    """原子认领一个可执行任务并置为 running。

    BEGIN IMMEDIATE 立即获取写锁：多个 Worker / 多个浏览器会话同时认领时
    在数据库层串行化，杜绝"先 SELECT 再 UPDATE"两步之间被抢走的重复消费。

    派工规则（三道）：
      1. 能力匹配：只派给会跑这个任务的 Worker（capability 为 NULL 表示不限人手）
      2. 动态优先级：有效优先级 = 基础优先级 + 老化加成；
         等得越久加成越高，最多 AGING_MAX，防止低优先级任务被永久饿死
      3. 同一时刻按入队顺序 FIFO，保证同优先级公平
    """
    caps = [str(c) for c in (capabilities or []) if c]
    now = _now()

    where = ["status='pending'", "(ready_at IS NULL OR ready_at <= ?)"]
    params = [now]
    if caps:
        where.append("(capability IS NULL OR capability IN (%s))" % ",".join("?" * len(caps)))
        params.extend(caps)

    aging = ("MIN(?, MAX(0, CAST((julianday(?) - julianday(COALESCE(created_at, ?)))"
             " * 86400 / ? AS INTEGER)))")
    sql = ("SELECT task_id, trace_id, node_id, input_context, retry_count, workflow "
           "FROM tasks WHERE " + " AND ".join(where)
           + " ORDER BY (COALESCE(priority, 0) + " + aging + ") DESC, rowid LIMIT 1")
    params.extend([AGING_MAX, now, now, AGING_STEP])

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(sql, params).fetchone()
        if row is None:
            conn.rollback()
            return None
        conn.execute("UPDATE tasks SET status='running', started_at=? WHERE task_id=?",
                     (now, row[0]))
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def scheduler_snapshot():
    """调度器观测快照：待执行任务按能力分类的积压与最长等待（前端「调度器」卡用）。"""
    now = _now()
    conn = get_conn()
    try:
        running = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
        rows = conn.execute(
            "SELECT COALESCE(capability, '(不限)') AS cap, COUNT(*) AS n, "
            "  MAX(COALESCE(priority, 0)) AS top, "
            "  CAST((julianday(?) - julianday(MIN(COALESCE(created_at, ?)))) * 86400 AS INTEGER) AS wait_s "
            "FROM tasks WHERE status='pending' GROUP BY cap ORDER BY n DESC",
            (now, now)).fetchall()
    finally:
        conn.close()
    return {
        "running": int(running or 0),
        "buckets": [
            {"capability": r[0], "pending": int(r[1]), "top_priority": int(r[2] or 0),
             "wait_s": int(r[3] or 0)}
            for r in rows
        ],
    }


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

# ---------------- 引擎单写者租约 ----------------

ENGINE_LEASE_TTL = 90          # 心跳超过这个秒数视为持有者已死（引擎每 20 秒续期一次）


def _lease_age_seconds(heartbeat_at):
    try:
        hb = datetime.strptime(str(heartbeat_at), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return 10 ** 9
    return (datetime.now() - hb).total_seconds()


def acquire_engine_lease(owner_pid, ttl=ENGINE_LEASE_TTL):
    """抢占引擎租约。返回 (ok, info)；ok=False 时 info 是占用者信息。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT pid, started_at, heartbeat_at FROM engine_lease WHERE id=1").fetchone()
        now = _now()
        if row and row[0] != owner_pid and _lease_age_seconds(row[2] or row[1]) < ttl:
            conn.rollback()
            return False, {"pid": row[0], "started_at": row[1], "heartbeat_at": row[2]}
        conn.execute(
            "INSERT OR REPLACE INTO engine_lease (id, pid, started_at, heartbeat_at)"
            " VALUES (1, ?, COALESCE((SELECT started_at FROM engine_lease WHERE id=1), ?), ?)",
            (owner_pid, now, now))
        conn.commit()
        return True, {"pid": owner_pid, "started_at": now, "heartbeat_at": now}
    finally:
        conn.close()


def heartbeat_engine_lease(owner_pid):
    conn = get_conn()
    try:
        conn.execute("UPDATE engine_lease SET heartbeat_at=? WHERE id=1 AND pid=?",
                     (_now(), owner_pid))
        conn.commit()
    finally:
        conn.close()


def release_engine_lease(owner_pid):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM engine_lease WHERE id=1 AND pid=?", (owner_pid,))
        conn.commit()
    finally:
        conn.close()
