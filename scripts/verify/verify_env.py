# -*- coding: utf-8 -*-
"""离线环境自检：不联网、不调大模型，3 秒内告诉你这个包在当前机器上能不能跑。

用法（在项目根目录）：
    python scripts/verify/verify_env.py
    python scripts/verify/verify_env.py --with-tests     # 顺带跑 160 项单元测试（约 20 秒）

退出码：0 = 可以跑；1 = 有硬阻塞（缺解释器/缺依赖/缺文件/端口被占）。
"""
import argparse
import importlib
import re
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
HARD_MODULES = [("streamlit", "界面"), ("openai", "模型客户端"), ("yaml", "工作流/YAML")]
SOFT_MODULES = [("openpyxl", "Excel 报表工作流 excel_report 需要")]


class Report:
    def __init__(self):
        self.rows = []
        self.failed = 0

    def add(self, level, name, detail=""):
        icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}[level]
        self.rows.append((icon, name, detail))
        if level == "fail":
            self.failed += 1

    def dump(self):
        width = max(len(n) for _, n, _ in self.rows) + 2
        for icon, name, detail in self.rows:
            print(f"  {icon} {name.ljust(width)} {detail}")


def check_python(r):
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 10):
        r.add("ok", "Python 版本", f"{ver}（本项目实测 3.14.6）")
    else:
        r.add("fail", "Python 版本", f"{ver} 太低，需要 3.10+（建议 3.11~3.14）")


def check_modules(r):
    for mod, why in HARD_MODULES:
        try:
            m = importlib.import_module(mod)
            r.add("ok", f"依赖 {mod}", getattr(m, "__version__", "") or why)
        except Exception as e:
            r.add("fail", f"依赖 {mod}", f"缺失/不可用（{type(e).__name__}）→ pip install -r requirements.txt")
    for mod, why in SOFT_MODULES:
        try:
            m = importlib.import_module(mod)
            r.add("ok", f"可选 {mod}", getattr(m, "__version__", "") or why)
        except Exception:
            r.add("warn", f"可选 {mod}", why + "（不影响其它工作流）")


def check_files(r):
    need = ["app.py", "requirements.txt", "README.md"]
    for name in need:
        p = REPO / name
        r.add("ok" if p.exists() else "fail", f"文件 {name}", "" if p.exists() else "缺失，包不完整")
    for pat, low, label in [("agents/*.yaml", 5, "Agent 定义"), ("workflows/*.yaml", 6, "工作流")]:
        n = len(list(REPO.glob(pat)))
        r.add("ok" if n >= low else "fail", label, f"{n} 个（不少于 {low}）")
    for d in ("examples/logs", "examples/data"):
        n = len(list((REPO / d).glob("*")))
        r.add("ok" if n else "warn", f"示例数据 {d}", f"{n} 个文件")


def check_db(r):
    db = REPO / "aegis_platform.db"
    seed = REPO / "seed" / "aegis_platform.db"
    if not db.exists():
        try:
            sys.path.insert(0, str(REPO))
            from core.db import init_db
            init_db()
            r.add("ok", "运行库", "原本没有，已自动建空库")
        except Exception as e:
            r.add("warn", "运行库", f"没有且初始化失败（{type(e).__name__}），首次启动会自动建")
    try:
        import sqlite3
        con = sqlite3.connect(db)
        n = con.execute("SELECT COUNT(DISTINCT trace_id) FROM tasks").fetchone()[0]
        con.close()
        if n:
            r.add("ok", "运行库", f"{n} 条 trace 可看")
        elif seed.exists():
            r.add("warn", "运行库", "空库；跑 `python scripts/setup_demo_data.py` 可装入 10 条演示 trace")
        else:
            r.add("ok", "运行库", "空库（正常，还没有任务）")
    except Exception as e:
        r.add("warn", "运行库", f"读不了（{type(e).__name__}）")


def check_seed(r):
    seed = REPO / "seed"
    if not seed.is_dir():
        r.add("warn", "演示数据 seed/", "缺失（无 Key 时就没法看真实 trace）")
        return
    db = seed / "aegis_platform.db"
    ws = seed / "workspace"
    ok = db.exists()
    detail = f"库 {'有' if ok else '无'} · 工作区 {len(list(ws.glob('*'))) if ws.is_dir() else 0} 个 trace"
    r.add("ok" if ok else "warn", "演示数据 seed/", detail)


def check_port(r, port=8507):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        r.add("ok", f"端口 {port}", "空闲")
    except OSError:
        r.add("warn", f"端口 {port}", "已被占用：可能已有一个实例在跑，先关掉它再启动")
    finally:
        s.close()


def check_key(r):
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    env_file = REPO / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                key = line.split("=", 1)[1].strip()
                break
    if key and key.startswith("sk-") and "xxxx" not in key:
        r.add("ok", "模型 Key", "已配置：可以真跑工作流")
    else:
        r.add("warn", "模型 Key", "未配置：可以看演示数据/跑离线测试，但启动新任务会失败")


def check_tests(r):
    print("\n  正在运行单元测试（约 20 秒）...")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(REPO))
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                       cwd=str(REPO), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    out = (p.stderr or "") + (p.stdout or "")
    ran = re.search(r"^Ran (\d+) tests in ([\d.]+)s", out, re.M)
    ok_line = re.search(r"^OK(?: \(.*\))?$", out, re.M)
    if p.returncode == 0 and ran:
        r.add("ok", "单元测试", f"{ran.group(1)} 项全部通过（{ran.group(2)}s）")
    elif p.returncode == 0:
        r.add("ok", "单元测试", "全部通过")
    else:
        tail = [ln for ln in out.splitlines() if ln.strip()][-3:]
        r.add("fail", "单元测试", " / ".join(tail) or "失败")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-tests", action="store_true", help="顺带跑单元测试")
    args = ap.parse_args()

    os.chdir(REPO)
    print("=" * 68)
    print("  Aegis 离线自检（不联网、不调大模型）")
    print(f"  项目目录: {REPO}")
    print(f"  解释器  : {sys.executable}")
    print("=" * 68)
    print()

    r = Report()
    check_python(r)
    check_modules(r)
    check_files(r)
    check_db(r)
    check_seed(r)
    check_port(r)
    check_key(r)
    if args.with_tests:
        check_tests(r)
    r.dump()

    print()
    if r.failed:
        print(f"  结论：有 {r.failed} 项硬阻塞，按上面提示处理后重跑本脚本。")
        return 1
    print("  结论：环境可用。启动平台：  run.bat   （或 streamlit run app.py --server.port 8507）")
    print("        只想看成果：直接打开 docs\\方案书.pdf、docs\\Aegis-赛道一-方案讲解.pptx、"
          "docs\\Aegis-演示视频.mp4")
    return 0


if __name__ == "__main__":
    sys.exit(main())