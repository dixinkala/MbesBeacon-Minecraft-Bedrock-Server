"""
API 速率限制模块：简单的令牌桶算法，防止前端 bug 或恶意脚本导致服务器频繁重启。
对 /api/cmd、/api/install、/api/server/start 等操作类端点添加速率限制。
"""

import threading
import time
from collections import defaultdict


class RateLimiter:
    """简单的令牌桶速率限制器。

    每个端点独立计数，默认每秒最多 5 次请求。
    超过限制的请求返回 429 Too Many Requests。
    """

    def __init__(self, default_rate=5, default_period=1.0):
        """初始化速率限制器。

        Args:
            default_rate: 默认每个周期允许的请求数
            default_period: 周期长度（秒）
        """
        self.default_rate = default_rate
        self.default_period = default_period
        self._buckets = defaultdict(
            lambda: {
                "tokens": default_rate,
                "last_refill": time.time(),
                "count": 0,
            }
        )
        self._lock = threading.Lock()
        # 特定端点的自定义速率限制
        self._custom_limits = {
            "/api/cmd": (10, 1.0),  # 控制台指令：每秒10次
            "/api/install": (1, 5.0),  # 安装：每5秒1次
            "/api/server/start": (2, 10.0),  # 启动：每10秒2次
            "/api/server/stop": (2, 10.0),  # 停止：每10秒2次
            "/api/server/restart": (1, 15.0),  # 重启：每15秒1次
            "/api/server/delete": (1, 30.0),  # 删除：每30秒1次
            "/api/backups/restore": (2, 10.0),  # 恢复备份：每10秒2次
        }

    def _get_limit(self, endpoint):
        """获取端点的速率限制配置。"""
        return self._custom_limits.get(endpoint, (self.default_rate, self.default_period))

    def allow(self, endpoint, client_ip="127.0.0.1"):
        """检查是否允许请求。

        Args:
            endpoint: API 端点路径
            client_ip: 客户端 IP

        Returns:
            tuple: (allowed, retry_after_seconds)
                allowed: 是否允许请求
                retry_after: 如果被限制，需要等待的秒数
        """
        rate, period = self._get_limit(endpoint)
        key = f"{client_ip}:{endpoint}"

        with self._lock:
            bucket = self._buckets[key]
            now = time.time()
            elapsed = now - bucket["last_refill"]

            # 补充令牌
            if elapsed >= period:
                bucket["tokens"] = rate
                bucket["last_refill"] = now
                bucket["count"] = 0
            else:
                # 按比例补充令牌
                refill = int(elapsed / period * rate)
                if refill > 0:
                    bucket["tokens"] = min(rate, bucket["tokens"] + refill)
                    bucket["last_refill"] = now - (elapsed % period)

            # 检查是否有足够的令牌
            if bucket["tokens"] > 0:
                bucket["tokens"] -= 1
                bucket["count"] += 1
                return True, 0
            else:
                # 计算需要等待的时间
                retry_after = period - (now - bucket["last_refill"])
                return False, max(0, retry_after)

    def reset(self, endpoint=None, client_ip="127.0.0.1"):
        """重置特定端点或所有端点的速率限制。"""
        with self._lock:
            if endpoint:
                key = f"{client_ip}:{endpoint}"
                if key in self._buckets:
                    del self._buckets[key]
            else:
                self._buckets.clear()

    def get_status(self, endpoint=None, client_ip="127.0.0.1"):
        """获取速率限制状态。"""
        with self._lock:
            if endpoint:
                key = f"{client_ip}:{endpoint}"
                bucket = self._buckets.get(key, {})
                return {
                    "endpoint": endpoint,
                    "tokens": bucket.get("tokens", 0),
                    "count": bucket.get("count", 0),
                }
            else:
                return {
                    "total_buckets": len(self._buckets),
                    "default_rate": self.default_rate,
                    "default_period": self.default_period,
                    "custom_limits": len(self._custom_limits),
                }


# 全局速率限制器实例
rate_limiter = RateLimiter(default_rate=5, default_period=1.0)


def check_rate_limit(endpoint, client_ip="127.0.0.1"):
    """检查 API 端点的速率限制。

    Args:
        endpoint: API 端点路径
        client_ip: 客户端 IP

    Returns:
        tuple: (allowed, retry_after_seconds)
    """
    return rate_limiter.allow(endpoint, client_ip)


def get_rate_limiter():
    """获取全局速率限制器实例。"""
    return rate_limiter


__all__ = ["RateLimiter", "rate_limiter", "check_rate_limit", "get_rate_limiter"]
