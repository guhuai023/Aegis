# -*- coding: utf-8 -*-
"""断言引擎单元测试（离线、无网络）。

断言词汇表就是平台的验收边界，所以每种类型都要有"能过"和"该失败"两条：
只测通过路径的断言，等于没有断言。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from core import tools


class BaseWs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = tools.WORKSPACES_DIR
        tools.WORKSPACES_DIR = self.tmp
        self.ws = self.tmp / "t1"
        self.ws.mkdir(parents=True)

    def tearDown(self):
        tools.WORKSPACES_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, code, assertions):
        (self.ws / "main.py").write_text(code, encoding="utf-8")
        return tools.run_workspace("t1", assertions)


class TestParse(unittest.TestCase):
    def test_known_kind(self):
        self.assertEqual(tools._parse_assertion("CONTAINS: 9"), ("CONTAINS", "9"))

    def test_chinese_label_falls_back_to_contains(self):
        self.assertEqual(tools._parse_assertion("总金额: 100"), ("CONTAINS", "总金额: 100"))

    def test_typo_kind_is_invalid_not_silently_contains(self):
        """拼错的断言类型必须判失败，不能被当成 CONTAINS 悄悄放过。"""
        self.assertEqual(tools._parse_assertion("CONTANIS: 9")[0], "INVALID")

    def test_regex_file_keeps_double_colon(self):
        kind, arg = tools._parse_assertion("REGEX_FILE: sales.csv :: ^日期,产品$")
        self.assertEqual(kind, "REGEX_FILE")
        self.assertIn("sales.csv ::", arg)


class TestBasicAssertions(BaseWs):
    def test_contains_pass_and_fail(self):
        ok, report, _ = self.run_main("print('answer=42')", ["CONTAINS: answer=42"])
        self.assertTrue(ok, report)
        ok, report, _ = self.run_main("print('answer=41')", ["CONTAINS: answer=42"])
        self.assertFalse(ok)
        self.assertIn("CONTAINS", report)

    def test_not_contains(self):
        ok, report, _ = self.run_main("print('all good')", ["NOT_CONTAINS: Traceback"])
        self.assertTrue(ok, report)

    def test_regex(self):
        ok, _, _ = self.run_main("print('rows=5')", [r"REGEX: rows=\d+"])
        self.assertTrue(ok)

    def test_no_assertions_needs_output(self):
        ok, report, _ = self.run_main("print('hi')", [])
        self.assertTrue(ok, report)
        ok, report, _ = self.run_main("x = 1", [])
        self.assertFalse(ok)
        self.assertIn("没有任何输出", report)

    def test_breakdown_in_report(self):
        ok, report, _ = self.run_main("print('9')", ["CONTAINS: 9", "REGEX: ^9$"])
        self.assertTrue(ok)
        self.assertIn("CONTAINS×1", report)

    def test_nonzero_exit_fails_by_default(self):
        ok, report, _ = self.run_main("raise SystemExit(3)", ["CONTAINS: x"])
        self.assertFalse(ok)
        self.assertIn("退出码 3", report)


class TestExitCodeAssertion(BaseWs):
    def test_exit_code_zero(self):
        ok, report, _ = self.run_main("print('ok')", ["EXIT_CODE: 0"])
        self.assertTrue(ok, report)

    def test_exit_code_expected_nonzero(self):
        """写了 EXIT_CODE 断言后，非 0 退出不再直接判死，而是交给断言裁。"""
        ok, report, _ = self.run_main("raise SystemExit(2)", ["EXIT_CODE: 2"])
        self.assertTrue(ok, report)

    def test_exit_code_mismatch_reports_actual(self):
        ok, report, _ = self.run_main("raise SystemExit(2)", ["EXIT_CODE: 0"])
        self.assertFalse(ok)
        self.assertIn("实际退出码 2", report)


class TestFileAssertions(BaseWs):
    def test_file_exists(self):
        code = "open('out.csv','w',encoding='utf-8').write('a,b\\n1,2\\n')\nprint('done')"
        ok, report, _ = self.run_main(code, ["FILE_EXISTS: out.csv"])
        self.assertTrue(ok, report)

    def test_file_exists_empty_file_rejected(self):
        code = "open('out.csv','w').close()\nprint('done')"
        ok, report, _ = self.run_main(code, ["FILE_EXISTS: out.csv"])
        self.assertFalse(ok)

    def test_file_min_size(self):
        code = "open('out.csv','w',encoding='utf-8').write('a,b\\n1,2\\n')\nprint('ok')"
        ok, _, _ = self.run_main(code, ["FILE_MIN_SIZE: out.csv, 5"])
        self.assertTrue(ok)
        ok, report, _ = self.run_main(code, ["FILE_MIN_SIZE: out.csv, 500"])
        self.assertFalse(ok)
        self.assertIn("B < 要求", report)

    def test_regex_file_checks_content(self):
        """内容断言：文件存在但表头错，必须判失败——只有 FILE_EXISTS 是抓不到的。"""
        code = ("open('out.csv','w',encoding='utf-8').write('date,product\\n2026-01,a\\n')\n"
                "print('rows=1')")
        ok, report, _ = self.run_main(code, [r"REGEX_FILE: out.csv :: ^date,product$"])
        self.assertTrue(ok, report)

        code_bad = ("open('out.csv','w',encoding='utf-8').write('wrong,header\\n')\n"
                    "print('rows=1')")
        ok, report, _ = self.run_main(code_bad, ["REGEX_FILE: out.csv :: ^date,product$"])
        self.assertFalse(ok)
        self.assertIn("REGEX_FILE", report)

    def test_regex_file_missing_file(self):
        ok, report, _ = self.run_main("print('ok')", ["REGEX_FILE: nope.csv :: x"])
        self.assertFalse(ok)
        self.assertIn("文件不存在", report)

    def test_path_escape_rejected(self):
        ok, report, _ = self.run_main("print('ok')", ["FILE_EXISTS: ../outside.txt"])
        self.assertFalse(ok)

    def test_regex_file_format_error(self):
        ok, report, _ = self.run_main("print('ok')", ["REGEX_FILE: out.csv"])
        self.assertFalse(ok)
        self.assertIn("格式应为", report)


class TestNearAssertion(BaseWs):
    def test_near_pass(self):
        ok, report, _ = self.run_main("print('log10(2) =', 0.30103)", ["NEAR: 0.30103, 1e-4"])
        self.assertTrue(ok, report)

    def test_near_tolerance_boundary(self):
        ok, _, _ = self.run_main("print(0.3015)", ["NEAR: 0.30103, 0.001"])
        self.assertTrue(ok)

    def test_near_fail_shows_nearest(self):
        ok, report, _ = self.run_main("print(0.3)", ["NEAR: 0.30103, 1e-6"])
        self.assertFalse(ok)
        self.assertIn("最接近的是 0.3", report)

    def test_near_without_numbers(self):
        ok, report, _ = self.run_main("print('no digits at all')", ["NEAR: 1.0, 0.1"])
        self.assertFalse(ok)

    def test_near_handles_scientific_notation(self):
        ok, _, _ = self.run_main("print(1.5e-3)", ["NEAR: 0.0015, 1e-9"])
        self.assertTrue(ok)


class TestInvalidKind(BaseWs):
    def test_unknown_kind_fails_with_hint(self):
        ok, report, _ = self.run_main("print('9')", ["CONTAINS_XXX: 9"])
        self.assertFalse(ok)
        self.assertIn("未知断言类型", report)

    def test_vocabulary_is_documented(self):
        for kind in ("CONTAINS", "NOT_CONTAINS", "REGEX", "FILE_EXISTS",
                     "FILE_MIN_SIZE", "REGEX_FILE", "EXIT_CODE", "NEAR"):
            self.assertIn(kind, tools.ASSERTION_KINDS)


if __name__ == "__main__":
    unittest.main()