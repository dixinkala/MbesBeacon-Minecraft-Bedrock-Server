"""
AppContext 单元测试 - Minecraft 基岩版服务器管理器
覆盖 AppContext 类的所有属性、方法和便捷功能
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bedrock_server_manager.app_context import AppContext


class TestAppContextSingleton(unittest.TestCase):
    """AppContext 单例模式测试"""

    def setUp(self):
        """每个测试前重置单例"""
        AppContext.reset()

    def tearDown(self):
        """每个测试后重置单例"""
        AppContext.reset()

    def test_singleton_instance(self):
        """测试单例模式：多次调用 instance() 返回同一对象"""
        ctx1 = AppContext.instance()
        ctx2 = AppContext.instance()
        self.assertIs(ctx1, ctx2)

    def test_reset_creates_new_instance(self):
        """测试 reset() 后创建新实例"""
        ctx1 = AppContext.instance()
        AppContext.reset()
        ctx2 = AppContext.instance()
        self.assertIsNot(ctx1, ctx2)

    def test_thread_safe_singleton(self):
        """测试线程安全的单例创建"""
        import threading

        results = []
        barrier = threading.Barrier(10)

        def create_instance():
            barrier.wait()
            results.append(AppContext.instance())

        threads = [threading.Thread(target=create_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有线程应该获得同一个实例
        for r in results:
            self.assertIs(results[0], r)


class TestAppContextRuntimeState(unittest.TestCase):
    """运行时状态测试"""

    def setUp(self):
        AppContext.reset()
        self.ctx = AppContext.instance()

    def tearDown(self):
        AppContext.reset()

    def test_api_token(self):
        """测试 API token 属性"""
        # api_token 初始值从 state 模块获取或随机生成，应为非空字符串
        self.assertIsInstance(self.ctx.api_token, str)
        self.assertGreater(len(self.ctx.api_token), 0)
        self.assertTrue(self.ctx.has_api_token)

        # 测试设置为空字符串
        self.ctx.api_token = ""
        self.assertEqual(self.ctx.api_token, "")
        self.assertFalse(self.ctx.has_api_token)

        # 测试设置为自定义 token
        self.ctx.api_token = "test_token_123"
        self.assertEqual(self.ctx.api_token, "test_token_123")
        self.assertTrue(self.ctx.has_api_token)

    def test_app_start_time(self):
        """测试应用启动时间"""
        start_time = self.ctx.app_start_time
        self.assertIsInstance(start_time, float)
        self.assertGreater(start_time, 0)

    def test_uptime(self):
        """测试运行时长"""
        time.sleep(0.1)
        self.assertGreater(self.ctx.uptime_seconds, 0)
        self.assertGreater(self.ctx.uptime_minutes, 0)
        self.assertGreaterEqual(self.ctx.uptime_hours, 0)
        self.assertIsInstance(self.ctx.uptime_str, str)

    def test_mutex_handle(self):
        """测试互斥量句柄"""
        self.assertIsNone(self.ctx.mutex_handle)
        self.ctx.mutex_handle = 12345
        self.assertEqual(self.ctx.mutex_handle, 12345)


class TestAppContextGlobalState(unittest.TestCase):
    """全局单例状态测试"""

    def setUp(self):
        AppContext.reset()
        self.ctx = AppContext.instance()

    def tearDown(self):
        AppContext.reset()

    def test_console(self):
        """测试控制台缓冲区"""
        # console 未初始化时，has_console 应为 False
        self.assertFalse(self.ctx.has_console)
        # console 属性返回 _NullConsole 空对象（不是 None），避免 NoneType 错误
        self.assertIsNotNone(self.ctx.console)
        # 空对象应具有 append 方法
        self.assertTrue(hasattr(self.ctx.console, "append"))

        mock_console = type("MockConsole", (), {"append": lambda self, text: None})()
        self.ctx.console = mock_console
        self.assertIs(self.ctx.console, mock_console)
        self.assertTrue(self.ctx.has_console)

    def test_install_state(self):
        """测试安装状态"""
        # install_state 未初始化时，has_install_state 应为 False
        self.assertFalse(self.ctx.has_install_state)
        self.assertFalse(self.ctx.is_installing)
        # install_state 属性返回 _NullInstallState 空对象（不是 None），避免 NoneType 错误
        self.assertIsNotNone(self.ctx.install_state)
        self.assertFalse(self.ctx.install_state.busy)

        mock_install = type("MockInstall", (), {"busy": True})()
        self.ctx.install_state = mock_install
        self.assertTrue(self.ctx.has_install_state)
        self.assertTrue(self.ctx.is_installing)

    def test_server_proc(self):
        """测试服务器进程"""
        self.assertIsNone(self.ctx.server_proc)
        self.assertFalse(self.ctx.has_server_proc)
        self.assertFalse(self.ctx.is_server_running)

        mock_server = type("MockServer", (), {"running": True})()
        self.ctx.server_proc = mock_server
        self.assertTrue(self.ctx.has_server_proc)
        self.assertTrue(self.ctx.is_server_running)

    def test_server_lock(self):
        """测试服务器进程锁"""
        import threading

        self.assertIsInstance(self.ctx.server_lock, type(threading.Lock()))

    def test_httpd(self):
        """测试 HTTP 服务器"""
        self.assertIsNone(self.ctx.httpd)
        self.assertFalse(self.ctx.has_httpd)

        mock_httpd = object()
        self.ctx.httpd = mock_httpd
        self.assertIs(self.ctx.httpd, mock_httpd)
        self.assertTrue(self.ctx.has_httpd)

    def test_tray(self):
        """测试系统托盘"""
        self.assertIsNone(self.ctx.tray)
        self.assertFalse(self.ctx.has_tray)

        mock_tray = object()
        self.ctx.tray = mock_tray
        self.assertIs(self.ctx.tray, mock_tray)
        self.assertTrue(self.ctx.has_tray)


class TestAppContextSettings(unittest.TestCase):
    """设置管理测试"""

    def setUp(self):
        AppContext.reset()
        self.ctx = AppContext.instance()
        # 重置 server_dir_history，避免测试之间的状态污染
        self.ctx.settings["server_dir_history"] = []

    def tearDown(self):
        AppContext.reset()

    def test_default_settings(self):
        """测试默认设置"""
        self.assertEqual(self.ctx.server_dir, "")
        self.assertFalse(self.ctx.is_server_dir_set)
        self.assertEqual(self.ctx.installed_version, "")
        self.assertFalse(self.ctx.has_installed_version)
        self.assertFalse(self.ctx.ignore_ssl)
        self.assertEqual(self.ctx.server_dir_history, [])

    def test_server_dir(self):
        """测试服务器目录"""
        self.ctx.server_dir = "C:\\MinecraftServer"
        self.assertEqual(self.ctx.server_dir, "C:\\MinecraftServer")
        self.assertTrue(self.ctx.is_server_dir_set)

    def test_installed_version(self):
        """测试已安装版本"""
        self.ctx.installed_version = "1.21.0.03"
        self.assertEqual(self.ctx.installed_version, "1.21.0.03")
        self.assertTrue(self.ctx.has_installed_version)

    def test_ignore_ssl(self):
        """测试忽略 SSL 设置"""
        self.ctx.ignore_ssl = True
        self.assertTrue(self.ctx.ignore_ssl)

    def test_get_setting(self):
        """测试获取设置"""
        self.ctx.set_setting("test_key", "test_value")
        self.assertEqual(self.ctx.get_setting("test_key"), "test_value")
        self.assertEqual(self.ctx.get_setting("nonexistent", "default"), "default")

    def test_set_setting(self):
        """测试设置设置"""
        self.ctx.set_setting("key1", "value1")
        self.assertEqual(self.ctx.settings["key1"], "value1")

    def test_update_settings(self):
        """测试批量更新设置"""
        self.ctx.update_settings({"key1": "value1", "key2": "value2"})
        self.assertEqual(self.ctx.get_setting("key1"), "value1")
        self.assertEqual(self.ctx.get_setting("key2"), "value2")

    def test_add_server_dir_history(self):
        """测试添加服务器目录历史"""
        self.ctx.add_server_dir_history("C:\\server1")
        self.ctx.add_server_dir_history("C:\\server2")
        self.assertEqual(len(self.ctx.server_dir_history), 2)
        self.assertEqual(self.ctx.server_dir_history[0], "C:\\server2")

    def test_add_server_dir_history_dedup(self):
        """测试服务器目录历史去重"""
        self.ctx.add_server_dir_history("C:\\server1")
        self.ctx.add_server_dir_history("C:\\server2")
        self.ctx.add_server_dir_history("C:\\server1")  # 重复
        self.assertEqual(len(self.ctx.server_dir_history), 2)
        self.assertEqual(self.ctx.server_dir_history[0], "C:\\server1")

    def test_add_server_dir_history_empty(self):
        """测试添加空目录到历史"""
        self.ctx.add_server_dir_history("")
        self.assertEqual(len(self.ctx.server_dir_history), 0)

    def test_add_server_dir_history_max(self):
        """测试服务器目录历史数量限制"""
        for i in range(25):
            self.ctx.add_server_dir_history(f"C:\\server{i}")
        self.assertEqual(len(self.ctx.server_dir_history), 20)


class TestAppContextConvenienceMethods(unittest.TestCase):
    """便捷方法测试"""

    def setUp(self):
        AppContext.reset()
        self.ctx = AppContext.instance()

    def tearDown(self):
        AppContext.reset()

    def test_log_without_console(self):
        """测试没有控制台时 log() 不报错"""
        self.ctx.log("test message")  # 应该不报错

    def test_log_with_console(self):
        """测试有控制台时 log() 调用 append"""
        appended = []
        mock_console = type("MockConsole", (), {"append": lambda self, text: appended.append(text)})()
        self.ctx.console = mock_console

        self.ctx.log("test message")
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0], "test message")

    def test_log_info(self):
        """测试 log_info()"""
        appended = []
        mock_console = type("MockConsole", (), {"append": lambda self, text: appended.append(text)})()
        self.ctx.console = mock_console

        self.ctx.log_info("info message")
        self.assertIn("[信息]", appended[0])
        self.assertIn("info message", appended[0])

    def test_log_warning(self):
        """测试 log_warning()"""
        appended = []
        mock_console = type("MockConsole", (), {"append": lambda self, text: appended.append(text)})()
        self.ctx.console = mock_console

        self.ctx.log_warning("warning message")
        self.assertIn("[警告]", appended[0])
        self.assertIn("warning message", appended[0])

    def test_log_error(self):
        """测试 log_error()"""
        appended = []
        mock_console = type("MockConsole", (), {"append": lambda self, text: appended.append(text)})()
        self.ctx.console = mock_console

        self.ctx.log_error("error message")
        self.assertIn("[错误]", appended[0])
        self.assertIn("error message", appended[0])

    def test_is_initialized(self):
        """测试初始化状态"""
        self.assertFalse(self.ctx.is_initialized())
        self.ctx.mark_initialized()
        self.assertTrue(self.ctx.is_initialized())

    def test_mark_initialized_thread_safe(self):
        """测试 mark_initialized 线程安全"""
        import threading

        errors = []

        def mark_init():
            try:
                self.ctx.mark_initialized()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=mark_init) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertTrue(self.ctx.is_initialized())


class TestAppContextPort(unittest.TestCase):
    """端口配置测试"""

    def setUp(self):
        AppContext.reset()
        self.ctx = AppContext.instance()

    def tearDown(self):
        AppContext.reset()

    def test_default_port(self):
        """测试默认端口"""
        self.assertEqual(self.ctx.port, 19100)

    def test_set_port(self):
        """测试设置端口"""
        self.ctx.port = 19101
        self.assertEqual(self.ctx.port, 19101)


class TestAppContextToDict(unittest.TestCase):
    """to_dict 方法测试"""

    def setUp(self):
        AppContext.reset()
        self.ctx = AppContext.instance()

    def tearDown(self):
        AppContext.reset()

    def test_to_dict_keys(self):
        """测试 to_dict 返回的键"""
        state_dict = self.ctx.to_dict()
        expected_keys = [
            "api_token_set",
            "uptime_seconds",
            "uptime_str",
            "port",
            "server_dir",
            "server_dir_set",
            "installed_version",
            "ignore_ssl",
            "console_set",
            "install_state_set",
            "is_installing",
            "server_proc_set",
            "is_server_running",
            "httpd_set",
            "tray_set",
            "initialized",
            "server_dir_history_count",
        ]
        for key in expected_keys:
            self.assertIn(key, state_dict, f"缺少键: {key}")

    def test_to_dict_values(self):
        """测试 to_dict 返回的值"""
        self.ctx.api_token = "test"
        self.ctx.server_dir = "C:\\test"
        self.ctx.installed_version = "1.21.0"
        self.ctx.mark_initialized()

        state_dict = self.ctx.to_dict()
        self.assertTrue(state_dict["api_token_set"])
        self.assertEqual(state_dict["server_dir"], "C:\\test")
        self.assertTrue(state_dict["server_dir_set"])
        self.assertEqual(state_dict["installed_version"], "1.21.0")
        self.assertTrue(state_dict["initialized"])

    def test_to_dict_uptime(self):
        """测试 to_dict 中的运行时长"""
        time.sleep(0.1)
        state_dict = self.ctx.to_dict()
        self.assertGreater(state_dict["uptime_seconds"], 0)
        self.assertIsInstance(state_dict["uptime_str"], str)


class TestAppContextRepr(unittest.TestCase):
    """__repr__ 方法测试"""

    def setUp(self):
        AppContext.reset()
        self.ctx = AppContext.instance()

    def tearDown(self):
        AppContext.reset()

    def test_repr(self):
        """测试 __repr__ 返回值"""
        repr_str = repr(self.ctx)
        self.assertIn("AppContext", repr_str)
        self.assertIn("port=", repr_str)
        self.assertIn("server_dir=", repr_str)
        self.assertIn("is_server_running=", repr_str)

    def test_repr_with_values(self):
        """测试设置值后的 __repr__"""
        self.ctx.server_dir = "C:\\test"
        self.ctx.port = 19101
        repr_str = repr(self.ctx)
        self.assertIn("C:\\test", repr_str)
        self.assertIn("19101", repr_str)


class TestAppContextStateConsistency(unittest.TestCase):
    """P1-1 状态迁移一致性测试：AppContext 与 state 全局变量保持同步"""

    def setUp(self):
        AppContext.reset()

    def tearDown(self):
        AppContext.reset()

    def test_api_token_matches_state(self):
        """AppContext 的 api_token 与 state.API_TOKEN 一致（迁移后仍同源）"""
        from bedrock_server_manager.state import API_TOKEN

        ctx = AppContext.instance()
        self.assertEqual(ctx.api_token, API_TOKEN)

    def test_settings_is_same_object_as_state(self):
        """AppContext.settings 与 state.settings 是同一对象（单一数据源）"""
        from bedrock_server_manager.state import settings

        ctx = AppContext.instance()
        self.assertIs(ctx.settings, settings)
        # 通过 AppContext 写入后 state 可见
        ctx.set_setting("test_key", "value")
        self.assertEqual(settings.get("test_key"), "value")

    def test_app_start_time_matches_state(self):
        """AppContext.app_start_time 与 state.app_start_time 同源"""
        from bedrock_server_manager.state import app_start_time

        ctx = AppContext.instance()
        self.assertAlmostEqual(ctx.app_start_time, app_start_time, delta=2.0)

    def test_console_synced_after_package_init(self):
        """包初始化后模块级导出与 state 全局变量保持同步（桥接正确性）"""
        import bedrock_server_manager as bsm
        from bedrock_server_manager.state import console, install_state

        self.assertIsNotNone(bsm.console)
        self.assertIsNotNone(bsm.install_state)
        self.assertIs(bsm.console, console)
        self.assertIs(bsm.install_state, install_state)

    def test_health_route_uses_context_app_start_time(self):
        """health 路由应使用 AppContext.app_start_time（不再依赖 state 直接引用）"""
        import bedrock_server_manager.web.routes.get_routes as get_routes_mod

        self.assertFalse(hasattr(get_routes_mod, "app_start_time"))
        self.assertGreater(AppContext.instance().app_start_time, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
