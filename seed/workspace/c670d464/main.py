import csv
import os
import re
from datetime import datetime

SRC = os.path.join("data", "orders_dirty_202611.csv")
CLEAN = "clean_orders.csv"
REPORT = "quality_report.csv"
HEADER = ["order_id", "user_id", "amount", "order_date", "region"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CN_RE = re.compile(r"^[\u4e00-\u9fa5]+$")


def read_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            rows.append({
                "order_id": (r.get("order_id") or "").strip(),
                "user_id": (r.get("user_id") or "").strip(),
                "amount": (r.get("amount") or "").strip(),
                "order_date": (r.get("order_date") or "").strip(),
                "region": (r.get("region") or "").strip(),
            })
        return rows


def parse_date(s):
    s = (s or "").strip()
    if DATE_RE.match(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    for fmt in ("%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def main():
    rows = read_rows(SRC)
    raw_count = len(rows)

    # 1) 重复 order_id：保留首次出现
    seen = set()
    deduped = []
    dup_ids = []
    for r in rows:
        oid = r["order_id"]
        if oid in seen:
            dup_ids.append(oid)
            continue
        seen.add(oid)
        deduped.append(r)

    # 2) 空金额：丢弃
    non_null = []
    null_ids = []
    for r in deduped:
        if r["amount"] == "":
            null_ids.append(r["order_id"])
            continue
        non_null.append(r)

    # 3) 负金额：取绝对值（视为录入错误）
    neg_ids = []
    for r in non_null:
        try:
            val = float(r["amount"])
        except ValueError:
            neg_ids.append(r["order_id"])
            r["amount"] = "0"
            continue
        if val < 0:
            neg_ids.append(r["order_id"])
            val = abs(val)
        r["amount"] = f"{val:.2f}"

    # 4) 日期统一 YYYY-MM-DD
    bad_date_ids = []
    fixed_date_ids = []
    for r in non_null:
        raw = r["order_date"]
        parsed = parse_date(raw)
        if parsed is None:
            bad_date_ids.append(r["order_id"])
            r["order_date"] = ""
        else:
            if not DATE_RE.match(raw):
                fixed_date_ids.append(r["order_id"])
            r["order_date"] = parsed

    # 5) 区域去空格；非法区域标记
    bad_region_ids = []
    for r in non_null:
        region = r["region"].strip()
        if region == "" or not CN_RE.match(region):
            bad_region_ids.append(r["order_id"])
        r["region"] = region if region else "未知"

    # 日期无法修复的行丢弃
    cleaned = [r for r in non_null if r["order_date"] != ""]

    with open(CLEAN, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        for r in cleaned:
            writer.writerow([r["order_id"], r["user_id"], r["amount"],
                             r["order_date"], r["region"]])

    # 清洗后校验
    post_dup = len(cleaned) - len({r["order_id"] for r in cleaned})
    post_null = sum(1 for r in cleaned if r["amount"].strip() == "")

    report_rows = [
        ["重复 order_id", len(dup_ids),
         "按 order_id 去重，保留首次出现；删除 %d 条（%s）" % (
             len(dup_ids), "、".join(dup_ids) if dup_ids else "无")],
        ["空金额", len(null_ids),
         "amount 为空的行直接删除，不填充；删除 %d 条（%s）" % (
             len(null_ids), "、".join(null_ids) if null_ids else "无")],
        ["负金额", len(neg_ids),
         "amount < 0 视为录入错误，取绝对值保留；处理 %d 条（%s）" % (
             len(neg_ids), "、".join(neg_ids) if neg_ids else "无")],
        ["非法日期", len(bad_date_ids) + len(fixed_date_ids),
         "非 YYYY-MM-DD 的日期按 MM/DD/YYYY 与 YYYYMMDD 显式转换，转换失败才删除；"
         "修复 %d 条（%s），无法修复 %d 条" % (
             len(fixed_date_ids), "、".join(fixed_date_ids) if fixed_date_ids else "无",
             len(bad_date_ids))],
        ["区域格式", len(bad_region_ids),
         "region 去首尾空格，空值或含非中文字符的标记为异常；异常 %d 条（%s）" % (
             len(bad_region_ids), "、".join(bad_region_ids) if bad_region_ids else "无")],
    ]

    with open(REPORT, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["检查项", "问题数", "说明"])
        for row in report_rows:
            writer.writerow(row)

    print("原始行数: %d" % raw_count)
    print("清洗后行数: %d" % len(cleaned))
    print("清洗后重复行数: %d" % post_dup)
    print("清洗后空值数: %d" % post_null)


if __name__ == "__main__":
    main()