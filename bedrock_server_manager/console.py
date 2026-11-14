"""
控制台日志模块：ConsoleBuffer、日志缓冲。
"""

import os
import threading

from .app_logger import safe_log_exception


class ConsoleBuffer:
    """控制台日志缓冲区。

    游标语义：对外暴露的 ``count`` 是**累计写入行数的绝对游标**（单调递增），
    而不是当前保留的行数。早期实现把 ``len(lines)`` 当作游标，缓冲区达到
    ``MAX_LINES`` 后长度恒定不变，客户端游标追上后 ``read_since()`` 永久返回空，
    导致控制台与性能面板静默冻结。
    """

    MAX_LINES = 2000  # 最大保留行数，防止内存无限增长
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 日志文件最大 5MB
    MAX_LOG_FILES = 3  # 滚动保留最近 3 个日志文件

    def __init__(self):
        # 使用 Condition 替代 Lock + Event，避免 set/clear 竞态导致通知丢失
        self._cond = threading.Condition()
        self._lines = []
        self._total = 0  # 累计写入行数（绝对游标，永不回退）
        self.log_file = None  # 可选：日志文件路径，设置后自动写入文件

    # ---------------- 兼容旧接口 ----------------

    @property
    def lock(self):
        """兼容旧代码：返回 Condition 对象（可作锁使用）。"""
        return self._cond

    @property
    def lines(self):
        """当前保留的日志行（副本）。"""
        with self._cond:
            return list(self._lines)

    @lines.setter
    def lines(self, value):
        """直接替换日志行时同步游标，避免游标倒挂。

        仅用于测试与兼容场景；正常写入路径应使用 :meth:`append`。
        """
        with self._cond:
            self._lines = list(value or [])
            self._total = max(self._total, len(self._lines))

    @property
    def total(self):
        """累计写入行数（绝对游标）。"""
        with self._cond:
            return self._total

    @property
    def start_index(self):
        """缓冲区中第一行对应的绝对游标。"""
        with self._cond:
            return self._total - len(self._lines)

    def __len__(self):
        with self._cond:
            return len(self._lines)

    def __iter__(self):
        with self._cond:
            return iter(list(self._lines))

    # ---------------- 写入 ----------------

    def set_log_file(self, path):
        """设置日志文件路径（服务器目录下的 server_console.log）。"""
        with self._cond:
            self.log_file = path

    def clear(self):
        """清空内存中的控制台日志（不删除日志文件）。

        ``_total`` 保留不清零：清空后所有旧游标都落在保留窗口之前，
        客户端会收到 reset 而不是把旧日志重放一遍。
        """
        with self._cond:
            self._lines = []

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
            self._lines.append(text)
            self._total += 1
            if len(self._lines) > self.MAX_LINES:
                # 丢弃最旧的行；_total 不回退，游标语义保持单调
                self._lines = self._lines[-self.MAX_LINES :]
            if self.log_file:
                try:
                    if os.path.exists(self.log_file) and os.path.getsize(self.log_file) > self.MAX_FILE_SIZE:
                        self._rotate_log_file(self.log_file)
                    with open(self.log_file, "a", encoding="utf-8") as f:
                        f.write(text)
                except Exception as e:
                    safe_log_exception("console", f"操作失败: {e}", "warning")
            self._cond.notify_all()

    # ---------------- 读取 ----------------

    def _slice_locked(self, idx):
        """把绝对游标转换为列表偏移，返回 (text, new_total)。调用方需持有锁。"""
        if idx < 0:
            idx = 0
        start = self._total - len(self._lines)
        offset = max(0, idx - start)
        return "".join(self._lines[offset:]), self._total

    def read_since(self, idx):
        """读取绝对游标 idx 之后的所有日志，返回 (text, new_index)。

        new_index 是累计写入行数，可安全用作下一次调用的游标；
        若客户端游标已落后于保留窗口，只返回仍保留的部分（不报错）。
        """
        with self._cond:
            return self._slice_locked(idx)

    def wait_for_new(self, idx, timeout=0.5):
        """等待新行到达，返回 (new_text, new_count)。超时则返回空。

        使用 Condition 变量，避免 Event set/clear 竞态导致通知丢失。
        """
        with self._cond:
            if idx < 0:
                idx = 0
            if self._total > idx:
                return self._slice_locked(idx)
            # 无新行，在条件变量上等待（原子释放锁并等待）
            self._cond.wait(timeout=timeout)
            # 被通知或超时后重新检查
            if self._total > idx:
                return self._slice_locked(idx)
            return "", self._total

    def dropped_since(self, idx):
        """判断游标 idx 是否已落后于保留窗口（其间有日志被丢弃/清空）。"""
        with self._cond:
            start = self._total - len(self._lines)
            return idx < start
