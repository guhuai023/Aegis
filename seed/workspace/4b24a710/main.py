import csv
import os

EAST_FILE = "east_sales.csv"
SOUTH_FILE = "south_sales.csv"
OUTPUT_FILE = "summary.csv"

FIELDNAMES = ["日期", "渠道", "金额"]


def read_sales(path):
    """读取单个大区的销售明细，返回 [{日期, 渠道, 金额(int)}]。"""
    if not os.path.exists(path):
        print("警告: 文件不存在 %s，按空数据处理" % path)
        return []

    rows = []
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                if not raw:
                    continue
                date = str(raw.get("日期", "") or "").strip()
                channel = str(raw.get("渠道", "") or "").strip()
                amount_raw = str(raw.get("金额", "") or "").strip()
                if not date or not channel or not amount_raw:
                    continue
                try:
                    amount = int(float(amount_raw))
                except (TypeError, ValueError):
                    print("警告: %s 中金额无法解析，已跳过: %r" % (path, amount_raw))
                    continue
                rows.append({"日期": date, "渠道": channel, "金额": amount})
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        print("警告: 读取 %s 失败 (%s)，按空数据处理" % (path, exc))
        return []

    return rows


def main():
    east_rows = read_sales(EAST_FILE)
    south_rows = read_sales(SOUTH_FILE)
    all_rows = east_rows + south_rows

    # 按渠道分组求和
    totals = {}
    for row in all_rows:
        totals[row["渠道"]] = totals.get(row["渠道"], 0) + row["金额"]

    # 渠道顺序：按首次出现顺序，保证输出稳定
    ordered_channels = []
    for row in all_rows:
        if row["渠道"] not in ordered_channels:
            ordered_channels.append(row["渠道"])

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["渠道", "金额"])
        for channel in ordered_channels:
            writer.writerow([channel, totals[channel]])

    grand_total = sum(totals.values())
    print("华东明细行数: %d" % len(east_rows))
    print("华南明细行数: %d" % len(south_rows))
    print("合并明细行数: %d" % len(all_rows))
    print("汇总行数: %d" % len(ordered_channels))
    print("总金额: %.2f" % grand_total)
    print("汇总文件已写入: %s" % OUTPUT_FILE)


if __name__ == "__main__":
    main()