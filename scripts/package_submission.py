# -*- coding: utf-8 -*-
"""打包提交物，并在干净目录里自检一遍（模拟评委解压）。

用法：
    python scripts/package_submission.py                # 只打包，产出 dist/Aegis-提交包.zip
    python scripts/package_submission.py --verify       # 打包 + 解压到临时目录跑自检（推荐）
    python scripts/package_submission.py --no-video     # 打一个不含视频的小包（有些平台限大小）

做什么：
  1. 按白名单收集文件（不含 .env / 运行库 / workspace / __pycache__ / .git）
  2. 逐文件算 SHA256，写 MANIFEST.txt 一起放进包
  3. --verify 时解压到 %TEMP%\\aegis_judge_check 这种干净路径，跑 verify_env --with-tests
     和 verify_matrix，确认“评委解压后能跑”，而不是另做一份干净目录
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"
ROOT_IN_ZIP = "Aegis"

INCLUDE_FILES = [
    "app.py", "run.bat", "run.ps1", "requirements.txt", "README.md", ".env.example",
    ".gitattributes", "docs/HOW-TO-RUN.txt",
    "docs/方案书.pdf", "docs/方案书.md", "docs/Aegis-赛道一-方案讲解.pptx",
    "docs/Aegis-演示视频.srt", "docs/提交说明-如何复刻.md",
]
INCLUDE_TREES = [
    "core", "ui", "agents", "workflows", "examples", "tests", "seed",
    ".streamlit", "scripts/verify", "scripts/demo",
]
INCLUDE_EXTRA = ["scripts/setup_demo_data.py"]
VIDEO = "docs/Aegis-演示视频.mp4"
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".venv", "venv", "dist"}
SKIP_SUFFIX = {".pyc", ".pyo", ".docx"}


def collect(with_video=True):
    files, ans = [], []
    for rel in INCLUDE_FILES:
        p = REPO / rel
        if p.exists():
            files.append(p)
        else:
            ans.append(rel)
    for tree in INCLUDE_TREES:
        root = REPO / tree
        if not root.is_dir():
            ans.append(tree + "/")
            continue
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            parts = set(f.relative_to(REPO).parts)
            if parts & SKIP_DIRS or f.suffix in SKIP_SUFFIX:
                continue
            files.append(f)
    for rel in INCLUDE_EXTRA:
        p = REPO / rel
        if p.exists():
            files.append(p)
        else:
            ans.append(rel)
    if with_video:
        p = REPO / VIDEO
        if p.exists():
            files.append(p)
        else:
            ans.append(VIDEO)
    if ans:
        print("[警告] 下面这些没找到，包里会缺：")
        for a in ans:
            print("   -", a)
    return sorted(set(files))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(files, out_zip):
    DIST.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()
    rows = []
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            rel = f.relative_to(REPO).as_posix()
            z.write(f, f"{ROOT_IN_ZIP}/{rel}")
            rows.append((f"{ROOT_IN_ZIP}/{rel}", f.stat().st_size, sha256(f)))
        manifest = [
            "Aegis 提交包清单 / MANIFEST",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"文件数  : {len(rows)}",
            f"原始大小: {sum(r[1] for r in rows) / 1024 / 1024:.2f} MB",
            "格式    : sha256  字节数  相对路径",
            "=" * 96,
        ]
        manifest += [f"{h}  {size:>10}  {name}" for name, size, h in rows]
        z.writestr(f"{ROOT_IN_ZIP}/MANIFEST.txt", "\n".join(manifest) + "\n")
    return rows


def verify(out_zip):
    print("\n[自检] 解压到干净目录，模拟评委首次打开 ...")
    tmp = Path(tempfile.mkdtemp(prefix="aegis_judge_check_"))
    try:
        with zipfile.ZipFile(out_zip) as z:
            z.extractall(tmp)
        root = tmp / ROOT_IN_ZIP
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(root))
        env.pop("DEEPSEEK_API_KEY", None)          # 模拟“没填 Key 的评委”
        py = sys.executable
        print(f"  目录: {root}")
        print(f"  解释器: {py}")

        # 回归检查：run.bat 必须纯 ASCII，run.ps1 必须是 UTF-8 BOM ——
        # 中文 Windows 的 cmd 用 GBK 读批处理，bat 里一旦有非 ASCII 字节，
        # 双击就会把命令结构读坏（真实事故：评委机器上双击报一串乱码）。
        bat = (root / "run.bat").read_bytes()
        ps1 = (root / "run.ps1").read_bytes()
        bad = [(i, b) for i, b in enumerate(bat) if b > 127]
        print(f"  run.bat 非 ASCII 字节: {len(bad)}{'（应为 0）' if bad else '  OK'}")
        print(f"  run.ps1 有 UTF-8 BOM: {ps1.startswith(b'\xef\xbb\xbf')}")
        if bad or not ps1.startswith(b"\xef\xbb\xbf"):
            print("[失败] 启动脚本编码不对，双击 run.bat 会乱码")
            return False
        if b"\r\n" not in bat[:200]:
            print("[警告] run.bat 不是 CRLF，cmd 的 goto/label 可能读错")

        env["AEGIS_NO_PAUSE"] = "1"
        from_env = dict(env, PYTHONPATH=str(root))
        r0 = subprocess.run(["cmd", "/c", "run.bat -DryRun"], cwd=str(root), env=from_env,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=300)
        print("\n--- run.bat -DryRun 输出（尾部）---")
        print("\n".join(r0.stdout.strip().splitlines()[-8:]))
        if r0.returncode != 0 or "is not recognized" in r0.stdout:
            print("[失败] run.bat 在 cmd 下不能正常解析")
            return False

        r1 = subprocess.run([py, "scripts/verify/verify_env.py", "--with-tests"],
                            cwd=str(root), env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=900)
        print("\n--- verify_env 输出（尾部）---")
        print("\n".join(r1.stdout.strip().splitlines()[-16:]))
        if r1.returncode != 0:
            print("[失败] verify_env 返回非 0")
            return False

        # 模拟评委双击 run.bat：装演示数据，然后确认真的有 10 条 trace 可看
        r_setup = subprocess.run([py, "scripts/setup_demo_data.py"],
                                 cwd=str(root), env=env, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=300)
        print("\n--- setup_demo_data 输出 ---")
        print(r_setup.stdout.strip()[-600:])
        if r_setup.returncode != 0:
            print("[失败] 演示数据安装失败")
            return False
        import sqlite3
        con = sqlite3.connect(root / "aegis_platform.db")
        n_trace = con.execute("SELECT COUNT(DISTINCT trace_id) FROM tasks").fetchone()[0]
        n_log = con.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        n_files = len([f for f in (root / "workspace").rglob("*") if f.is_file()])
        con.close()
        print(f"  演示数据检查: {n_trace} 条 trace / {n_log} 条日志 / {n_files} 个交付文件")
        if n_trace < 10:
            print("[失败] 演示 trace 少于 10 条")
            return False

        r2 = subprocess.run([py, "scripts/verify/verify_matrix.py"],
                            cwd=str(root), env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=900)
        tail = "\n".join(r2.stdout.strip().splitlines()[-4:])
        print("\n--- verify_matrix 输出（尾部）---")
        print(tail)
        ok = r2.returncode == 0
        print("\n[自检结果]", "通过：解压即可跑" if ok else "失败：见上面输出")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="打包后解压并自检")
    ap.add_argument("--no-video", action="store_true", help="不包含演示视频")
    ap.add_argument("--name", default=None,
                    help="指定的包名（不含 .zip），例如 \"赛道一+队伍名+Aegis\"；默认按日期生成")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d")
    if args.name:
        out_zip = DIST / f"{args.name}.zip"
    else:
        out_zip = DIST / f"Aegis-提交包-{stamp}{'-无视频' if args.no_video else ''}.zip"

    print("=" * 68)
    print("  打包提交物")
    print("=" * 68)
    files = collect(with_video=not args.no_video)
    rows = build(files, out_zip)
    size = out_zip.stat().st_size

    print(f"\n  输出: {out_zip}")
    print(f"  文件: {len(rows)} 个（原始 {sum(r[1] for r in rows)/1024/1024:.2f} MB → 压缩后 {size/1024/1024:.2f} MB）")
    print("\n  包内一级结构:")
    tops = {}
    for name, sz, _ in rows:
        parts = name.split("/")
        key = parts[1] if len(parts) > 2 else "(根目录)"
        tops[key] = tops.get(key, 0) + sz
    for k in sorted(tops, key=lambda x: -tops[x]):
        print(f"    {k:<22} {tops[k]/1024:>9.1f} KB")

    biggest = sorted(rows, key=lambda r: -r[1])[:5]
    print("\n  最大的 5 个文件:")
    for name, sz, _ in biggest:
        print(f"    {sz/1024/1024:>6.2f} MB  {name}")

    if args.verify:
        ok = verify(out_zip)
        sys.exit(0 if ok else 1)
    print("\n提示：加 --verify 可以解压到干净目录自动自检（推荐提交前跑一次）。")


if __name__ == "__main__":
    main()