"""
ConsoleBuffer 单元测试 - Minecraft 基岩版服务器管理器
覆盖控制台缓冲区的所有功能
"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bedrock_server_manager.console import ConsoleBuffer


class TestConsoleBufferInit(unittest.TestCase):
    """初始化测试"""

    def test_default_init(self):
        """测试默认初始化"""
        buf = ConsoleBuffer()
        self.assertEqual(buf.lines, [])
        self.assertIsNone(buf.log_file)
        self.assertEqual(buf.MAX_LINES, 2000)
        self.assertEqual(buf.MAX_FILE_SIZE, 5 * 1024 * 1024)
        self.assertEqual(buf.MAX_LOG_FILES, 3)

    def test_lock_property(self):
        """测试 lock 属性（兼容旧代码）"""
        buf = ConsoleBuffer()
        self.assertIsNotNone(buf.lock)
        # 应该可以作为锁使用
        with buf.lock:
            pass


class TestConsoleBufferAppend(unittest.TestCase):
    """追加日志测试"""

    def setUp(self):
        self.buf = ConsoleBuffer()

    def test_append_single_line(self):
        """测试追加单行日志"""
        self.buf.append("test line\n")
        self.assertEqual(len(self.buf.lines), 1)
        self.assertEqual(self.buf.lines[0], "test line\n")

    def test_append_multiple_lines(self):
        """测试追加多行日志"""
        for i in range(10):
            self.buf.append(f"line {i}\n")
        self.assertEqual(len(self.buf.lines), 10)
        self.assertEqual(self.buf.lines[0], "line 0\n")
        self.assertEqual(self.buf.lines[9], "line 9\n")

    def test_append_max_lines_truncation(self):
        """测试超过最大行数时自动截断"""
        buf = ConsoleBuffer()
        buf.MAX_LINES = 100  # 临时设置较小的最大值
        for i in range(150):
            buf.append(f"line {i}\n")
        self.assertEqual(len(buf.lines), 100)
        # 应该保留最新的 100 行
        self.assertEqual(buf.lines[0], "line 50\n")
        self.assertEqual(buf.lines[99], "line 149\n")

    def test_append_notifies_waiters(self):
        """测试追加日志时通知等待者"""
        buf = ConsoleBuffer()
        results = []

        def waiter():
            text, count = buf.wait_for_new(0, timeout=1.0)
            results.append((text, count))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)  # 等待线程开始等待
        buf.append("test notification\n")
        t.join(timeout=2.0)

        self.assertEqual(len(results), 1)
        self.assertIn("test notification", results[0][0])
        self.assertEqual(results[0][1], 1)


class TestConsoleBufferClear(unittest.TestCase):
    """清空日志测试"""

    def setUp(self):
        self.buf = ConsoleBuffer()
        for i in range(10):
            self.buf.append(f"line {i}\n")

    def test_clear(self):
        """测试清空内存中的日志"""
        self.assertEqual(len(self.buf.lines), 10)
        self.buf.clear()
        self.assertEqual(len(self.buf.lines), 0)

    def test_clear_does_not_delete_file(self):
        """测试清空不删除日志文件"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("existing log\n")
            log_path = f.name

        try:
            self.buf.set_log_file(log_path)
            self.buf.append("new log\n")
            self.buf.clear()
            self.assertEqual(len(self.buf.lines), 0)
            # 文件应该仍然存在
            self.assertTrue(os.path.exists(log_path))
        finally:
            if os.path.exists(log_path):
                os.remove(log_path)


class TestConsoleBufferReadSince(unittest.TestCase):
    """读取日志测试"""

    def setUp(self):
        self.buf = ConsoleBuffer()
        for i in range(10):
            self.buf.append(f"line {i}\n")

    def test_read_since_beginning(self):
        """测试从开头读取"""
        text, count = self.buf.read_since(0)
        self.assertEqual(count, 10)
        self.assertIn("line 0", text)
        self.assertIn("line 9", text)

    def test_read_since_middle(self):
        """测试从中间读取"""
        text, count = self.buf.read_since(5)
        self.assertEqual(count, 10)
        self.assertNotIn("line 0", text)
        self.assertIn("line 5", text)
        self.assertIn("line 9", text)

    def test_read_since_end(self):
        """测试从末尾读取"""
        text, count = self.buf.read_since(10)
        self.assertEqual(count, 10)
        self.assertEqual(text, "")

    def test_read_since_negative_index(self):
        """测试负索引（应该从 0 开始）"""
        text, count = self.buf.read_since(-5)
        self.assertEqual(count, 10)
        self.assertIn("line 0", text)


