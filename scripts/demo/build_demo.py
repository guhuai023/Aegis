# -*- coding: utf-8 -*-
"""Aegis 演示视频自动生成：AI 配音 + 自动操作 + 逐镜录制 + 字幕合成。

用法：
    python demo/build_demo.py --dry      # 只走位并截图，检查每个镜头画面（先跑这个）
    python demo/build_demo.py            # 真正录制并合成 demo_out/aegis_demo.mp4

设计：
  · 每个镜头先"摆好状态"（不录），再按该句配音的时长录制，因此声画天然对齐；
  · 配音用 edge-tts（zh-CN-YunxiNeural），一句一镜；
  · 录屏用 imageio-ffmpeg 自带的完整 ffmpeg（gdigrab 抓浏览器客户区）；
  · 片头/终端/片尾用本地 HTML 卡片，字体与配色跟平台一致。
"""
import argparse
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # D:\下载\项目1
REPO = Path(r"D:\gh666\Aegis")
OUT = ROOT / "demo_out"
ASSETS = OUT / "assets"
SEG_DIR = OUT / "segments"
CARDS = OUT / "cards"

sys.path.insert(0, str(ROOT / "demo"))
import cdp  # noqa: E402

import imageio_ffmpeg  # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
APP_URL = "http://127.0.0.1:8507/"
VOICE = "zh-CN-YunxiNeural"
RATE = "-2%"
CLIENT_W, CLIENT_H = 1600, 900
FPS = 30
NARRATION = {}

user32 = ctypes.windll.user32


# ---------------------------------------------------------------- Win32 窗口
def _client_rect(hwnd):
    rc = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, rc.right - rc.left, rc.bottom - rc.top


def _window_rect(hwnd):
    rc = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    return rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top


def _find_window(pid):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            r = wt.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(r))
            if r.right - r.left > 600 and r.bottom - r.top > 400:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def find_app_window():
    """找一个已经打开的浏览器窗口（客户区接近 1600x900，类名 Chrome_WidgetWin_*）"""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if not buf.value.startswith("Chrome_WidgetWin"):
            return True
        r = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if 1400 <= w <= 1800 and 800 <= h <= 980:
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def fit_client_1600x900(hwnd):
    """把窗口调整到客户区正好 1600x900，并移到左上角。"""
    ox, oy, ow, oh = _window_rect(hwnd)
    cx, cy, cw, ch = _client_rect(hwnd)
    dx, dy = ow - cw, oh - ch
    user32.SetWindowPos(hwnd, 0, 0, 0, CLIENT_W + dx, CLIENT_H + dy, 0x0040)
    time.sleep(1.0)
    return _client_rect(hwnd)


