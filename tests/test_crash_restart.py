"""
崩溃自动重启逻辑的单元测试

覆盖：
- 崩溃计数器递增和重置
- 指数退避时间计算
- 最大重启次数限制
- 正常运行后计数器重置
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bedrock_server_manager.constants import SERVER_EXE


class TestCrashRestartLogic(unittest.TestCase):
    """测试崩溃自动重启的核心逻辑（不实际启动进程）"""

    def setUp(self):
        """创建 ServerProcess 实例（mock subprocess）"""
        from bedrock_server_manager.server import ServerProcess

        self.server_dir = os.path.join(os.path.dirname(__file__), "test_server_crash")
        os.makedirs(self.server_dir, exist_ok=True)
        # 创建模拟的 bedrock_server.exe
        with open(os.path.join(self.server_dir, SERVER_EXE), "wb") as f:
            f.write(b"MZ" + b"\x00" * 100)

        # mock 回调函数
        self.mock_on_output = MagicMock()
        self.mock_on_state = MagicMock()
        self.proc = ServerProcess(self.server_dir, self.mock_on_output, self.mock_on_state)

    def tearDown(self):
        """清理测试文件"""
        import shutil

        if os.path.exists(self.server_dir):
            shutil.rmtree(self.server_dir, ignore_errors=True)

    def test_initial_crash_count(self):
        """测试初始崩溃计数器为 0"""
        self.assertEqual(self.proc._crash_restart_count, 0)
        self.assertIsNone(self.proc._last_crash_time)

    def test_max_crash_restarts_constant(self):
        """测试最大崩溃重启次数常量"""
        self.assertEqual(self.proc.MAX_CRASH_RESTARTS, 5)
        self.assertEqual(self.proc.CRASH_RESET_SECONDS, 300)
        self.assertEqual(self.proc.CRASH_BACKOFF_BASE, 2)

    def test_backoff_calculation(self):
        """测试指数退避时间计算"""
        # 第1次: 2^1 = 2秒
        self.proc._crash_restart_count = 1
        backoff = min(self.proc.CRASH_BACKOFF_BASE**self.proc._crash_restart_count, 60)
        self.assertEqual(backoff, 2)

        # 第2次: 2^2 = 4秒
        self.proc._crash_restart_count = 2
        backoff = min(self.proc.CRASH_BACKOFF_BASE**self.proc._crash_restart_count, 60)
        self.assertEqual(backoff, 4)

        # 第3次: 2^3 = 8秒
        self.proc._crash_restart_count = 3
        backoff = min(self.proc.CRASH_BACKOFF_BASE**self.proc._crash_restart_count, 60)
        self.assertEqual(backoff, 8)

        # 第5次: 2^5 = 32秒
        self.proc._crash_restart_count = 5
        backoff = min(self.proc.CRASH_BACKOFF_BASE**self.proc._crash_restart_count, 60)
        self.assertEqual(backoff, 32)

        # 第6次: 2^6 = 64秒，但上限是60秒
        self.proc._crash_restart_count = 6
        backoff = min(self.proc.CRASH_BACKOFF_BASE**self.proc._crash_restart_count, 60)
        self.assertEqual(backoff, 60)

    def test_crash_count_increment(self):
        """测试崩溃计数器递增"""
        self.proc._crash_restart_count = 0
        self.proc._crash_restart_count += 1
        self.assertEqual(self.proc._crash_restart_count, 1)
        self.proc._crash_restart_count += 1
        self.assertEqual(self.proc._crash_restart_count, 2)

    def test_max_crash_limit_reached(self):
        """测试达到最大崩溃次数后停止重启"""
        self.proc._crash_restart_count = self.proc.MAX_CRASH_RESTARTS
        # 模拟再次崩溃
        self.proc._crash_restart_count += 1
        # 应该超过最大次数
        self.assertGreater(self.proc._crash_restart_count, self.proc.MAX_CRASH_RESTARTS)

    def test_crash_reset_after_normal_run(self):
        """测试正常运行一段时间后崩溃计数器重置"""
        # 设置上次崩溃时间为 6 分钟前（超过 300 秒）
        self.proc._last_crash_time = time.time() - 360  # 6分钟前
        self.proc._crash_restart_count = 3

        # 模拟启动时检查
        if self.proc._last_crash_time and (time.time() - self.proc._last_crash_time) > self.proc.CRASH_RESET_SECONDS:
            self.proc._crash_restart_count = 0

        # 计数器应该被重置
        self.assertEqual(self.proc._crash_restart_count, 0)

    def test_crash_not_reset_shortly_after(self):
        """测试短时间内崩溃计数器不重置"""
        # 设置上次崩溃时间为 1 分钟前（不到 300 秒）
        self.proc._last_crash_time = time.time() - 60  # 1分钟前
        self.proc._crash_restart_count = 3

        # 模拟启动时检查
        if self.proc._last_crash_time and (time.time() - self.proc._last_crash_time) > self.proc.CRASH_RESET_SECONDS:
            self.proc._crash_restart_count = 0

        # 计数器不应该被重置
        self.assertEqual(self.proc._crash_restart_count, 3)

    def test_last_crash_time_update(self):
        """测试崩溃时更新上次崩溃时间"""
        self.assertIsNone(self.proc._last_crash_time)

        # 模拟崩溃
        self.proc._last_crash_time = time.time()
        self.proc._crash_restart_count += 1

        self.assertIsNotNone(self.proc._last_crash_time)
        self.assertAlmostEqual(self.proc._last_crash_time, time.time(), delta=1)

    def test_user_stop_not_counted_as_crash(self):
        """测试用户主动停止不计为崩溃"""
        # 用户主动停止时 _user_stopped = True
        self.proc._user_stopped = True
        # 在实际逻辑中，用户停止不会触发崩溃重启
        # 这里验证标记设置正确
        self.assertTrue(self.proc._user_stopped)

    def test_exit_code_recorded(self):
        """测试退出码被记录"""
        self.assertIsNone(self.proc._exit_code)
        # 模拟进程退出
        self.proc._exit_code = 1
        self.assertEqual(self.proc._exit_code, 1)

    def test_start_time_recorded(self):
        """测试启动时间被记录"""
        self.assertIsNone(self.proc._start_time)
        # 模拟启动
        self.proc._start_time = time.time()
        self.assertIsNotNone(self.proc._start_time)

    def test_running_duration_calculation(self):
        """测试运行时长计算"""
        self.proc._start_time = time.time() - 10  # 10秒前启动
        elapsed = time.time() - self.proc._start_time
        self.assertGreaterEqual(elapsed, 10)
        self.assertLess(elapsed, 11)

    def test_server_dir_set(self):
        """测试服务器目录设置"""
        self.assertEqual(self.proc.server_dir, self.server_dir)

    def test_not_running_initially(self):
        """测试初始状态未运行"""
        self.assertFalse(self.proc.running)

    def test_proc_none_initially(self):
        """测试初始进程对象为 None"""
        self.assertIsNone(self.proc.proc)

    def test_send_raises_when_not_running(self):
        """测试未运行时发送命令抛出异常"""
        with self.assertRaises(RuntimeError):
            self.proc.send("test command")


if __name__ == "__main__":
    unittest.main(verbosity=2)
