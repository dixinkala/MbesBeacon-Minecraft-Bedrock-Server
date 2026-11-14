"""C 类工程化项回归：托盘状态、覆盖率/测试配置、CI 与依赖配置一致性。"""

import os
import pathlib
import re
import unittest
from unittest.mock import MagicMock, patch

REPO = pathlib.Path(__file__).resolve().parent.parent


class TestTrayStateTooltip(unittest.TestCase):
    """托盘 tooltip 应随服务器状态变化（NIM_MODIFY）"""

    def setUp(self):
        from bedrock_server_manager.tray import SystemTray

        self.tray = SystemTray("http://127.0.0.1:19100")
        self.tray._nid = MagicMock()
        self.tray._nid.szTip = ""

    def test_state_tooltip_text_differs_by_state(self):
        """文案必须随状态变化，且不依赖界面语言（CI 上语言可能是英文）"""
        running = self.tray._state_tooltip(True)
        stopped = self.tray._state_tooltip(False)
        self.assertNotEqual(running, stopped)
        for text in (running, stopped):
            self.assertTrue(text.strip())
            self.assertIn("MbesBeacon", text)

    @unittest.skipUnless(os.name == "nt", "NIM_MODIFY 仅 Windows")
    def test_tooltip_reflects_running(self):
        from bedrock_server_manager import tray as tray_mod

        with patch.object(tray_mod, "server_running", return_value=True):
            self.tray.refresh_state_tooltip(force=True)
        self.assertEqual(self.tray._nid.szTip, self.tray._state_tooltip(True))

    @unittest.skipUnless(os.name == "nt", "NIM_MODIFY 仅 Windows")
    def test_tooltip_reflects_stopped(self):
        from bedrock_server_manager import tray as tray_mod

        with patch.object(tray_mod, "server_running", return_value=False):
            self.tray.refresh_state_tooltip(force=True)
        self.assertEqual(self.tray._nid.szTip, self.tray._state_tooltip(False))

    @unittest.skipUnless(os.name == "nt", "NIM_MODIFY 仅 Windows")
    def test_uses_nif_tip_flag(self):
        """必须把 uFlags 重置为 NIF_TIP，否则 notify() 留下的 NIF_INFO 会改错字段"""
        from bedrock_server_manager import tray as tray_mod

        self.tray._nid.uFlags = 0x10  # 模拟 notify() 之后的状态
        with patch.object(tray_mod, "server_running", return_value=True):
            self.tray.refresh_state_tooltip(force=True)
        self.assertEqual(self.tray._nid.uFlags, 0x04)

    @unittest.skipUnless(os.name == "nt", "NIM_MODIFY 仅 Windows")
    def test_skips_when_state_unchanged(self):
        """状态未变时不重复调用 shell API"""
        from bedrock_server_manager import tray as tray_mod

        with patch.object(tray_mod, "server_running", return_value=True):
            self.tray.refresh_state_tooltip(force=True)
            self.tray._nid.szTip = "sentinel"
            self.tray.refresh_state_tooltip()  # 状态相同，应直接返回
        self.assertEqual(self.tray._nid.szTip, "sentinel")

    def test_no_nid_does_not_crash(self):
        self.tray._nid = None
        self.tray.refresh_state_tooltip(force=True)  # 不抛异常即可

    def test_i18n_has_state_keys(self):
        from bedrock_server_manager.tray import _TRAY_TEXTS

        for lang in ("zh", "en"):
            self.assertIn("state_running", _TRAY_TEXTS[lang])
            self.assertIn("state_stopped", _TRAY_TEXTS[lang])


class TestPytestConfig(unittest.TestCase):
    """testpaths 应收窄到 tests/，避免误收集根目录 smoke_test.py"""

    def test_testpaths_is_tests_dir(self):
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        block = text.split("[tool.pytest.ini_options]", 1)[1]
        self.assertIn('testpaths = ["tests"]', block)
        self.assertNotIn('testpaths = ["."]', block)

    def test_coverage_gate_configured(self):
        text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.coverage.report]", text)
        m = re.search(r"fail_under\s*=\s*(\d+)", text)
        self.assertIsNotNone(m, "未配置覆盖率下限")
        self.assertGreaterEqual(int(m.group(1)), 50)


class TestCIConfig(unittest.TestCase):
    """CI 应包含前端测试步骤，并把覆盖率门禁交给 pytest"""

    def setUp(self):
        self.text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def test_frontend_job_exists(self):
        self.assertIn("node --test", self.text)
        self.assertIn("tests/js/**/*.test.mjs", self.text)

    def test_build_depends_on_frontend(self):
        m = re.search(r"needs:\s*\[([^\]]+)\]", self.text)
        self.assertIsNotNone(m)
        self.assertIn("frontend", m.group(1))

    def test_tag_push_triggers(self):
        self.assertIn("tags:", self.text)

    def test_coverage_artifact_name_has_os(self):
        self.assertIn("coverage-report-${{ matrix.os }}-${{ matrix.python-version }}", self.text)


class TestDependabotConfig(unittest.TestCase):
    """ignore 需带 update-types，才只屏蔽版本更新而保留安全更新"""

    def setUp(self):
        self.text = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    def test_ignore_rules_scoped_to_version_updates(self):
        # 每个 dependency-name 之后都应紧跟 update-types
        entries = re.findall(r"- dependency-name: \"([^\"]+)\"\n(\s+)", self.text)
        self.assertTrue(entries, "未找到 ignore 规则")
        for name, _indent in entries:
            idx = self.text.index(f'- dependency-name: "{name}"')
            following = self.text[idx : idx + 200]
            self.assertIn("update-types:", following, f"{name} 的 ignore 规则未限定 update-types")

    def test_keeps_pinned_deps(self):
        for dep in ("pyinstaller", "ruff"):
            self.assertIn(dep, self.text)


class TestFrontendTestSuite(unittest.TestCase):
    """前端测试文件应存在并覆盖关键回归点"""

    def setUp(self):
        self.path = REPO / "tests" / "js" / "frontend.test.mjs"

    def test_file_exists(self):
        self.assertTrue(self.path.is_file(), "缺少前端测试文件")

    def test_covers_known_regressions(self):
        src = self.path.read_text(encoding="utf-8")
        for keyword in ("esc", "highlightLog", "packCardHtml", "renderConfig", "showConfirm", "filterConsole"):
            self.assertIn(keyword, src, f"前端测试未覆盖 {keyword}")


class TestNoSilentExceptionSwallow(unittest.TestCase):
    """B2 第 2 步的产物：不应再出现"捕获 Exception 但完全不记日志"的块"""

    def test_no_fully_silent_handlers_in_core(self):
        import ast

        pkg = REPO / "bedrock_server_manager"
        log_calls = {"safe_log_exception", "print", "log_debug", "log_info", "log_warning", "log_error"}
        offenders = []
        for path in pkg.rglob("*.py"):
            if path.name == "app_logger.py":
                continue  # 日志模块自身必须能静默
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if node.type is not None and not (isinstance(node.type, ast.Name) and node.type.id == "Exception"):
                    continue
                logged = False
                for stmt in ast.walk(node):
                    if isinstance(stmt, ast.Raise):
                        logged = True
                    if isinstance(stmt, ast.Call):
                        fn = stmt.func
                        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                        if name in log_calls:
                            logged = True
                if not logged:
                    offenders.append(f"{path.name}:{node.lineno}")
        # 允许少量历史存量，但不能无限增长
        self.assertLessEqual(len(offenders), 15, f"静默吞异常增多: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