class TestConsoleBufferWaitForNew(unittest.TestCase):
    """等待新日志测试"""

    def setUp(self):
        self.buf = ConsoleBuffer()

    def test_wait_for_new_with_existing(self):
        """测试有现有日志时立即返回"""
        self.buf.append("existing\n")
        text, count = self.buf.wait_for_new(0, timeout=0.1)
        self.assertIn("existing", text)
        self.assertEqual(count, 1)

    def test_wait_for_new_timeout(self):
        """测试超时时返回空"""
        text, count = self.buf.wait_for_new(0, timeout=0.1)
        self.assertEqual(text, "")
        self.assertEqual(count, 0)

    def test_wait_for_new_negative_index(self):
        """测试负索引"""
        self.buf.append("test\n")
        text, count = self.buf.wait_for_new(-1, timeout=0.1)
        self.assertIn("test", text)

    def test_wait_for_new_after_append(self):
        """测试追加后等待返回新内容"""
        results = []

        def waiter():
            text, count = self.buf.wait_for_new(0, timeout=1.0)
            results.append((text, count))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        self.buf.append("new line\n")
        t.join(timeout=2.0)

        self.assertEqual(len(results), 1)
        self.assertIn("new line", results[0][0])


class TestConsoleBufferLogFile(unittest.TestCase):
    """日志文件测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.log_path = os.path.join(self.temp_dir, "test_console.log")
        self.buf = ConsoleBuffer()

    def tearDown(self):
        import shutil

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_set_log_file(self):
        """测试设置日志文件"""
        self.buf.set_log_file(self.log_path)
        self.assertEqual(self.buf.log_file, self.log_path)

    def test_append_writes_to_file(self):
        """测试追加日志时写入文件"""
        self.buf.set_log_file(self.log_path)
        self.buf.append("test log line\n")

        self.assertTrue(os.path.exists(self.log_path))
        with open(self.log_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("test log line", content)

    def test_log_file_rotation(self):
        """测试日志文件滚动"""
        # 创建一个超过最大大小的文件
        large_content = "x" * (ConsoleBuffer.MAX_FILE_SIZE + 100)
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write(large_content)

        self.buf.set_log_file(self.log_path)
        self.buf.append("new content after rotation\n")

        # 原文件应该被重命名为 .1（注意：os.path.splitext 会生成 test.1.log 而不是 test.log.1）
        base, ext = os.path.splitext(self.log_path)
        rotated_path = f"{base}.1{ext}"
        self.assertTrue(os.path.exists(rotated_path))
        # 新文件应该包含新内容
        with open(self.log_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("new content after rotation", content)

    def test_log_file_rotation_max_files(self):
        """测试日志文件滚动保留最大数量"""
        base, ext = os.path.splitext(self.log_path)
        # 创建多个滚动文件（注意：os.path.splitext 会生成 test.1.log 而不是 test.log.1）
        for i in range(ConsoleBuffer.MAX_LOG_FILES):
            path = self.log_path if i == 0 else f"{base}.{i}{ext}"
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"content {i}\n")

        # 使主文件超过最大大小
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("x" * (ConsoleBuffer.MAX_FILE_SIZE + 100))

        self.buf.set_log_file(self.log_path)
        self.buf.append("new content\n")

        # 滚动后应该只有 MAX_LOG_FILES 个文件（包括主文件）
        log_files = [f for f in os.listdir(self.temp_dir) if f.startswith("test") and f.endswith(".log")]
        self.assertEqual(len(log_files), ConsoleBuffer.MAX_LOG_FILES)
        # 主文件应该包含新内容
        with open(self.log_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("new content", content)


class TestConsoleBufferThreadSafety(unittest.TestCase):
    """线程安全测试"""

    def test_concurrent_appends(self):
        """测试并发追加日志"""
        buf = ConsoleBuffer()
        num_threads = 10
        lines_per_thread = 100

        def append_lines(thread_id):
            for i in range(lines_per_thread):
                buf.append(f"thread {thread_id} line {i}\n")

        threads = [threading.Thread(target=append_lines, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(buf.lines), num_threads * lines_per_thread)

    def test_concurrent_read_and_append(self):
        """测试并发读取和追加"""
        buf = ConsoleBuffer()
        errors = []

        def reader():
            try:
                for _ in range(100):
                    text, count = buf.read_since(0)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def appender():
            try:
                for i in range(100):
                    buf.append(f"line {i}\n")
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=appender)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
