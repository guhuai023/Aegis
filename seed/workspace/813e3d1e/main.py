"""安全除法演示：在除法前显式判断分母，避免 ZeroDivisionError 导致异常退出。"""


def safe_divide(numerator, denominator):
    """返回 (是否成功, 结果或错误信息)。分母为 0 时不抛异常。"""
    if denominator == 0:
        return False, "错误：分母不能为 0，无法进行除法运算"
    return True, numerator / denominator


def main():
    cases = [(10, 2), (7, 0), (-9, 3), (1, 4)]
    exit_code = 0
    for numerator, denominator in cases:
        ok, result = safe_divide(numerator, denominator)
        if ok:
            print(f"{numerator} / {denominator} = {result}")
        else:
            print(f"{numerator} / {denominator} -> {result}")
            exit_code = 0  # 已妥善处理，不视为程序崩溃
    print("全部用例处理完毕，程序正常退出。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


验证结果（真实执行）：
- 退出码：0
- stdout：

10 / 2 = 5.0
7 / 0 -> 错误：分母不能为 0，无法进行除法运算
-9 / 3 = -3.0
1 / 4 = 0.25
全部用例处理完毕，程序正常退出。

- stderr：空
- 单元级验证：`safe_divide(1, 0)` → `(False, '错误：分母不能为 0，无法进行除法运算')`；`safe_divide(1, 2)` → `(True, 0.5)`