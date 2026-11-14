"""B5/B6/A6/A7/A9 回归测试。"""

import os
import shutil
import tempfile
import time
import unittest


class TestVersionComparison(unittest.TestCase):
    """B5：预发布版本不参与比较（方案A）"""

    def test_prerelease_returns_none(self):
        from bedrock_server_manager.app_update import _parse_version

        for v in ("1.0.25-beta", "1.0.25rc1", "1.0.25+build", "abc", ".", "1..2"):
            self.assertIsNone(_parse_version(v), f"{v!r} 应无法解析")

    def test_release_versions_parse(self):
        from bedrock_server_manager.app_update import _parse_version

        self.assertEqual(_parse_version("1.0.22.1"), (1, 0, 22, 1))
        self.assertEqual(_parse_version("v1.0.22"), (1, 0, 22, 0))
        self.assertEqual(_parse_version("V0.1.0.0"), (0, 1, 0, 0))

    def test_prerelease_never_triggers_update(self):
        """预发布 vs 正式版：任何方向都不提示（宁可不提示也不给错结论）"""
        from bedrock_server_manager.app_update import _is_newer

        self.assertFalse(_is_newer("1.0.25-beta", "1.0.25"))
        self.assertFalse(_is_newer("1.0.25", "1.0.25-beta"))
        self.assertFalse(_is_newer("1.0.26-beta", "1.0.25"))

    def test_normal_progression_still_works(self):
        from bedrock_server_manager.app_update import _is_newer

        self.assertTrue(_is_newer("1.0.23", "1.0.22"))
        self.assertTrue(_is_newer("1.0.22.1", "1.0.22"))
        self.assertFalse(_is_newer("1.0.22", "1.0.22.1"))
        self.assertFalse(_is_newer("1.0.22", "1.0.22"))


class TestSingleInstanceMutexName(unittest.TestCase):
    """B6：Windows 互斥量使用 Local\\ 前缀（无需提升权限）"""

    @staticmethod
    def _main_module():
        # 注意：bedrock_server_manager.main 既可能是子模块也可能是包级导出的函数，
        # 用 importlib 明确取模块对象。
        import importlib

        return importlib.import_module("bedrock_server_manager.main")

    def test_mutex_name_is_local(self):
        import inspect

        src = inspect.getsource(self._main_module()._acquire_single_instance)
        self.assertIn("Local\\\\MbesBeacon_Mutex", src)
        self.assertNotIn("Global\\\\MbesBeacon_Mutex", src)

    def test_handle_none_when_create_fails(self):
        """CreateMutexW 返回 NULL 时应返回 None 而不是假装持有锁"""
        from unittest.mock import MagicMock, patch

        main_mod = self._main_module()

        fake_kernel32 = MagicMock()
        fake_kernel32.CreateMutexW.return_value = 0
        fake_kernel32.GetLastError.return_value = 5

        with (
            patch.object(main_mod, "IS_WINDOWS", True),
            patch("ctypes.windll", MagicMock(kernel32=fake_kernel32), create=True),
        ):
            lock, kernel32, stale = main_mod._acquire_single_instance()
        self.assertIsNone(lock)
        self.assertIsNone(kernel32)
        self.assertIsNone(stale)

    def test_already_exists_reports_stale(self):
        """互斥量已存在（ERROR_ALREADY_EXISTS=183）时返回 stale 句柄"""
        from unittest.mock import MagicMock, patch

        main_mod = self._main_module()

        fake_kernel32 = MagicMock()
        fake_kernel32.CreateMutexW.return_value = 12345
        fake_kernel32.GetLastError.return_value = 183

        with (
            patch.object(main_mod, "IS_WINDOWS", True),
            patch("ctypes.windll", MagicMock(kernel32=fake_kernel32), create=True),
        ):
            lock, _kernel32, stale = main_mod._acquire_single_instance()
        self.assertIsNone(lock)
        self.assertEqual(stale, 12345)


class TestHealthEndpointNoPathLeak(unittest.TestCase):
    """A6：免认证的 /api/health 不得返回绝对路径"""

    def test_no_server_dir_in_payload(self):
        from unittest.mock import MagicMock

        from bedrock_server_manager.web.handler import Handler

        h = Handler.__new__(Handler)
        h._json = MagicMock()
        h._send = MagicMock()
        Handler._get_routes["/api/health"]
        getattr(h, Handler._get_routes["/api/health"])({})
        payload = h._json.call_args[0][0]
        self.assertNotIn("server_dir", payload, "health 泄露了服务器绝对路径")
        for key in ("ok", "app", "version", "uptime_seconds", "server_running"):
            self.assertIn(key, payload)


