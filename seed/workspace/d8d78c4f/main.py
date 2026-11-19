import re
import sys
from collections import Counter

LOG_PATH = "logs/nginx_access_20261118.log"
REPORT_PATH = "report.md"

LINE_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" '
    r'(?P<status>\d{3}) (?P<size>\S+) (?P<rt>[0-9.]+)'
)


def parse_line(line):
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    d = m.groupdict()
    try:
        d["status"] = int(d["status"])
        d["rt"] = float(d["rt"])
    except ValueError:
        return None
    return d


def main():
    records = []
    bad = 0
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = parse_line(line)
                if rec is None:
                    bad += 1
                    continue
                records.append(rec)
    except FileNotFoundError:
        print("日志文件不存在: %s" % LOG_PATH)
        sys.exit(1)

    total = len(records)
    status_counter = Counter(r["status"] for r in records)
    err5xx = [r for r in records if 500 <= r["status"] < 600]
    err5xx_count = len(err5xx)
    err5xx_ratio = (err5xx_count / total * 100.0) if total else 0.0

    ip_counter = Counter(r["ip"] for r in records)
    top_ips = ip_counter.most_common(5)

    slow = sorted([r for r in records if r["rt"] > 1.0], key=lambda x: x["rt"], reverse=True)
    slow_top5 = slow[:5]

    path5xx_counter = Counter(r["path"] for r in err5xx)
    top5xx_paths = path5xx_counter.most_common(3)

    lines = []
    lines.append("# Nginx 访问日志值班周报")
    lines.append("")
    lines.append("数据来源：`%s`" % LOG_PATH)
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("- 总请求数: %d" % total)
    lines.append("- 解析失败行数: %d" % bad)
    lines.append("- 独立来源 IP 数: %d" % len(ip_counter))
    lines.append("- 独立路径数: %d" % len(set(r["path"] for r in records)))
    lines.append("")
    lines.append("## 状态码分布")
    lines.append("")
    lines.append("| 状态码 | 数量 | 占比 |")
    lines.append("| --- | --- | --- |")
    for code, cnt in sorted(status_counter.items()):
        ratio = (cnt / total * 100.0) if total else 0.0
        lines.append("| %d | %d | %.2f%% |" % (code, cnt, ratio))
    lines.append("")
    lines.append("## 5xx 错误")
    lines.append("")
    lines.append("- 5xx 数量: %d" % err5xx_count)
    lines.append("- 5xx 占比: %.2f%%" % err5xx_ratio)
    lines.append("")
    lines.append("## Top5 来源 IP")
    lines.append("")
    lines.append("| 排名 | IP | 请求数 |")
    lines.append("| --- | --- | --- |")
    for i, (ip, cnt) in enumerate(top_ips, 1):
        lines.append("| %d | %s | %d |" % (i, ip, cnt))
    lines.append("")
    lines.append("## 慢请求 Top5（响应时间 > 1s）")
    lines.append("")
    lines.append("- 慢请求总数: %d" % len(slow))
    lines.append("")
    if slow_top5:
        lines.append("| 排名 | 响应时间(s) | 状态码 | 方法 | 路径 | IP |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for i, r in enumerate(slow_top5, 1):
            lines.append("| %d | %.3f | %d | %s | %s | %s |"
                         % (i, r["rt"], r["status"], r["method"], r["path"], r["ip"]))
    else:
        lines.append("无慢请求。")
    lines.append("")
    lines.append("## 5xx 最多的路径 Top3")
    lines.append("")
    if top5xx_paths:
        lines.append("| 排名 | 路径 | 5xx 次数 |")
        lines.append("| --- | --- | --- |")
        for i, (p, cnt) in enumerate(top5xx_paths, 1):
            lines.append("| %d | %s | %d |" % (i, p, cnt))
    else:
        lines.append("无 5xx 请求。")
    lines.append("")

    report = "\n".join(lines)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)

    print("总请求数: %d" % total)
    print("解析失败行数: %d" % bad)
    print("状态码分布: %s" % dict(sorted(status_counter.items())))
    print("5xx 数量: %d" % err5xx_count)
    print("5xx 占比: %.2f%%" % err5xx_ratio)
    print("慢请求(>1s) 数量: %d" % len(slow))
    print("Top5 来源 IP: %s" % top_ips)
    print("5xx 最多路径 Top3: %s" % top5xx_paths)
    print("报告已生成: %s" % REPORT_PATH)


if __name__ == "__main__":
    main()