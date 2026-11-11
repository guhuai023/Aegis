import time
import threading
import traceback

from core.db import create_task, log_event, claim_task, finish_task, recover_stale_tasks
from core.agents import ask_agent, router_agent
from core.tools import execute_spec

CODER_ROLE = """你是资深Python开发专家。请严格按以下纯文本格式输出，不要 markdown 标记，不要额外解释：
=== REQUIREMENTS ===
（需要 pip 安装的第三方包，每行一个；没有依赖就留空本节）
=== ASSERTIONS ===
（验收断言，每行一条，格式：CONTAINS: 文本 / NOT_CONTAINS: 文本 / REGEX: 正则；根据任务需求设置有实际区分度的断言）
=== FILE: main.py ===
（入口代码，运行时必须真实执行并产生输出，不允许只定义函数不调用；如需多文件，继续用 === FILE: 文件名 === 追加）"""

FIXER_ROLE = """你是排错专家。请根据全部失败历史修复代码。
输出与开发 Agent 相同的结构化格式（=== REQUIREMENTS === / === ASSERTIONS === / === FILE: 文件名 ===），
所有文件必须全量输出（不是 diff），修复后的代码必须真实执行并满足断言。"""


def _short(text, n=300):
    """压缩日志可读性：单行 + 截断"""
    text = (text or "").strip().replace("\n", " ⏎ ")
    return text[:n] + ("…" if len(text) > n else "")


def _files_to_ctx(files):
    return "\n\n".join(f"=== FILE: {name} ===\n{content}" for name, content in files.items())


def start_engine():
    def worker_loop():
        print("🔥 [引擎] 后台 Worker 线程已启动，正在监听任务黑板！")
        recovered = recover_stale_tasks()
        if recovered:
            print(f"♻️ [引擎] 启动时回收了 {recovered} 个卡死任务")
        while True:
            try:
                _step()
            except Exception as e:
                print(f"🚨 [引擎] 调度异常: {e}\n{traceback.format_exc()}")
                recover_stale_tasks()
            time.sleep(1)

    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()


def _step():
    """单步调度：原子认领 -> 分发到对应节点 -> 终态落库"""
    task = claim_task()
    if not task:
        return
    task_id, trace_id, node_id, context, retry = task
    print(f"🎯 [引擎] 认领任务: {node_id} (task={task_id}, retry={retry})")
    log_event(trace_id, "RUNNING", f"▶️ 开始执行: {node_id}")

    try:
        # ------------------ 核心调度流 ------------------
        if node_id == "router":
            intent = router_agent(context)
            log_event(trace_id, "INFO", f"🧠 识别意图: {intent}")

            # 🌟 Router 分诊完成后必须把自己置为 done，再路由下游节点
            finish_task(task_id, "done", f"已分诊（{intent.strip()}）")

            if "[CODE]" in intent:
                create_task(trace_id, "coder", context, 0)
            else:
                create_task(trace_id, "chat_reply", context, 0)

        elif node_id == "coder":
            spec_text = ask_agent(CODER_ROLE, context)

            # 物理锚点：解析规格 -> 写工作区 -> 装依赖 -> 执行 -> 断言验收
            result = execute_spec(trace_id, spec_text)
            if result.ok:
                finish_task(task_id, "done", result.report)
                log_event(trace_id, "SUCCESS", f"🎉 初次验收通过！{_short(result.report)}")
            else:
                finish_task(task_id, "failed", _short(result.report))
                log_event(trace_id, "ERROR", f"❌ 验收未通过: {_short(result.report)}")
                # 失败历史链：原始需求 + 验收报告 + 当前文件全量交给修复 Agent
                fixer_ctx = (f"原始需求：{context}\n\n【失败记录 #1】\n{result.report}"
                             f"\n\n当前代码文件：\n{_files_to_ctx(result.files)}"
                             "\n\n请输出修复后的完整方案。")
                create_task(trace_id, "fixer", fixer_ctx, retry_count=1)

        elif node_id == "chat_reply":
            # 闲聊节点：Router 判定为非代码任务时兜底应答
            reply = ask_agent("你是友好的AI助手。", context)
            finish_task(task_id, "done", reply)
            log_event(trace_id, "SUCCESS", "💬 闲聊回复完毕")

        elif node_id == "fixer":
            spec_text = ask_agent(FIXER_ROLE, context)

            result = execute_spec(trace_id, spec_text)
            if result.ok:
                finish_task(task_id, "done", result.report)
                log_event(trace_id, "SUCCESS", f"🎉 修复后验收通过！{_short(result.report)}")
            else:
                finish_task(task_id, "failed", _short(result.report))
                log_event(trace_id, "ERROR", f"❌ 修复失败: {_short(result.report)}")

                if retry >= 2:
                    # 熔断：把完整失败链交给人工介入节点
                    human_ctx = (f"{context}\n\n【失败记录 #{retry + 1}】\n{result.report}"
                                 f"\n\n当前代码文件：\n{_files_to_ctx(result.files)}")
                    create_task(trace_id, "human", human_ctx, retry_count=retry)
                else:
                    next_ctx = (f"{context}\n\n【失败记录 #{retry + 1}】\n{result.report}"
                                f"\n\n当前代码文件：\n{_files_to_ctx(result.files)}"
                                "\n\n请输出修复后的完整方案。")
                    create_task(trace_id, "fixer", next_ctx, retry_count=retry + 1)

        elif node_id == "human":
            finish_task(task_id, "waiting_human")
            log_event(trace_id, "HUMAN", "⚠️ 触发熔断，等待人工介入！")

        else:
            finish_task(task_id, "failed", f"未知节点: {node_id}")
            log_event(trace_id, "ERROR", f"❓ 未知节点: {node_id}")

    except Exception as e:
        print(f"🚨 [引擎崩溃] {e}")
        log_event(trace_id, "ERROR", f"系统崩溃: {e}")
        finish_task(task_id, "failed", str(e))
