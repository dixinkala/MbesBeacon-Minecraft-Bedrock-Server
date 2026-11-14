"""
API 速率限制模块：令牌桶算法，防止前端 bug 或恶意脚本导致服务器频繁重启。
对 /api/cmd、/api/install、/api/server/start 等操作类端点添加速率限制。
"""

import threading
import time

# 单个限流器最多保留的桶数量。超过后清理长期未活跃的桶，
# 避免任意 POST 路径（含 404）无限增长内存。
MAX_BUCKETS = 4096
# 桶的空闲回收时间：超过该秒数未再被访问即丢弃
BUCKET_IDLE_TTL = 3600.0


class RateLimiter:
    """令牌桶速率限制器。

    每个 (客户端 IP, 端点) 一个独立桶，桶容量等于该端点的 rate，
    空闲时按 ``rate / period`` 的速率匀速补充令牌。
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
        self._buckets = {}
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
            # 补全/帮助是只读廉价端点，但前端会在输入过程中频繁调用；
            # 沿用默认 5/s 会让快速打字时补全被 429 掐断。
            "/api/commands/autocomplete": (30, 1.0),  # 命令补全：每秒30次
            "/api/commands/help": (10, 1.0),  # 命令帮助：每秒10次
        }
        # GET 端点的配额（只对重端点收紧；其余沿用宽松默认）。
        # 早期实现只对 POST 限流，GET 完全无限制：
        # /api/worlds/export 每次都要打包整个世界并遍历磁盘，
        # /api/players 会同步阻塞最多 2.8 秒等服务器输出，都可被反复打到。
        self._get_limits = {
            # 导出世界要打包整个世界并遍历磁盘，但容量给 1 会让"导出失败后
            # 重新点一次"在 30 秒内一直 429；给 3 次留出纠正余地。
            "/api/worlds/export": (3, 30.0),  # 导出世界上传：每30秒3次
            "/api/players": (5, 1.0),  # 在线玩家探测（阻塞式）：每秒5次
            "/api/console/export": (2, 10.0),  # 导出控制台日志：每10秒2次
            "/api/browse-dir": (2, 1.0),  # 弹系统目录对话框：每秒2次
            "/api/config/full": (10, 1.0),  # 全量配置：每秒10次
        }
        # GET 的宽松默认：前端约每 800ms 轮询一次 /api/status，
        # 页面切换时会短时间连打多个接口，这里留足余量（60/秒）。
        self.default_get_rate = 60
        self.default_get_period = 1.0

    def _get_limit(self, endpoint, method="POST"):
        """获取端点的速率限制配置。

        Args:
            endpoint: 端点路径
            method: HTTP 方法（GET 使用独立的配额表）
        """
        if method.upper() == "GET":
            if endpoint in self._get_limits:
                return self._get_limits[endpoint]
            return (self.default_get_rate, self.default_get_period)
        return self._custom_limits.get(endpoint, (self.default_rate, self.default_period))

    def _new_bucket(self, rate, now):
        """新建一个满令牌的桶。

        容量必须取**该端点自己的 rate**：早期实现固定用 default_rate，
        导致 /api/server/restart（1 次/15 秒）首次使用时可连续放行 5 次。
        """
        return {"tokens": float(rate), "last_refill": now, "count": 0, "rate": rate}

    def _prune_locked(self, now):
        """清理长期不活跃的桶（调用方需持有锁）。"""
        if len(self._buckets) <= MAX_BUCKETS:
            return
        stale = [k for k, b in self._buckets.items() if now - b["last_refill"] > BUCKET_IDLE_TTL]
        for k in stale:
            del self._buckets[k]
        # 仍然超限时按 last_refill 由旧到新丢弃
        if len(self._buckets) > MAX_BUCKETS:
            ordered = sorted(self._buckets.items(), key=lambda kv: kv[1]["last_refill"])
            for k, _b in ordered[: len(self._buckets) - MAX_BUCKETS]:
                del self._buckets[k]

    def allow(self, endpoint, client_ip="127.0.0.1", method="POST"):
        """检查是否允许请求，并消耗一个令牌。

        Args:
            endpoint: API 端点路径
            client_ip: 客户端 IP
            method: HTTP 方法（GET / POST 使用不同的配额表与独立桶）

        Returns:
            tuple: (allowed, retry_after_seconds)
                allowed: 是否允许请求
                retry_after: 如果被限制，需要等待的秒数
        """
        method = (method or "POST").upper()
        rate, period = self._get_limit(endpoint, method)
        if rate <= 0:
            return False, period
        # 方法也要进键：同一路径的 GET/POST 配额不同，共用一个桶会互相挤占
        key = f"{client_ip}:{method}:{endpoint}"
        refill_per_sec = rate / period if period > 0 else float(rate)

        with self._lock:
            # 用单调时钟：墙钟被回拨会让 elapsed 为负、令牌卡在 0 很久，
            # 前跳则会瞬间补满桶（限流失效）。
            now = time.monotonic()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._new_bucket(rate, now)
                self._buckets[key] = bucket
                self._prune_locked(now)
            elif bucket["rate"] != rate:
                # 端点配额被调整过：按新容量重建，避免沿用旧容量
                bucket = self._new_bucket(rate, now)
                self._buckets[key] = bucket

            # 按经过时间匀速补充，最多补到容量上限
            elapsed = now - bucket["last_refill"]
            if elapsed > 0:
                bucket["tokens"] = min(float(rate), bucket["tokens"] + elapsed * refill_per_sec)
                bucket["last_refill"] = now

            if bucket["tokens"] >= 1.0:
                bucket["tokens"] -= 1.0
                bucket["count"] += 1
                return True, 0

            retry_after = (1.0 - bucket["tokens"]) / refill_per_sec
            return False, max(0.0, retry_after)

    def reset(self, endpoint=None, client_ip="127.0.0.1", method=None):
        """重置特定端点或所有端点的速率限制。

        Args:
            endpoint: 端点路径；None 表示清空全部
            client_ip: 客户端 IP
            method: 指定方法；None（默认）表示该端点的 **GET 与 POST 都清**。
                早期写死 POST，导致 GET 桶清不掉——测试里 reset 后 GET 仍被限流。

        Returns:
            None
        """
        methods = (method or "").upper() or None
        with self._lock:
            if not endpoint:
                self._buckets.clear()
                return
            if methods:
                self._buckets.pop(f"{client_ip}:{methods}:{endpoint}", None)
                return
            # 两种方法都清（桶键里带方法，不能只删一个）
            for m in ("GET", "POST"):
                self._buckets.pop(f"{client_ip}:{m}:{endpoint}", None)

    def get_status(self, endpoint=None, client_ip="127.0.0.1", method="POST"):
        """获取速率限制状态。"""
        with self._lock:
            if endpoint:
                m = (method or "POST").upper()
                key = f"{client_ip}:{m}:{endpoint}"
                bucket = self._buckets.get(key, {})
                return {
                    "endpoint": endpoint,
                    "method": m,
                    "tokens": int(bucket.get("tokens", 0)),
                    "count": bucket.get("count", 0),
                }
            return {
                "total_buckets": len(self._buckets),
                "default_rate": self.default_rate,
                "default_period": self.default_period,
                "default_get_rate": self.default_get_rate,
                "default_get_period": self.default_get_period,
                "custom_limits": len(self._custom_limits),
                "custom_get_limits": len(self._get_limits),
            }


# 全局速率限制器实例
rate_limiter = RateLimiter(default_rate=5, default_period=1.0)


def check_rate_limit(endpoint, client_ip="127.0.0.1", method="POST"):
    """检查 API 端点的速率限制。

    Args:
        endpoint: API 端点路径
        client_ip: 客户端 IP
        method: HTTP 方法（GET / POST 配额不同）

    Returns:
        tuple: (allowed, retry_after_seconds)
    """
    return rate_limiter.allow(endpoint, client_ip, method=method)


def get_rate_limiter():
    """获取全局速率限制器实例。"""
    return rate_limiter


__all__ = ["RateLimiter", "rate_limiter", "check_rate_limit", "get_rate_limiter"]
