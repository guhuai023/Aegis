"""Aegis core 包。

导入任何 core.* 之前先做一次运行环境加固：Windows 控制台默认 cp936(GBK)，
遇到日志里的 emoji 会抛 UnicodeEncodeError——这个异常会让 Worker 线程中断、
任务永久停在 running。这里把 stdout/stderr 降级为「不可编码字符替换」，
保证日志再花也不会把引擎打崩。
"""
import sys


def _harden_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # 被重定向到非文本流 / 被测试框架接管时忽略


_harden_console()