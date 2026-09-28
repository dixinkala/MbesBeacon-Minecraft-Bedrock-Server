"""
应用日志系统单元测试 - Minecraft 基岩版服务器管理器
覆盖日志器单例、路径一致性、级别设置、最近日志读取与安全异常记录
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAppLogger(unittest.TestCase):
    """app_logger 功能测试（使用临时日志目录隔离）"""

    @classmethod
    def setUpClass(cls):
        # 将日志目录替换为临时目录，避免污染真实用户数据
        import bedrock_server_manager.app_logger as al

        cls._tmp_dir = tempfile.mkdtemp(prefix="mbesbeacon_log_test_")
        cls._orig_dir = al.log_dir
        cls._orig_file = al.log_file
        al.log_dir = cls._tmp_dir
        al.log_file = os.path.join(cls._tmp_dir, "app.log")
        cls._al = al

    @classmethod
    def tearDownClass(cls):
        cls._al.log_dir = cls._orig_dir
        cls._al.log_file = cls._orig_file
        cls._al._reset_logger()

    def setUp(self):
        self.al = self._al
        self.al._reset_logger()

    def tearDown(self):
        self.al._reset_logger()

    def test_singleton_logger(self):
        """get_app_logger 多次调用返回同一实例"""
        logger1 = self.al.get_app_logger()
        logger2 = self.al.get_app_logger()
        self.assertIs(logger1, logger2)

    def test_log_paths_consistent_with_constants(self):
        """日志路径默认值与 constants.LOGS_DIR 一致（P1-3 路径统一）"""
        from bedrock_server_manager.constants import LOGS_DIR

        self.assertEqual(self._orig_dir, LOGS_DIR)
        self.assertEqual(self._orig_file, os.path.join(LOGS_DIR, "app.log"))

    def test_log_file_created_on_init(self):
        """初始化日志器后日志文件可写"""
        logger = self.al.get_app_logger()
        logger.info("测试日志消息")
        self.assertTrue(os.path.exists(self.al.log_file))

    def test_read_recent_logs_returns_lines(self):
        """read_recent_logs 返回最近写入的日志"""
        logger = self.al.get_app_logger()
        for i in range(5):
            logger.info(f"测试消息 {i}")
        recent = self.al.read_recent_logs(lines=10)
        self.assertTrue(any("测试消息 4" in line for line in recent))

    def test_read_recent_logs_missing_file(self):
        """日志文件不存在时返回空列表"""
        missing_file = os.path.join(self._tmp_dir, "nonexistent.log")
        self.al.log_file = missing_file
        try:
            self.assertEqual(self.al.read_recent_logs(), [])
        finally:
            self.al.log_file = os.path.join(self._tmp_dir, "app.log")

    def test_read_recent_logs_empty_lines_filtered(self):
        """空行被过滤"""
        logger = self.al.get_app_logger()
        logger.info("非空消息")
        recent = self.al.read_recent_logs()
        self.assertTrue(all(line.strip() for line in recent))

    def test_set_log_level_valid(self):
        """有效日志级别生效"""
        logger = self.al.get_app_logger()
        self.al.set_log_level("DEBUG")
        self.assertEqual(logger.level, 10)  # logging.DEBUG
        self.al.set_log_level("WARNING")
        self.assertEqual(logger.level, 30)  # logging.WARNING

    def test_set_log_level_invalid(self):
        """无效日志级别保持默认并记录警告"""
        self.al.set_log_level("INVALID_LEVEL")
        self.assertEqual(self.al.get_app_logger().level, 20)  # 默认 INFO 不变

    def test_safe_log_exception_all_levels(self):
        """safe_log_exception 各级别均可记录（不抛异常）"""
        for level in ("debug", "info", "warning", "error"):
            self.al.safe_log_exception("test_module", ValueError(f"错误-{level}"), level=level)
        self.al.safe_log_exception("test_module", ValueError("默认级别"))
        self.assertTrue(os.path.exists(self.al.log_file))

    def test_log_helpers(self):
        """log_* 便捷函数工作"""
        self.al.log_info("info")
        self.al.log_warning("warning")
        self.al.log_error("error")
        self.al.log_debug("debug")
        self.al.log_critical("critical")
        self.assertTrue(os.path.exists(self.al.log_file))

    def test_log_exception_helper(self):
        """log_exception 记录异常（含堆栈）"""
        try:
            raise ValueError("测试异常")
        except ValueError:
            self.al.log_exception("捕获异常")
        self.assertTrue(os.path.exists(self.al.log_file))

    def test_reset_rebuilds_clean_logger(self):
        """_reset_logger 后重建日志器，handler 不累积"""
        first = self.al.get_app_logger()
        handler_count_first = len(first.handlers)
        self.al._reset_logger()
        second = self.al.get_app_logger()
        # logging 按名缓存同一 logger 实例，handler 数量保持一致（重建时清空）
        self.assertEqual(len(second.handlers), handler_count_first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
