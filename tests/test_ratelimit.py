"""
RateLimiter 单元测试 - Minecraft 基岩版服务器管理器
覆盖 API 速率限制器的所有功能
"""

import os
import sys
import time
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bedrock_server_manager.ratelimit import RateLimiter, check_rate_limit, get_rate_limiter


class TestRateLimiterInit(unittest.TestCase):
    """初始化测试"""

    def test_default_init(self):
        """测试默认初始化"""
        limiter = RateLimiter()
        self.assertEqual(limiter.default_rate, 5)
        self.assertEqual(limiter.default_period, 1.0)

    def test_custom_init(self):
        """测试自定义初始化"""
        limiter = RateLimiter(default_rate=10, default_period=2.0)
        self.assertEqual(limiter.default_rate, 10)
        self.assertEqual(limiter.default_period, 2.0)

    def test_custom_limits(self):
        """测试自定义端点速率限制"""
        limiter = RateLimiter()
        # 检查预定义的自定义限制
        self.assertIn("/api/cmd", limiter._custom_limits)
        self.assertIn("/api/install", limiter._custom_limits)
        self.assertIn("/api/server/start", limiter._custom_limits)
        self.assertIn("/api/server/delete", limiter._custom_limits)

    def test_get_limit_custom(self):
        """测试获取自定义端点的速率限制"""
        limiter = RateLimiter()
        rate, period = limiter._get_limit("/api/cmd")
        self.assertEqual(rate, 10)
        self.assertEqual(period, 1.0)

    def test_get_limit_default(self):
        """测试获取默认端点的速率限制"""
        limiter = RateLimiter()
        rate, period = limiter._get_limit("/api/unknown")
        self.assertEqual(rate, 5)
        self.assertEqual(period, 1.0)


class TestRateLimiterAllow(unittest.TestCase):
    """允许请求测试"""

    def setUp(self):
        self.limiter = RateLimiter(default_rate=3, default_period=1.0)

    def test_allow_first_request(self):
        """测试第一个请求应该被允许"""
        allowed, retry_after = self.limiter.allow("/api/test")
        self.assertTrue(allowed)
        self.assertEqual(retry_after, 0)

    def test_allow_multiple_requests(self):
        """测试多个请求在限制内应该被允许"""
        for i in range(3):
            allowed, _ = self.limiter.allow("/api/test")
            self.assertTrue(allowed, f"第 {i+1} 个请求应该被允许")

    def test_deny_when_exceeded(self):
        """测试超过限制后应该被拒绝"""
        # 消耗所有令牌
        for i in range(3):
            self.limiter.allow("/api/test")
        # 第4个请求应该被拒绝
        allowed, retry_after = self.limiter.allow("/api/test")
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_refill_after_period(self):
        """测试周期过后令牌应该被补充"""
        # 消耗所有令牌
        for i in range(3):
            self.limiter.allow("/api/test")
        # 等待周期结束
        time.sleep(1.1)
        # 应该又可以请求了
        allowed, _ = self.limiter.allow("/api/test")
        self.assertTrue(allowed)

    def test_different_endpoints_independent(self):
        """测试不同端点的速率限制相互独立"""
        # 消耗 /api/test1 的所有令牌
        for i in range(3):
            self.limiter.allow("/api/test1")
        # /api/test2 应该仍然可以请求
        allowed, _ = self.limiter.allow("/api/test2")
        self.assertTrue(allowed)

    def test_different_clients_independent(self):
        """测试不同客户端的速率限制相互独立"""
        # 消耗 client1 的所有令牌
        for i in range(3):
            self.limiter.allow("/api/test", client_ip="192.168.1.1")
        # client2 应该仍然可以请求
        allowed, _ = self.limiter.allow("/api/test", client_ip="192.168.1.2")
        self.assertTrue(allowed)

    def test_custom_endpoint_limit(self):
        """测试自定义端点的速率限制"""
        limiter = RateLimiter()
        # /api/install 限制为每5秒1次
        # 注意：初始令牌使用 default_rate (5)，需要先消耗完
        for i in range(5):
            allowed, _ = limiter.allow("/api/install")
            self.assertTrue(allowed, f"第 {i+1} 个初始请求应该被允许")
        # 第6个请求应该被拒绝
        allowed, retry_after = limiter.allow("/api/install")
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 4)  # 应该接近5秒