class TestAuditLogRotation(unittest.TestCase):
    """A7：审计日志轮转 + 尾部读取"""

    def setUp(self):
        from bedrock_server_manager import settings

        self._old = settings.get("server_dir", "")
        self.tmp = tempfile.mkdtemp(prefix="mbes_audit_")
        settings["server_dir"] = self.tmp

    def tearDown(self):
        from bedrock_server_manager import settings

        settings["server_dir"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rotation_creates_backup(self):
        from bedrock_server_manager import security

        log = os.path.join(self.tmp, "audit.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write('{"action":"OLD","detail":"x"}\n' * 200)
        orig = security.AUDIT_LOG_MAX_BYTES
        try:
            # 临时把阈值降到 1 字节以触发轮转
            security.AUDIT_LOG_MAX_BYTES = 1
            security.audit_log("TEST_ROTATE", "detail", self.tmp)
        finally:
            security.AUDIT_LOG_MAX_BYTES = orig
        # 轮转命名为 audit.1.log（splitext 后按 base.N.ext 命名）
        rotated = os.path.join(self.tmp, "audit.1.log")
        self.assertTrue(os.path.exists(rotated), f"未生成轮转文件，目录内容: {os.listdir(self.tmp)}")
        self.assertTrue(os.path.exists(log), "新审计日志应被重新创建")
        # 新文件只含轮转后写入的那一条
        with open(log, encoding="utf-8") as f:
            self.assertIn("TEST_ROTATE", f.read())

    def test_read_audit_log_tail(self):
        from bedrock_server_manager import security

        for i in range(50):
            security.audit_log(f"ACTION_{i}", "", self.tmp)
        logs = security.read_audit_log(self.tmp, limit=5)
        self.assertEqual(len(logs), 5)
        self.assertEqual(logs[-1]["action"], "ACTION_49")

    def test_read_empty_returns_empty(self):
        from bedrock_server_manager import security

        self.assertEqual(security.read_audit_log(self.tmp), [])

    def test_limit_zero(self):
        from bedrock_server_manager import security

        security.audit_log("A", "", self.tmp)
        self.assertEqual(security.read_audit_log(self.tmp, limit=0), [])


class TestSchedulerStopIsInterruptible(unittest.TestCase):
    """A9：stop_scheduler 不再最长阻塞 30 秒"""

    def test_stop_returns_fast(self):
        from bedrock_server_manager.scheduler import start_scheduler, stop_scheduler

        start_scheduler()
        t0 = time.time()
        stop_scheduler(timeout=3.0)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 5.0, f"停止耗时 {elapsed:.1f}s，说明 sleep 不可中断")

    def test_stop_is_idempotent(self):
        from bedrock_server_manager.scheduler import stop_scheduler

        stop_scheduler(timeout=0.1)
        stop_scheduler(timeout=0.1)


class TestGetRateLimitApplied(unittest.TestCase):
    """A5：GET 限流已接入 handler"""

    def test_do_get_rejects_after_quota(self):
        from unittest.mock import MagicMock

        from bedrock_server_manager import ratelimit
        from bedrock_server_manager.web.handler import Handler

        limiter = ratelimit.get_rate_limiter()
        limiter.reset()

        h = Handler.__new__(Handler)
        h._json = MagicMock()
        h._send = MagicMock()
        h.headers = {"Host": "127.0.0.1:19100", "X-API-Token": ""}
        h.client_address = ("127.0.0.1", 1234)
        h.rfile = MagicMock()
        h.wfile = MagicMock()
        h.server = None
        h.path = "/api/worlds/export"
        # 用 origin 通过认证
        h.headers["Origin"] = "http://127.0.0.1:19100"

        # 导出端点配额是 3 次/30 秒：打满后再请求应被 429
        for _ in range(4):
            h.do_GET()
        codes = [c.kwargs.get("code") for c in h._json.call_args_list]
        self.assertIn(429, codes, f"未触发 GET 限流，返回码: {codes}")
        limiter.reset()


if __name__ == "__main__":
    unittest.main(verbosity=2)
