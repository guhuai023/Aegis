# -*- coding: utf-8 -*-
"""把每个 CDP 抓帧片段校正到 manifest 声明的时长，再合成。"""
import json
import subprocess
from pathlib import Path

import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
OUT = Path(r"D:\下载\项目1\demo_out")
SEG = OUT / "segments"
manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))


def dur_of(p):
    r = subprocess.run([FF, "-hide_banner", "-i", str(p)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    import re
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


for key, want in manifest.items():
    seg = SEG / f"seg_{key}.mp4"
    if not seg.exists():
        print("  跳过（缺片段）", key)
        continue
    cur = dur_of(seg)
    if abs(cur - want) < 0.08:
        print(f"  {key}: {cur:.2f}s ≈ {want:.2f}s 不变")
        continue
    factor = want / cur
    tmp = SEG / f"_tmp_{key}.mp4"
    cmd = [FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(seg),
           "-filter:v", f"setpts={factor:.6f}*PTS", "-r", "15",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", str(tmp)]
    subprocess.run(cmd, check=True)
    tmp.replace(seg)
    print(f"  {key}: {cur:.2f}s → {dur_of(seg):.2f}s（目标 {want:.2f}s）")
print("校正完成")