class TestRateLimiterReset(unittest.TestCase):
    """重置测试"""

    def setUp(self):
        self.limiter = RateLimiter(default_rate=3, default_period=1.0)
        # 消耗所有令牌
        for i in range(3):
            self.limiter.allow("/api/test")

    def test_reset_specific_endpoint(self):
        """测试重置特定端点"""
        # 确认被限制
        allowed, _ = self.limiter.allow("/api/test")
        self.assertFalse(allowed)
        # 重置
        self.limiter.reset("/api/test")
        # 应该又可以请求了
        allowed, _ = self.limiter.allow("/api/test")
        self.assertTrue(allowed)

    def test_reset_all(self):
        """测试重置所有端点"""
        # 消耗另一个端点的令牌
        for i in range(3):
            self.limiter.allow("/api/test2")
        # 重置所有
        self.limiter.reset()
        # 两个端点都应该可以请求了
        allowed1, _ = self.limiter.allow("/api/test")
        allowed2, _ = self.limiter.allow("/api/test2")
        self.assertTrue(allowed1)
        self.assertTrue(allowed2)

    def test_reset_nonexistent_endpoint(self):
        """测试重置不存在的端点不报错"""
        # 应该不抛出异常
        self.limiter.reset("/api/nonexistent")


class TestRateLimiterStatus(unittest.TestCase):
    """状态查询测试"""

    def setUp(self):
        self.limiter = RateLimiter(default_rate=5, default_period=1.0)

    def test_get_status_specific_endpoint(self):
        """测试获取特定端点的状态"""
        self.limiter.allow("/api/test")
        status = self.limiter.get_status("/api/test")
        self.assertEqual(status["endpoint"], "/api/test")
        self.assertEqual(status["tokens"], 4)  # 5-1=4
        self.assertEqual(status["count"], 1)

    def test_get_status_nonexistent_endpoint(self):
        """测试获取不存在端点的状态"""
        status = self.limiter.get_status("/api/nonexistent")
        self.assertEqual(status["endpoint"], "/api/nonexistent")
        self.assertEqual(status["tokens"], 0)
        self.assertEqual(status["count"], 0)

    def test_get_status_all(self):
        """测试获取所有端点的状态"""
        self.limiter.allow("/api/test1")
        self.limiter.allow("/api/test2")
        status = self.limiter.get_status()
        self.assertEqual(status["total_buckets"], 2)
        self.assertEqual(status["default_rate"], 5)
        self.assertEqual(status["default_period"], 1.0)
        self.assertGreater(status["custom_limits"], 0)


class TestRateLimiterThreadSafety(unittest.TestCase):
    """线程安全测试"""

    def test_concurrent_requests(self):
        """测试并发请求不会超过限制"""
        limiter = RateLimiter(default_rate=10, default_period=1.0)
        results = []
        errors = []

        def make_requests():
            try:
                for i in range(5):
                    allowed, _ = limiter.allow("/api/test")
                    results.append(allowed)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=make_requests) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        # 总共 20 个请求，限制为 10，应该有 10 个被允许，10 个被拒绝
        allowed_count = sum(1 for r in results if r)
        self.assertLessEqual(allowed_count, 10)

    def test_concurrent_reset(self):
        """测试并发重置不会崩溃"""
        limiter = RateLimiter(default_rate=5, default_period=1.0)
        errors = []

        def reset_loop():
            try:
                for i in range(10):
                    limiter.reset()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def request_loop():
            try:
                for i in range(10):
                    limiter.allow("/api/test")
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=reset_loop),
            threading.Thread(target=request_loop),
            threading.Thread(target=reset_loop),
            threading.Thread(target=request_loop),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)


class TestGlobalFunctions(unittest.TestCase):
    """全局函数测试"""

    def test_check_rate_limit(self):
        """测试 check_rate_limit 全局函数"""
        # 重置全局限制器
        get_rate_limiter().reset()
        allowed, _ = check_rate_limit("/api/test")
        self.assertTrue(allowed)

    def test_get_rate_limiter(self):
        """测试 get_rate_limiter 全局函数"""
        limiter = get_rate_limiter()
        self.assertIsInstance(limiter, RateLimiter)
        # 应该返回同一个实例
        limiter2 = get_rate_limiter()
        self.assertIs(limiter, limiter2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
