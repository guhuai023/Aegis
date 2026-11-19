import csv
import os

EAST_FILE = "east_detail.csv"
SOUTH_FILE = "south_detail.csv"
SUMMARY_FILE = "summary.csv"


def read_detail(path):
    """读取明细文件，返回 [(渠道, 金额), ...]；文件缺失或格式异常时返回空列表。"""
    if not os.path.exists(path):
        print(f"警告: 未找到 {path}，按空数据处理")
        return []
    rows = []
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                print(f"警告: {path} 缺少表头，按空数据处理")
                return []
            for raw in reader:
                channel = (raw.get("渠道") or "").strip()
                amount_text = (raw.get("金额") or "").strip()
                if not channel or not amount_text:
                    continue
                try:
                    amount = float(amount_text)
                except ValueError:
                    print(f"警告: {path} 中金额无法解析: {amount_text!r}，已跳过")
                    continue
                rows.append((channel, amount))
    except (OSError, csv.Error) as exc:
        print(f"警告: 读取 {path} 失败: {exc}，按空数据处理")
        return []
    return rows


def aggregate(rows):
    """按渠道分组求和，保持首次出现顺序。"""
    totals = {}
    for channel, amount in rows:
        totals[channel] = totals.get(channel, 0.0) + amount
    return [(channel, round(amount, 2)) for channel, amount in totals.items()]


def write_summary(path, summary_rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["渠道", "金额"])
        for channel, amount in summary_rows:
            writer.writerow([channel, f"{amount:.2f}"])


def main():
    east_rows = read_detail(EAST_FILE)
    south_rows = read_detail(SOUTH_FILE)
    detail_rows = east_rows + south_rows

    summary_rows = aggregate(detail_rows)
    write_summary(SUMMARY_FILE, summary_rows)

    total = round(sum(amount for _, amount in detail_rows), 2)

    print(f"华东明细: {EAST_FILE} 共 {len(east_rows)} 行")
    print(f"华南明细: {SOUTH_FILE} 共 {len(south_rows)} 行")
    print(f"明细行数: {len(detail_rows)}")
    print(f"汇总渠道数: {len(summary_rows)}")
    print(f"合并总金额: {total:.2f}")
    print(f"汇总文件已生成: {SUMMARY_FILE}")


if __name__ == "__main__":
    main()