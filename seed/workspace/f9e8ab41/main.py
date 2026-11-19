import csv
from collections import OrderedDict

from openpyxl import Workbook, load_workbook

SRC = "data/sales_202610.csv"
OUT = "report_202610.xlsx"


def read_source(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if r and any(c.strip() for c in r)]
    if not rows:
        raise ValueError("源文件为空")
    header = [c.strip() for c in rows[0]]
    data = []
    for r in rows[1:]:
        date, region, channel, amount = r[0].strip(), r[1].strip(), r[2].strip(), r[3].strip()
        data.append([date, region, channel, float(amount)])
    return header, data


def build_workbook(header, data):
    wb = Workbook()

    ws = wb.active
    ws.title = "明细"
    ws.append(header)
    for row in data:
        ws.append(row)
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
        for cell in row:
            cell.number_format = "0.00"

    summary = OrderedDict()
    for row in data:
        summary[row[1]] = summary.get(row[1], 0.0) + row[3]

    ws2 = wb.create_sheet("汇总")
    ws2.append(["大区", "金额"])
    total = 0.0
    for region, amount in summary.items():
        ws2.append([region, round(amount, 2)])
        total += amount
    ws2.append(["合计", round(total, 2)])
    for row in ws2.iter_rows(min_row=2, min_col=2, max_col=2):
        for cell in row:
            cell.number_format = "0.00"

    wb.save(OUT)
    return round(total, 2)


def verify(expected_total):
    wb = load_workbook(OUT, data_only=True)
    names = wb.sheetnames
    assert names == ["明细", "汇总"], "sheet 名不符: %s" % names

    ws = wb["明细"]
    assert ws.max_row == 25, "明细行数不符: %s" % ws.max_row
    assert [c.value for c in ws[1]] == ["日期", "大区", "渠道", "金额"], "明细表头不符"

    ws2 = wb["汇总"]
    assert [c.value for c in ws2[1]] == ["大区", "金额"], "汇总表头不符"
    total_cell = ws2.cell(row=ws2.max_row, column=2).value
    assert abs(float(total_cell) - expected_total) < 0.005, "合计金额不符: %s" % total_cell
    assert ws2.cell(row=ws2.max_row, column=1).value == "合计", "合计行标签不符"
    return names, float(total_cell)


def main():
    header, data = read_source(SRC)
    total = build_workbook(header, data)
    names, read_back_total = verify(total)
    print("校验通过")
    print("总金额: %.2f" % read_back_total)
    print("sheet: %s" % ",".join(names))


if __name__ == "__main__":
    main()