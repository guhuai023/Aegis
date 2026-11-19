# -*- coding: utf-8 -*-
"""构建「离线版提交包」用的便携运行时（一次性，产物很大，不进 git）。

产物 runtime/python314/ = 官方嵌入式解释器 + 锁定版本的依赖包。
评委拿到含 runtime 的包后：不需要安装 Python、不需要联网、不需要 pip。

为什么用嵌入式解释器 + 锁定依赖：
- 判分机的 Python 版本未知（可能是 3.11/3.12/3.13），numpy/pandas/pyarrow 这些是
  按小版本编译的 wheel，混用会直接崩；内置解释器把版本问题彻底消掉。
- 依赖版本用 scripts/offline/requirements-lock.txt 固定，保证「装出来的就是验证过的」。

用法（需要联网，开发机执行一次）：
    python scripts/offline/build_offline_runtime.py
    python scripts/offline/build_offline_runtime.py --py 3.14.6 --keep-tests

然后打包：
    python scripts/package_submission.py --offline
"""
import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
OFFLINE = REPO / "scripts" / "offline"
LOCK = OFFLINE / "requirements-lock.txt"
RUNTIME = REPO / "runtime"
PY_DIR_NAME = "python314"


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd))
    r = subprocess.run([str(c) for c in cmd], text=True, encoding="utf-8",
                       errors="replace", **kw)
    if r.returncode != 0:
        raise SystemExit(f"命令失败（{r.returncode}）")
    return r


def download_wheels(py_tag, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.glob("*.whl")):
        print(f"  wheel 已存在（{len(list(out_dir.glob('*.whl')))} 个），跳过下载")
        return
    run([sys.executable, "-m", "pip", "download", "-r", LOCK,
         "--only-binary=:all:", "--python-version", py_tag, "--implementation", "cp",
         "--platform", "win_amd64", "-d", out_dir, "--disable-pip-version-check"])


def get_embed_zip(py_version, cache_dir):
    url = f"https://www.python.org/ftp/python/{py_version}/python-{py_version}-embed-amd64.zip"
    dest = cache_dir / f"python-{py_version}-embed-amd64.zip"
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"  嵌入式解释器已缓存: {dest.name}（{dest.stat().st_size/1e6:.1f} MB）")
        return dest
    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"  下载 {url}")
    urllib.request.urlretrieve(url, dest)
    print(f"  完成 {dest.stat().st_size/1e6:.1f} MB")
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--py", default="3.14.6", help="嵌入式解释器版本（需与 requirements-lock 对应）")
    ap.add_argument("--py-tag", default="314", help="wheel 的 cp 标签，如 314")
    ap.add_argument("--out", default=str(RUNTIME), help="运行时输出目录")
    ap.add_argument("--wheelhouse", default=str(REPO / "wheelhouse" / "cp314-locked"))
    ap.add_argument("--keep-tests", action="store_true", help="保留测试/头文件（体积大很多）")
    args = ap.parse_args()

    out = Path(args.out)
    py_dir = out / PY_DIR_NAME
    wheel_dir = Path(args.wheelhouse)

    print("=" * 66)
    print("  构建便携运行时（离线版）")
    print("=" * 66)

    print("\n[1/5] 下载锁定版本的 wheels")
    download_wheels(args.py_tag, wheel_dir)

    print("\n[2/5] 准备嵌入式解释器")
    embed = get_embed_zip(args.py, REPO / ".cache")
    if py_dir.exists():
        shutil.rmtree(py_dir)
    py_dir.mkdir(parents=True)
    with zipfile.ZipFile(embed) as z:
        z.extractall(py_dir)
    print(f"  解压到 {py_dir}")

    print("\n[3/5] 离线安装依赖（--no-index，不碰网络）")
    target = py_dir / "Lib" / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    run([sys.executable, "-m", "pip", "install", "--no-index",
         "--find-links", wheel_dir, "--target", target, "-r", LOCK,
         "--no-cache-dir", "--disable-pip-version-check", "-q"])

    print("\n[4/5] 配置 ._pth（让嵌入式 Python 认识 Lib\\site-packages）")
    pth = list(py_dir.glob("python*._pth"))
    if not pth:
        raise SystemExit("没找到 ._pth，解释器包不完整")
    pth[0].write_text("Lib\\site-packages\r\npython314.zip\r\n.\r\nimport site\r\n",
                      encoding="ascii")
    print(f"  {pth[0].name} 已改写")

    if not args.keep_tests:
        print("\n[5/5] 剪掉测试/头文件/类型存根（只影响体积，不影响运行）")
        removed = 0
        for f in list(py_dir.rglob("*")):
            if not f.is_file():
                continue
            p = f.as_posix()
            if ("/tests/" in p or "/test/" in p or p.endswith(".pyi")
                    or "/__pycache__/" in p or p.endswith((".c", ".h", ".lib", ".exp"))):
                removed += f.stat().st_size
                f.unlink()
        print(f"  省下 {removed/1024/1024:.1f} MB")
    else:
        print("\n[5/5] 跳过剪枝（--keep-tests）")

    size = sum(f.stat().st_size for f in py_dir.rglob("*") if f.is_file())
    py = py_dir / "python.exe"
    print(f"\n完成: {py_dir}")
    print(f"  大小 {size/1024/1024:.1f} MB   解释器 {'有' if py.exists() else '缺失'}")
    if py.exists():
        r = subprocess.run([str(py), "-c",
                            "import streamlit, openai, yaml, sqlite3, openpyxl;"
                            "print('streamlit', streamlit.__version__)"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(f"  自测: {r.stdout.strip() or r.stderr.strip()[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())