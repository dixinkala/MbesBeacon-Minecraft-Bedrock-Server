"""
性能监控模块单元测试 - Minecraft 基岩版服务器管理器
覆盖 P2-1 增量读取：玩家快照保持、TPS 解析、采集历史与摘要
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 使用不存在的 PID，使 psutil.Process 抛出 NoSuchProcess（OSError 子类），
# 采集逻辑捕获后 CPU/内存保持 0，不依赖真实进程
FAKE_PID = 99999999


class FakeConsole:
    """模拟 ConsoleBuffer 的 read_since 增量语义"""

    def __init__(self, initial_lines=None):
        self.lines = list(initial_lines or [])

    def append(self, text):
        self.lines.append(text)

    def read_since(self, idx):
        if idx < 0:
            idx = 0
        return "".join(self.lines[idx:]), len(self.lines)


def make_server_proc():
    """构造运行中的假服务器进程对象"""
    return SimpleNamespace(running=True, proc=SimpleNamespace(pid=FAKE_PID))


class TestPerformanceCollect(unittest.TestCase):
    """collect 采集逻辑（含 P2-1 增量读取）"""

    def setUp(self):
        from bedrock_server_manager.performance import PerformanceMonitor

        self.monitor = PerformanceMonitor(max_history=100)
        self.console = FakeConsole()
        self.proc = make_server_proc()

    def test_collect_initial_full_read_and_snapshot(self):
        """首次采集读取全部日志并建立玩家快照"""
        self.console.lines = ["[INFO] Server started.", "There are 2/10 players online: Steve, Alex"]
        data = self.monitor.collect(self.proc, self.console)
        self.assertEqual(data["players"], 2)
        self.assertEqual(self.monitor._last_console_idx, 2)
        self.assertEqual(self.monitor._players_snapshot, 2)
        self.assertTrue(data["running"])

    def test_collect_incremental_update_snapshot(self):
        """增量读取：仅读新增日志，玩家列表变化更新快照"""
        self.console.lines = ["[INFO] Server started.", "There are 2/10 players online: Steve, Alex"]
        self.monitor.collect(self.proc, self.console)
        # 追加新日志（玩家增加到 3）
        self.console.append("[INFO] There are 3/10 players online: Steve, Alex, Bob")
        data = self.monitor.collect(self.proc, self.console)
        self.assertEqual(data["players"], 3)
        # 增量位置推进到末尾
        self.assertEqual(self.monitor._last_console_idx, 3)

    def test_collect_no_new_lines_keeps_snapshot(self):
        """无新增日志时玩家快照保持（不错误归零）"""
        self.console.lines = ["[INFO] Server started.", "There are 2/10 players online: Steve, Alex"]
        self.monitor.collect(self.proc, self.console)
        # 无新日志再采集
        data = self.monitor.collect(self.proc, self.console)
        self.assertEqual(data["players"], 2)  # 沿用快照，而非归 0

    def test_collect_console_clear_recovers(self):
        """控制台清空后增量位置安全恢复，新日志可正常解析"""
        self.console.lines = ["[INFO] Server started.", "There are 2/10 players online: Steve, Alex"]
        self.monitor.collect(self.proc, self.console)
        self.console.lines = []  # 模拟 clear
        self.console.append("There are 1/10 players online: Steve")
        data = self.monitor.collect(self.proc, self.console)
        self.assertEqual(data["players"], 1)

    def test_collect_not_running(self):
        """服务器未运行时返回默认数据"""
        self.proc.running = False
        data = self.monitor.collect(self.proc, self.console)
        self.assertFalse(data["running"])
        self.assertEqual(data["players"], 0)
        self.assertEqual(len(self.monitor.get_history()), 1)

    def test_collect_no_console(self):
        """无控制台时玩家数保持快照默认 0"""
        data = self.monitor.collect(self.proc, None)
        self.assertEqual(data["players"], 0)


class TestParseTps(unittest.TestCase):
    """TPS 解析（BDS 默认无输出，预留能力）"""

    def setUp(self):
        from bedrock_server_manager.performance import PerformanceMonitor

        self.monitor = PerformanceMonitor(max_history=100)
        self.proc = make_server_proc()

    def _collect_tps(self, lines):
        console = FakeConsole(lines)
        return self.monitor.collect(self.proc, console)["tps"]

    def test_tps_supported_format(self):
        """预留格式 TPS: 20.0"""
        self.assertEqual(self._collect_tps(["[INFO] TPS: 20.0"]), 20.0)

    def test_tps_average_tick_time(self):
        """平均刻时间转换：50ms → 20 TPS"""
        self.assertEqual(self._collect_tps(["[INFO] Average tick time: 50ms"]), 20.0)

    def test_tps_ticks_per_second(self):
        """ticks per second 格式"""
        self.assertEqual(self._collect_tps(["ticks per second: 15.5"]), 15.5)

    def test_tps_no_output(self):
        """BDS 默认无 TPS 输出 → 0（前端显示暂不支持）"""
        self.assertEqual(self._collect_tps(["[INFO] Server started."]), 0.0)


class TestPerformanceHistory(unittest.TestCase):
    """历史数据与摘要"""

    def setUp(self):
        from bedrock_server_manager.performance import PerformanceMonitor

        self.monitor = PerformanceMonitor(max_history=100)
        self.proc = make_server_proc()
        self.console = FakeConsole()

    def test_get_history_returns_points(self):
        """多次采集后历史点数正确"""
        self.console.append("There are 2/10 players online: Steve, Alex")
        for _ in range(5):
            self.monitor.collect(self.proc, self.console)
        history = self.monitor.get_history()
        self.assertEqual(len(history), 5)

    def test_get_history_limited(self):
        """get_history 限制返回点数"""
        for i in range(10):
            self.console.append(f"line {i}")
            self.monitor.collect(self.proc, self.console)
        history = self.monitor.get_history(points=3)
        self.assertEqual(len(history), 3)

    def test_get_summary_empty(self):
        """无数据时摘要字段齐全且为 0"""
        summary = self.monitor.get_summary()
        self.assertEqual(summary["data_points"], 0)
        self.assertEqual(summary["players_max"], 0)

    def test_reset_clears_state(self):
        """reset 清空历史与增量状态"""
        self.console.append("There are 2/10 players online: Steve, Alex")
        self.monitor.collect(self.proc, self.console)
        self.monitor.reset()
        self.assertEqual(self.monitor.get_history(), [])
        self.assertEqual(self.monitor._last_console_idx, 0)
        self.assertEqual(self.monitor._players_snapshot, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