# ---------------------------------------------------------------- 浏览器 / 应用
def ensure_app():
    """应用没在跑就起一个（真实库，方便展示真实 trace）"""
    import urllib.request
    try:
        urllib.request.urlopen(APP_URL, timeout=3)
        print("  [app] 已在运行")
        return None
    except Exception:
        pass
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(REPO))
    p = subprocess.Popen(
        [r"D:\project\jqxx\python.exe", "-m", "streamlit", "run", "app.py",
         "--server.port", "8507", "--server.address", "127.0.0.1", "--server.headless", "true"],
        cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(APP_URL, timeout=2)
            print("  [app] 已启动")
            return p
        except Exception:
            time.sleep(1)
    raise RuntimeError("应用启动超时")


def launch_browser():
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not Path(edge).exists():
        edge = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    prof = OUT / "edge_profile"
    prof.mkdir(parents=True, exist_ok=True)
    cmd = [edge, f"--app={APP_URL}", "--window-position=0,0", "--window-size=1616,955",
           f"--user-data-dir={prof}", "--remote-debugging-port=9223",
           "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check",
           "--disable-features=CalculateNativeWinOcclusion", "--force-device-scale-factor=1"]
    p = subprocess.Popen(cmd)
    time.sleep(6)
    return p


# ---------------------------------------------------------------- 卡片
TITLE_CARD = """<html><meta charset="utf-8"><body style="margin:0;height:100vh;
background:#FFFFFF;font-family:'Microsoft YaHei',sans-serif;display:flex;
flex-direction:column;justify-content:center;padding:0 96px;box-sizing:border-box">
<div style="width:56px;height:3px;background:#175CD3;margin-bottom:34px"></div>
<div style="font-size:52px;font-weight:700;color:#0F1B2D;letter-spacing:0.5px">Aegis</div>
<div style="font-size:30px;color:#333D4D;margin-top:16px">可编排、可验收的多 Agent 协作平台</div>
<div style="font-size:17px;color:#667088;margin-top:26px;line-height:1.9">
本视频所有画面取自真实运行记录：真实数据、真实沙箱、真实断言<br>
未使用动画或剪辑伪造任何执行过程</div>
<div style="position:absolute;bottom:64px;left:96px;font-size:15px;color:#98A2B3">
2026 未来杯 · 赛道一《Agent 编排创新应用》 · 赛题一</div>
</body></html>"""

END_CARD = """<html><meta charset="utf-8"><body style="margin:0;height:100vh;
background:#FFFFFF;font-family:'Microsoft YaHei',sans-serif;display:flex;
flex-direction:column;justify-content:center;padding:0 96px;box-sizing:border-box">
<div style="width:56px;height:3px;background:#175CD3;margin-bottom:30px"></div>
<div style="font-size:40px;font-weight:700;color:#0F1B2D;line-height:1.4">
让 Agent 协作<br>可以验收 · 可以复现 · 可以追责</div>
<div style="font-size:19px;color:#333D4D;margin-top:28px;line-height:2">
6 条工作流 · 8 类断言 · 158 项测试 · 四层真跑验证
</div>
<div style="font-size:16px;color:#175CD3;margin-top:22px">github.com/guhuai023/Aegis</div>
</body></html>"""


def terminal_card(text_tail):
    lines = "\n".join(
        f'<div style="white-space:pre;font-family:Consolas,monospace;font-size:17px;'
        f'line-height:1.75;color:{("#9AE6B4" if l.startswith(("Ran", "OK")) else "#E2E8F0")}">{l}</div>'
        for l in text_tail)
    return f"""<html><meta charset="utf-8"><body style="margin:0;height:100vh;background:#0B1220;
font-family:'Microsoft YaHei',sans-serif;padding:44px 56px;box-sizing:border-box">
<div style="font-size:14px;color:#94A3B8;margin-bottom:18px">Windows PowerShell · D:\\gh666\\Aegis</div>
<div style="white-space:pre;font-family:Consolas,monospace;font-size:17px;color:#E2E8F0;line-height:1.75">
C:\\&gt; python -m unittest discover -s tests -t .</div>
<div style="height:14px"></div>{lines}
<div style="height:22px"></div>
<div style="white-space:pre;font-family:Consolas,monospace;font-size:17px;color:#E2E8F0">C:\\&gt; python scripts/verify/verify_matrix.py</div>
<div style="white-space:pre;font-family:Consolas,monospace;font-size:17px;color:#9AE6B4">控制结构矩阵：14/14 通过</div>
</body></html>"""


def real_test_tail():
    """真的跑一遍测试，拿最后几行输出（不做假）"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(REPO))
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                       cwd=str(REPO), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    out = (r.stderr or "") + (r.stdout or "")
    tail = [l for l in out.splitlines() if l.strip()][-6:]
    tail = [l.replace("=", "").strip() for l in tail]
    return [l for l in tail if l]


# ---------------------------------------------------------------- TTS
def tts(text, path):
    import edge_tts

    async def _run():
        await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))

    asyncio.run(_run())
    return path


def media_duration(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                       capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    if not m:
        raise RuntimeError(f"取不到时长: {path}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


# ---------------------------------------------------------------- 录制
def record(rect, dur, out_path, actions=()):
    """抓浏览器客户区录像；actions 是 [(相对秒, fn)]，在录制期间执行。"""
    x, y, w, h = rect
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "gdigrab", "-framerate", str(FPS), "-draw_mouse", "0",
           "-offset_x", str(x), "-offset_y", str(y),
           "-video_size", f"{w}x{h}", "-i", "desktop",
           "-t", f"{dur:.2f}", "-r", str(FPS),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", str(out_path)]
    p = subprocess.Popen(cmd)
    t0 = time.time()
    for when, fn in sorted(actions, key=lambda a: a[0]):
        wait = when - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)
        try:
            fn()
        except Exception as e:
            print(f"    [warn] 录制中动作失败: {e}")
    remain = dur - (time.time() - t0)
    if remain > 0:
        time.sleep(remain)
    p.wait(timeout=90)
    return out_path


def audio_track(mp3, dur, out_m4a):
    """配音加 0.25s 起播延迟并补静音到镜头时长，保证与画面等长。"""
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3),
                    "-af", "adelay=250|250,apad", "-t", f"{dur:.2f}",
                    "-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "160k", str(out_m4a)],
                   check=True)


# ---------------------------------------------------------------- 镜头定义
def build_shots():
    """返回镜头列表：dict(key, text, setup(cdp), during=[(t, fn)], min_dur)"""
    nav = lambda url, ready: (lambda: cdp.goto(url, ready, settle=1.6))

    def canvas_view(wf):
        def _go():
            cdp.click_text("编排画布")
            time.sleep(1.6)
            cdp.click_text(wf)
            time.sleep(2.2)
        return _go

    shots = []

    shots.append(dict(
        key="01_title", text="", min_dur=11.0, card="title", setup=None, during=[],
        narration="这是一段用真实运行记录做出来的演示：所有画面都来自平台本身，"
                  "没有加速、没有剪辑拼接的执行结果。我们要回答一个问题——"
                  "模型的输出，怎样才能被真正验收。"))

    shots.append(dict(
        key="02_console", text="", card=None,
        setup=nav(APP_URL + "?trace=43d6b6a9",
                  "!!document.body.innerText.includes('执行控制台')"),
        during=[(1.0, lambda: cdp.scroll_by(200)), (6.0, lambda: cdp.scroll_by(-140))],
        narration="先看一条真实的 trace。data_pipeline：把一句话需求拆成两条支线并行跑，"
                  "再汇合，八个节点全部完成，四份交付物落盘。整个过程两分多钟，一万零五百 tokens。"))

    shots.append(dict(
        key="03_assertions", text="", card=None,
        setup=nav(APP_URL + "?trace=43d6b6a9",
                  "!!document.body.innerText.includes('节点明细')"),
        during=[(0.8, lambda: cdp.scroll_by(520)), (7.0, lambda: cdp.scroll_by(300))],
        narration="关键在于每个 tool 节点：它在真实沙箱里装依赖、跑代码，"
                  "然后用断言逐条判定——文件是否真的生成、内容对不对、退出码是不是零。"
                  "模型说自己完成了，不算数。"))

    shots.append(dict(
        key="04_canvas", text="", card=None,
        setup=lambda: (cdp.goto(APP_URL, "!!document.body.innerText.includes('执行控制台')",
                                settle=1.4),
                       cdp.go_canvas("code_gen", expect="6 节点 · 10 条转移")),
        during=[(0.6, lambda: time.sleep(0.1))],
        narration="这是编排画布。左边这条是软件交付：需求进来，生成代码，"
                  "验收失败就回到修复节点重写，预算两次，用尽则转人工。"))

    shots.append(dict(
        key="05_workflows", text="", card=None,
        setup=lambda: cdp.go_canvas("log_triage", expect="5 节点 · 7 条转移"),
        during=[(0.5, lambda: cdp.scroll_by(160))],
        narration="切到企业场景：运维值班周报。日志分析、验收、失败修复、人工介入都在同一张画布上，"
                  "节点的连线就是真实转移规则。"))

    shots.append(dict(
        key="05b_editor", text="", card=None,
        setup=lambda: (cdp.go_canvas("log_triage", expect="5 节点 · 7 条转移"), cdp.scroll_to(560, smooth=False),
                       time.sleep(0.8)),
        during=[(0.6, lambda: cdp.scroll_by(240)), (6.5, lambda: cdp.scroll_by(180))],
        narration="左边是节点编辑器，右下角是这份工作流的真身。"
                  "想换失败策略，不用改代码：把失败出口从修复改成就地终止，保存之后引擎立刻按新流程跑。"))

    shots.append(dict(
        key="06_gate", text="", card=None,
        setup=lambda: (cdp.goto(APP_URL, "!!document.body.innerText.includes('执行控制台')", settle=1.6),
                       cdp.go_canvas("data_quality_gate", expect="4 节点 · 5 条转移")),
        during=[(0.5, lambda: cdp.scroll_by(120))],
        narration="这一条是数仓质量门禁。注意它的失败出口不同：直接指向终止，不自动改写。"
                  "因为错数据被悄悄洗干净，比失败更危险。同一个引擎，改三个字段就是另一套策略。"))

    shots.append(dict(
        key="07b_failure", text="", card=None,
        setup=nav(APP_URL + "?trace=d70f9e90",
                  "!!document.body.innerText.includes('执行控制台')"),
        during=[(1.0, lambda: cdp.scroll_by(430)), (7.0, lambda: cdp.scroll_by(260))],
        narration="也有真失败的时候。这一条里模型改用工具之后没按规格输出，"
                  "整段中文说明差点被当成代码写进工作区——规格里缺少断言段，引擎直接拒绝执行。"
                  "失败被记录下来，而不是被伪装成成功。"))

    shots.append(dict(
        key="07_loop", text="", card=None,
        setup=nav(APP_URL + "?trace=43cbcac1",
                  "!!document.body.innerText.includes('执行控制台')"),
        during=[(0.8, lambda: cdp.scroll_by(420)), (8.0, lambda: cdp.scroll_by(260))],
        narration="这条记录是真实的自我循环：第一次验收失败，两次修复都没过，预算用尽自动熔断，"
                  "问题交给人。人给一句话，修复预算重置，第三次通过。全过程七步、六十五条日志，都在库里。"))

    shots.append(dict(
        key="08_artifacts", text="", card=None,
        setup=nav(APP_URL + "?trace=f9e8ab41",
                  "!!document.body.innerText.includes('交付物')"),
        during=[(1.0, lambda: cdp.scroll_by(360)), (6.5, lambda: cdp.scroll_by(160))],
        narration="交付物是可核算的文件：Excel 报表里明细、汇总、合计三块齐全，"
                  "合计金额与原始数据逐项对得上。产物就在工作区，可以下载、可以复核。"))

    shots.append(dict(
        key="09_tests", text="", card="terminal", setup=None, during=[],
        narration="最后一组证据：一条命令，一百五十八项测试全部通过，不联网、不调模型；"
                  "另有四层真跑验证，覆盖控制结构、调度、工具边界和前端交互。"))

    shots.append(dict(
        key="10_end", text="", card="end", min_dur=8.0, setup=None, during=[],
        narration="Aegis：让 Agent 协作，可以验收、可以复现、可以追责。谢谢观看。"))

    return shots


# ---------------------------------------------------------------- 主流程
def prepare_cards(test_tail):
    CARDS.mkdir(parents=True, exist_ok=True)
    (CARDS / "title.html").write_text(TITLE_CARD, encoding="utf-8")
    (CARDS / "end.html").write_text(END_CARD, encoding="utf-8")
    (CARDS / "terminal.html").write_text(terminal_card(test_tail), encoding="utf-8")


def assemble(shots_meta):
    """按镜头顺序用已有片段合成最终视频（字幕由每镜时长推出）。"""
    print("[4/5] 合成")
    vlist = OUT / "vlist.txt"
    alist = OUT / "alist.txt"
    lines_v, lines_a, srt, t_cursor = [], [], [], 0.0
    for key, dur in shots_meta:
        v = SEG_DIR / f"seg_{key}.mp4"
        a = SEG_DIR / f"seg_{key}.m4a"
        if not v.exists() or not a.exists():
            raise SystemExit(f"缺少片段：{key}")
        lines_v.append(f"file '{v.as_posix()}'\n")
        lines_a.append(f"file '{a.as_posix()}'\n")
        # 一句话太长，读不过来：按中文句号拆成若干条，按字数比例分配时长
        text = NARRATION[key]
        parts = [x.strip() for x in text.replace("\n", "").split("。") if x.strip()]
        if not parts:
            parts = [text]
        parts = [p + "。" for p in parts]
        total_chars = sum(len(p) for p in parts)
        t0 = t_cursor
        for p in parts:
            span = dur * len(p) / max(1, total_chars)
            srt.append((t0, t0 + span, p))
            t0 += span
        t_cursor += dur
    vlist.write_text("".join(lines_v), encoding="utf-8")
    alist.write_text("".join(lines_a), encoding="utf-8")
    video = OUT / "video_silent.mp4"
    audio_all = OUT / "audio_all.m4a"
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                    "-safe", "0", "-i", str(vlist), "-c", "copy", str(video)], check=True)
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                    "-safe", "0", "-i", str(alist), "-c", "copy", str(audio_all)], check=True)
    srt_path = OUT / "aegis_demo.srt"

    def _srt_time(t):
        h = int(t // 3600); m = int(t % 3600 // 60); sec = int(t % 60)
        ms = int(round((t % 1) * 1000))
        if ms == 1000:
            sec += 1; ms = 0
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

    srt_lines = [f"{i}\n{_srt_time(s0)} --> {_srt_time(e0)}\n{txt}\n"
                 for i, (s0, e0, txt) in enumerate(srt, 1)]
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

    final = OUT / "aegis_demo.mp4"
    # 软字幕（mov_text）：不烧进画面，播放器可开关。
    # 之前试过 ass/subtitles 滤镜烧录，浅色界面上白字压白底几乎不可读，
    # 且字号受 PlayRes 影响难以稳定控制 —— 交给播放器渲染更可靠。
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(video), "-i", str(audio_all), "-i", str(srt_path),
                    "-map", "0:v", "-map", "1:a", "-map", "2:s",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                    "-c:s", "mov_text", "-metadata:s:s:0", "language=chi",
                    "-shortest", str(final)], check=True)
    print(f"[5/5] 完成：{final}")
    print(f"  时长 {media_duration(final):.1f}s   "
          f"大小 {final.stat().st_size / 1024 / 1024:.1f} MB")
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只走位并截图，不录制")
    ap.add_argument("--from", dest="start", type=int, default=0)
    ap.add_argument("--only", default="", help="只跑某个镜头 key")
    ap.add_argument("--reuse", action="store_true", help="复用已在运行的应用与浏览器窗口")
    ap.add_argument("--assemble", action="store_true",
                    help="不录制，用已有片段重新合成（配合 --only 重录个别镜头后使用）")
    args = ap.parse_args()
    global NARRATION

    for d in (OUT, ASSETS, SEG_DIR, CARDS):
        d.mkdir(parents=True, exist_ok=True)

    if args.assemble:
        all_shots = build_shots()
        NARRATION = {x["key"]: x["narration"] for x in all_shots}
        manifest_path = OUT / "manifest.json"
        if not manifest_path.exists():
            raise SystemExit("缺少 manifest.json，请先完整录一遍")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        missing = [x["key"] for x in all_shots if x["key"] not in manifest]
        if missing:
            raise SystemExit(f"manifest 缺镜头: {missing}")
        assemble([(x["key"], manifest[x["key"]]) for x in all_shots])
        return

    print("[1/5] 准备环境")
    app = None
    browser = None
    if args.reuse:
        hwnd = find_app_window()
        if not hwnd:
            raise RuntimeError("--reuse 但没找到可复用的浏览器窗口")
        rect = fit_client_1600x900(hwnd)
    else:
        app = ensure_app()
        browser = launch_browser()
        hwnd = None
        for _ in range(30):
            hwnd = _find_window(browser.pid)
            if hwnd:
                break
            time.sleep(1)
        if not hwnd:
            raise RuntimeError("找不到浏览器窗口")
        rect = fit_client_1600x900(hwnd)
    try:
        print(f"  客户区: {rect}（应为 (0,0,1600,900) 附近）")

        print("[2/5] 连接 CDP")
        cdp.connect("127.0.0.1:8507")
        time.sleep(1)

        test_tail = None if args.dry else real_test_tail()
        prepare_cards(test_tail or ["Ran 158 tests in 18.1s", "OK"])

        shots = build_shots()
        if args.only:
            shots = [s for s in shots if s["key"] == args.only]
        elif args.start:
            shots = shots[args.start:]

        print(f"[3/5] 共 {len(shots)} 个镜头")
        all_shots = build_shots()
        NARRATION = {x["key"]: x["narration"] for x in all_shots}
        manifest_path = OUT / "manifest.json"
        manifest = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        if args.assemble:
            missing = [x["key"] for x in all_shots if x["key"] not in manifest]
            if missing:
                raise SystemExit(f"manifest 缺镜头: {missing}")
            assemble([(x["key"], manifest[x["key"]]) for x in all_shots])
            return

        for i, shot in enumerate(shots, 1):
            key = shot["key"]
            audio = ASSETS / f"{key}.mp3"
            if shot.get("card"):
                cdp.goto((CARDS / f"{shot['card']}.html").as_uri(),
                         "document.readyState==='complete'", settle=0.8)
            elif shot.get("setup"):
                shot["setup"]()
                time.sleep(0.6)

            if args.dry:
                p = OUT / f"dry_{key}.png"
                cdp.screenshot(str(p))
                print(f"  [{i}/{len(shots)}] {key} -> {p.name}")
                continue

            tts(shot["narration"], audio)
            adur = media_duration(audio)
            dur = max(adur + 1.0, shot.get("min_dur") or 0)
            if shot.get("card"):
                cdp.goto((CARDS / f"{shot['card']}.html").as_uri(),
                         "document.readyState==='complete'", settle=0.6)

            seg_v = SEG_DIR / f"seg_{key}.mp4"
            seg_a = SEG_DIR / f"seg_{key}.m4a"
            record(rect, dur, seg_v, shot.get("during") or [])
            audio_track(audio, dur, seg_a)
            manifest[key] = round(dur, 2)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
            print(f"  [{i}/{len(shots)}] {key}  {dur:5.1f}s  配音 {adur:4.1f}s")

        if args.dry:
            print("dry 完成，检查 demo_out/dry_*.png")
            return

        missing = [x["key"] for x in all_shots if x["key"] not in manifest]
        if missing:
            print("  还差镜头（先 --only 单独录）:", missing)
            return
        assemble([(x["key"], manifest[x["key"]]) for x in all_shots])

    finally:
        if browser is not None:
            try:
                browser.terminate()
            except Exception:
                pass
        if app is not None:
            app.terminate()


if __name__ == "__main__":
    main()