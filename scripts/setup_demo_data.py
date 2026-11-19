# -*- coding: utf-8 -*-
"""安装演示数据：把 seed/ 里的 10 条真实运行记录与交付物装进本地运行库。

为什么需要它：运行库 aegis_platform.db 与 workspace/ 都是运行时产物、不进 git，
所以刚解压的包里是空的。没有 DeepSeek Key 的评委靠这份演示数据，
就能在控制台里打开真实 trace、看节点全绿、看产物预览、看熔断转人工的完整过程。

用法：
    python scripts/setup_demo_data.py            # 安装（不覆盖已有运行库）
    python scripts/setup_demo_data.py --force    # 覆盖已有运行库与工作区文件
    python scripts/setup_demo_data.py --list     # 只列出包里有哪些 trace
"""
import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SEED = REPO / "seed"
DB = REPO / "aegis_platform.db"
WS = REPO / "workspace"

TRACE_NOTE = {
    "43d6b6a9": "data_pipeline 全绿：两条支线并行 + 汇合，8 节点完成，4 份交付物",
    "4b24a710": "data_pipeline 第二次全绿（可对比重跑一致性）",
    "82934d70": "code_gen 全绿：需求→代码→物理验收",
    "6ae96fbb": "parallel_demo 并行分支 + 汇合",
    "c670d464": "data_quality_gate 质量门禁：断言写的是业务不变量",
    "d8d78c4f": "log_triage 运维值班周报：日志分析 → 报告验收",
    "f9e8ab41": "excel_report Excel 报表：明细/汇总/合计，xlsx 自校验",
    "d70f9e90": "真实失败：模型没按规格输出、缺断言段，引擎拒绝执行",
    "43cbcac1": "熔断 → 人工一句话 → 预算重置 → 第三次通过（完整人工干预链路）",
    "813e3d1e": "code_gen 人工恢复后继续跑（真实 human 节点恢复）",
}


def list_seed(seed_db):
    if not seed_db.exists():
        print("seed/aegis_platform.db 不存在，包不完整")
        return 1
    con = sqlite3.connect(seed_db)
    rows = list(con.execute(
        "SELECT trace_id, workflow, "
        "SUM(status='done') done, SUM(status='failed') failed, "
        "SUM(status='waiting_human') waiting, COUNT(*) nodes "
        "FROM tasks GROUP BY trace_id, workflow ORDER BY trace_id"))
    con.close()
    print(f"演示数据里有 {len(rows)} 条 trace：\n")
    print(f"  {'trace':10} {'工作流':18} {'完成':>4} {'失败':>4} {'待人工':>5}  说明")
    print("  " + "-" * 92)
    for tid, wf, done, failed, waiting, nodes in rows:
        print(f"  {tid:10} {(wf or '-'):18} {done:>4} {failed:>4} {waiting:>5}  "
              f"{TRACE_NOTE.get(tid, '')}")
    return 0


def install(force=False):
    seed_db = SEED / "aegis_platform.db"
    if not seed_db.exists():
        print("[错误] 没有 seed/aegis_platform.db，无法安装演示数据")
        return 1

    if DB.exists() and not force:
        con = sqlite3.connect(DB)
        n = con.execute("SELECT COUNT(DISTINCT trace_id) FROM tasks").fetchone()[0]
        con.close()
        if n:
            print(f"[跳过] 已有运行库（{n} 条 trace）。要覆盖请加 --force")
            return 0
        print("[提示] 已有空库，直接覆盖为演示库")
    shutil.copy2(seed_db, DB)
    print(f"[1/2] 运行库已安装：{DB.name}")

    seed_ws = SEED / "workspace"
    copied = skipped = 0
    if seed_ws.is_dir():
        for src in seed_ws.rglob("*"):
            if src.is_dir():
                continue
            dst = WS / src.relative_to(seed_ws)
            if dst.exists() and not force:
                skipped += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    print(f"[2/2] 工作区交付物：复制 {copied} 个"
          + (f"，跳过已存在 {skipped} 个" if skipped else ""))
    print()
    print("完成。启动平台（run.bat），左侧「最近任务」里可以打开这些 trace；")
    print("也可以直接访问  http://127.0.0.1:8507/?trace=43d6b6a9")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖已有运行库与工作区文件")
    ap.add_argument("--list", action="store_true", help="只列出演示 trace")
    args = ap.parse_args()
    if args.list:
        return list_seed(SEED / "aegis_platform.db")
    return install(force=args.force)


if __name__ == "__main__":
    sys.exit(main())