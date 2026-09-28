"""
服务器性能监控模块：实时监控服务器进程 CPU/内存占用、在线玩家数、TPS（每秒刻数），
并保留历史数据用于趋势展示。
"""

import re
import threading
import time
from collections import deque

import psutil

from .players import parse_online_players, safe_log_exception


class PerformanceMonitor:
    """服务器性能监控器，收集并保留历史性能数据。"""

    def __init__(self, max_history=300):
        """初始化性能监控器。

        Args:
            max_history: 保留的最大历史数据点数（默认300点，每5秒一点约25分钟）
        """
        self.max_history = max_history
        self._history = deque(maxlen=max_history)
        self._lock = threading.Lock()
        self._last_cpu_time = None
        self._last_cpu_wall_time = None
        self._tps_history = deque(maxlen=60)  # 最近60秒的TPS
        self._last_tick_time = None
        self._tick_count = 0
        self._current_tps = 0.0
        # P2-1 增量读取：记录上次控制台读取位置与玩家快照，避免每 5 秒全文扫描
        self._last_console_idx = 0
        self._players_snapshot = 0

    def collect(self, server_proc=None, console=None):
        """收集一次性能数据。

        Args:
            server_proc: ServerProcess 实例
            console: ConsoleBuffer 实例，用于解析日志中的 TPS

        Returns:
            dict: 性能数据
        """

        data = {
            "timestamp": time.time(),
            "time_str": time.strftime("%H:%M:%S"),
            "cpu_percent": 0.0,
            "memory_mb": 0.0,
            "memory_percent": 0.0,
            "players": 0,
            "tps": 0.0,
            "running": False,
            "uptime_seconds": 0,
        }

        try:
            if server_proc is None or not server_proc.running:
                with self._lock:
                    self._history.append(data)
                return data

            data["running"] = True
            proc = server_proc.proc
            if proc is None:
                with self._lock:
                    self._history.append(data)
                return data

            pid = proc.pid

            # 获取进程启动时间（用于计算运行时长）
            try:
                create_time = proc.create_time() if hasattr(proc, "create_time") else time.time()
                data["uptime_seconds"] = int(time.time() - create_time)
            except (OSError, ValueError, AttributeError) as e:
                safe_log_exception("performance.py", f"性能数据采集失败: {e}", "debug")

            # 获取 CPU 和内存使用情况
            # psutil 为锁定运行时依赖（requirements.txt）；psutil.Error 覆盖
            # NoSuchProcess/AccessDenied，进程刚退出时采集不崩溃
            try:
                p = psutil.Process(pid)
                data["cpu_percent"] = round(
                    p.cpu_percent(interval=None), 1
                )  # 非阻塞模式，首次调用返回0，后续返回自上次调用以来的CPU使用率
                mem_info = p.memory_info()
                data["memory_mb"] = round(mem_info.rss / (1024 * 1024), 1)
                # 计算内存占用百分比
                try:
                    total_mem = psutil.virtual_memory().total
                    data["memory_percent"] = round(mem_info.rss / total_mem * 100, 1)
                except (OSError, ValueError, AttributeError, psutil.Error) as e:
                    safe_log_exception("performance.py", f"性能数据采集失败: {e}", "debug")
            except (OSError, ValueError, AttributeError, psutil.Error) as e:
                safe_log_exception("performance.py", f"性能数据采集失败: {e}", "debug")

            # 获取在线玩家数与 TPS（增量读取控制台日志，不全文扫描）
            # P2-1：仅读取自上次采集以来的新增日志；玩家列表为完整快照输出，
            # 解析到新快照则更新，否则沿用上次值（保持与全文解析等效）。
            recent_text = ""
            if console is not None:
                try:
                    recent_text, new_idx = console.read_since(self._last_console_idx)
                    if new_idx < self._last_console_idx:
                        # 控制台被清空或截断导致索引失效：从头重读
                        recent_text, new_idx = console.read_since(0)
                    self._last_console_idx = new_idx
                except (OSError, ValueError, AttributeError) as e:
                    safe_log_exception("performance.py", f"性能数据采集失败: {e}", "debug")

            # 在线玩家数
            try:
                # 尝试从服务器进程获取在线玩家列表
                if hasattr(server_proc, "get_online_players"):
                    online = server_proc.get_online_players()
                    if online:
                        self._players_snapshot = len(online)
                    data["players"] = self._players_snapshot
                else:
                    # 从控制台日志增量解析玩家列表（快照保持）
                    if console is not None:
                        online = parse_online_players(recent_text)
                        if online:
                            self._players_snapshot = len(online)
                        data["players"] = self._players_snapshot
            except (OSError, ValueError, AttributeError) as e:
                safe_log_exception("performance.py", f"性能数据采集失败: {e}", "debug")

            # 解析 TPS（从增量控制台日志；BDS 默认无 TPS 输出时返回 0）
            data["tps"] = self._parse_tps(recent_text)

        except (OSError, ValueError, AttributeError) as e:
            safe_log_exception("performance.py", f"性能数据采集失败: {e}", "debug")

        with self._lock:
            self._history.append(data)

        return data

    def _parse_tps(self, recent_text=None):
        """从控制台日志增量文本解析 TPS（每秒刻数）。

        注意：Minecraft 基岩版专用服务器（BDS）默认不输出 TPS 信息到控制台，
        因此 TPS 字段通常为 0。如果未来 BDS 版本支持 TPS 输出，此方法会自动解析。
        支持的格式："TPS: 20.0"、"ticks per second: 20.0"、"Average tick time: 50ms"。
        """
        if recent_text is None or recent_text == "":
            return self._current_tps

        try:
            # 匹配 TPS 相关输出（BDS 目前不支持，预留解析能力）
            tps_patterns = [
                r"TPS[:\s]+([\d.]+)",
                r"ticks per second[:\s]+([\d.]+)",
                r"Average tick time[:\s]+([\d.]+)ms",
            ]
            for pattern in tps_patterns:
                matches = re.findall(pattern, recent_text, re.IGNORECASE)
                if matches:
                    try:
                        tps = float(matches[-1])
                        # 如果是平均刻时间（ms），转换为 TPS
                        if "tick time" in pattern.lower() and tps > 0:
                            tps = 1000.0 / tps
                        self._current_tps = round(min(tps, 20.0), 1)  # Bedrock 最大 TPS 为 20
                        return self._current_tps
                    except (ValueError, ZeroDivisionError):
                        continue
        except (OSError, ValueError, AttributeError) as e:
            safe_log_exception("performance.py", f"性能数据采集失败: {e}", "debug")

        # BDS 默认不输出 TPS，返回 0（前端应显示"暂不支持"或"--"）
        return 0.0

    def get_history(self, points=60):
        """获取历史性能数据。

        Args:
            points: 返回的数据点数（默认最近60点）

        Returns:
            list: 历史性能数据列表
        """
        with self._lock:
            history = list(self._history)
        return history[-points:] if points < len(history) else history

    def get_summary(self):
        """获取性能数据摘要（平均值、最大值、最小值）。

        Returns:
            dict: 性能摘要
        """
        with self._lock:
            history = list(self._history)

        if not history:
            return {
                "cpu_avg": 0,
                "cpu_max": 0,
                "cpu_min": 0,
                "mem_avg": 0,
                "mem_max": 0,
                "mem_min": 0,
                "players_avg": 0,
                "players_max": 0,
                "tps_avg": 0,
                "tps_min": 0,
                "data_points": 0,
            }

        cpu_values = [d["cpu_percent"] for d in history if d["cpu_percent"] > 0]
        mem_values = [d["memory_mb"] for d in history if d["memory_mb"] > 0]
        player_values = [d["players"] for d in history]
        tps_values = [d["tps"] for d in history if d["tps"] > 0]

        return {
            "cpu_avg": round(sum(cpu_values) / len(cpu_values), 1) if cpu_values else 0,
            "cpu_max": round(max(cpu_values), 1) if cpu_values else 0,
            "cpu_min": round(min(cpu_values), 1) if cpu_values else 0,
            "mem_avg": round(sum(mem_values) / len(mem_values), 1) if mem_values else 0,
            "mem_max": round(max(mem_values), 1) if mem_values else 0,
            "mem_min": round(min(mem_values), 1) if mem_values else 0,
            "players_avg": round(sum(player_values) / len(player_values), 1) if player_values else 0,
            "players_max": max(player_values) if player_values else 0,
            "tps_avg": round(sum(tps_values) / len(tps_values), 1) if tps_values else 0,
            "tps_min": round(min(tps_values), 1) if tps_values else 0,
            "data_points": len(history),
        }

    def get_current(self):
        """获取最新的性能数据。

        Returns:
            dict: 最新性能数据，如果没有数据则返回默认值
        """
        with self._lock:
            if self._history:
                return self._history[-1].copy()
        return {
            "timestamp": time.time(),
            "time_str": time.strftime("%H:%M:%S"),
            "cpu_percent": 0.0,
            "memory_mb": 0.0,
            "memory_percent": 0.0,
            "players": 0,
            "tps": 0.0,
            "running": False,
            "uptime_seconds": 0,
        }

    def reset(self):
        """重置历史数据。"""
        with self._lock:
            self._history.clear()
            self._current_tps = 0.0
            self._last_console_idx = 0
            self._players_snapshot = 0


# 全局性能监控器实例
performance_monitor = PerformanceMonitor(max_history=300)


def get_server_performance_full(server_proc=None, console=None):
    """获取完整的服务器性能数据（包含历史和摘要）。

    Args:
        server_proc: ServerProcess 实例
        console: ConsoleBuffer 实例

    Returns:
        dict: 完整性能数据
    """
    # 收集一次新数据
    current = performance_monitor.collect(server_proc, console)
    history = performance_monitor.get_history(points=60)
    summary = performance_monitor.get_summary()

    return {
        "current": current,
        "history": history,
        "summary": summary,
        "max_history": performance_monitor.max_history,
    }


def get_performance_monitor():
    """获取全局性能监控器实例。"""
    return performance_monitor


def reset_performance_monitor():
    """重置性能监控器历史数据。"""
    performance_monitor.reset()


__all__ = [
    "PerformanceMonitor",
    "performance_monitor",
    "get_server_performance_full",
    "get_performance_monitor",
    "reset_performance_monitor",
]
