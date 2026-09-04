"""
控制台日志模块：ConsoleBuffer、日志缓冲。
"""

import os
import threading

from .app_logger import safe_log_exception


class ConsoleBuffer:
    MAX_LINES = 2000  # 最大保留行数，防止内存无限增长
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 日志文件最大 5MB
    MAX_LOG_FILES = 3  # 滚动保留最近 3 个日志文件

    def __init__(self):
        # 使用 Condition 替代 Lock + Event，避免 set/clear 竞态导致通知丢失
        self._cond = threading.Condition()
        self.lines = []
        self.log_file = None  # 可选：日志文件路径，设置后自动写入文件

    @property
    def lock(self):
        """兼容旧代码：返回 Condition 对象（可作锁使用）。"""
        return self._cond

    def set_log_file(self, path):
        """设置日志文件路径（服务器目录下的 server_console.log）。"""
        with self.lock:
            self.log_file = path

    def clear(self):
        """清空内存中的控制台日志（不删除日志文件）。"""
        with self.lock:
            self.lines = []

    def _rotate_log_file(self, path):
        """滚动日志文件：log.1 → log.2, log → log.1，保留最近 MAX_LOG_FILES 个。"""
        try:
            base, ext = os.path.splitext(path)
            oldest = f"{base}.{self.MAX_LOG_FILES - 1}{ext}"
            if os.path.exists(oldest):
                os.remove(oldest)
            for i in range(self.MAX_LOG_FILES - 2, -1, -1):
                src = f"{base}.{i}{ext}" if i > 0 else path
                dst = f"{base}.{i + 1}{ext}"
                if os.path.exists(src):
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.rename(src, dst)
        except Exception as e:
            safe_log_exception("console", f"操作失败: {e}", "warning")

    def append(self, text):
        with self._cond:
            self.lines.append(text)
            if len(self.lines) > self.MAX_LINES:
                self.lines = self.lines[-self.MAX_LINES :]
            if self.log_file:
                try:
                    if os.path.exists(self.log_file) and os.path.getsize(self.log_file) > self.MAX_FILE_SIZE:
                        self._rotate_log_file(self.log_file)
                    with open(self.log_file, "a", encoding="utf-8") as f:
                        f.write(text)
                except Exception as e:
                    safe_log_exception("console", f"操作失败: {e}", "warning")
            self._cond.notify_all()

    def read_since(self, idx):
        with self.lock:
            if idx < 0:
                idx = 0
            return "".join(self.lines[idx:]), len(self.lines)

    def wait_for_new(self, idx, timeout=0.5):
        """等待新行到达，返回 (new_text, new_count)。超时则返回空。
        使用 Condition 变量，避免 Event set/clear 竞态导致通知丢失。"""
        with self._cond:
            if idx < 0:
                idx = 0
            if len(self.lines) > idx:
                return "".join(self.lines[idx:]), len(self.lines)
            # 无新行，在条件变量上等待（原子释放锁并等待）
            self._cond.wait(timeout=timeout)
            # 被通知或超时后重新检查
            if len(self.lines) > idx:
                return "".join(self.lines[idx:]), len(self.lines)
            return "", len(self.lines)
