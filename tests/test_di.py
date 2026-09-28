"""
DI 容器单元测试 - Minecraft 基岩版服务器管理器
覆盖 DIContainer 的注册、解析、生命周期与注入装饰器
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bedrock_server_manager.di import DIContainer, container, initialize_container, inject


class TestDIContainer(unittest.TestCase):
    """DIContainer 注册与解析测试"""

    def setUp(self):
        self.di = DIContainer()

    def test_resolve_unregistered_returns_none(self):
        """未注册的依赖解析返回 None"""
        self.assertIsNone(self.di.resolve("missing"))

    def test_register_singleton_cached(self):
        """单例依赖缓存：多次解析返回同一实例"""
        calls = []
        self.di.register_singleton("svc", lambda: calls.append(1) or object())
        first = self.di.resolve("svc")
        second = self.di.resolve("svc")
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)

    def test_register_transient_new_each_time(self):
        """瞬态依赖每次解析创建新实例"""
        calls = []
        self.di.register_transient("svc", lambda: calls.append(1) or object())
        first = self.di.resolve("svc")
        second = self.di.resolve("svc")
        self.assertIsNot(first, second)
        self.assertEqual(len(calls), 2)

    def test_register_instance(self):
        """直接注册实例并解析"""
        obj = object()
        self.di.register_instance("svc", obj)
        self.assertIs(self.di.resolve("svc"), obj)

    def test_is_registered(self):
        """is_registered 覆盖三种注册方式"""
        self.di.register_singleton("a", lambda: 1)
        self.di.register_transient("b", lambda: 2)
        self.di.register_instance("c", 3)
        self.assertTrue(self.di.is_registered("a"))
        self.assertTrue(self.di.is_registered("b"))
        self.assertTrue(self.di.is_registered("c"))
        self.assertFalse(self.di.is_registered("d"))

    def test_reregister_singleton_overrides_transient(self):
        """transient 被 singleton 覆盖后按单例解析（状态互斥修复）"""
        self.di.register_transient("svc", lambda: object())
        fixed = object()
        self.di.register_singleton("svc", lambda: fixed)
        first = self.di.resolve("svc")
        second = self.di.resolve("svc")
        self.assertIs(first, fixed)
        self.assertIs(second, fixed)

    def test_reregister_transient_overrides_singleton(self):
        """singleton 被 transient 覆盖后每次新建（清除缓存）"""
        self.di.register_singleton("svc", lambda: object())
        cached = self.di.resolve("svc")
        self.di.register_transient("svc", lambda: object())
        first = self.di.resolve("svc")
        self.assertIsNot(first, cached)

    def test_register_instance_overrides_factory(self):
        """register_instance 覆盖工厂注册"""
        self.di.register_singleton("svc", lambda: object())
        obj = object()
        self.di.register_instance("svc", obj)
        self.assertIs(self.di.resolve("svc"), obj)

    def test_clear(self):
        """clear 清空所有注册"""
        self.di.register_singleton("a", lambda: 1)
        self.di.register_transient("b", lambda: 2)
        self.di.clear()
        self.assertFalse(self.di.is_registered("a"))
        self.assertFalse(self.di.is_registered("b"))
        self.assertIsNone(self.di.resolve("a"))


class TestInjectDecorator(unittest.TestCase):
    """inject 装饰器测试"""

    def setUp(self):
        import bedrock_server_manager.di as di_mod

        self.di = DIContainer()
        # 替换 di 模块级全局 container（inject 引用的是 di.container），测试后恢复
        self._orig_container = di_mod.container
        di_mod.container = self.di

    def tearDown(self):
        import bedrock_server_manager.di as di_mod

        di_mod.container = self._orig_container

    def test_inject_dependency(self):
        """注入注册的依赖"""
        self.di.register_instance("settings", {"key": "value"})

        @inject("settings")
        def func(settings):
            return settings

        self.assertEqual(func(), {"key": "value"})

    def test_inject_keeps_explicit_kwarg(self):
        """显式提供的参数优先于注入"""
        self.di.register_instance("settings", {"default": True})

        @inject("settings")
        def func(settings):
            return settings

        self.assertEqual(func(settings={"explicit": True}), {"explicit": True})

    def test_inject_positional_argument_respected(self):
        """位置参数提供时不注入"""
        self.di.register_instance("settings", {"default": True})

        @inject("settings")
        def func(settings):
            return settings

        self.assertEqual(func({"positional": True}), {"positional": True})

    def test_inject_unregistered_leaves_unchanged(self):
        """未注册依赖不注入（函数需提供默认值）"""

        @inject("not_registered")
        def func(not_registered=None):
            return not_registered

        self.assertIsNone(func())

    def test_inject_multiple_dependencies(self):
        """多依赖注入"""
        self.di.register_instance("a", "value_a")
        self.di.register_instance("b", "value_b")

        @inject("a", "b")
        def func(a, b):
            return a, b

        self.assertEqual(func(), ("value_a", "value_b"))

    def test_inject_preserves_wraps(self):
        """functools.wraps 保留函数元数据"""

        @inject("settings")
        def my_func(settings):
            """docstring"""

        self.assertEqual(my_func.__name__, "my_func")
        self.assertEqual(my_func.__doc__, "docstring")


class TestInitializeContainer(unittest.TestCase):
    """initialize_container 与 AppContext 集成测试"""

    def setUp(self):
        from bedrock_server_manager.app_context import AppContext

        AppContext.reset()
        container.clear()
        initialize_container()

    def tearDown(self):
        from bedrock_server_manager.app_context import AppContext

        AppContext.reset()
        container.clear()

    def test_registers_app_context_dependencies(self):
        """常用依赖全部注册"""
        for name in (
            "app_context",
            "settings",
            "console",
            "install_state",
            "server_proc",
            "server_lock",
            "httpd",
            "api_token",
            "tray",
        ):
            self.assertTrue(container.is_registered(name), f"缺少依赖: {name}")

    def test_settings_resolves_from_current_context(self):
        """settings 解析为当前 AppContext 的 settings（瞬态语义）"""
        from bedrock_server_manager.app_context import AppContext

        ctx = AppContext.instance()
        resolved = container.resolve("settings")
        self.assertIs(resolved, ctx.settings)

    def test_settings_tracks_context_reset(self):
        """AppContext.reset 后 settings 解析指向新实例（瞬态，非快照）"""
        from bedrock_server_manager.app_context import AppContext

        old = container.resolve("settings")
        AppContext.reset()
        ctx = AppContext.instance()
        resolved = container.resolve("settings")
        self.assertIsNot(resolved, old)
        self.assertIs(resolved, ctx.settings)

    def test_console_resolves_from_current_context(self):
        """console 解析为当前 AppContext 的 console（瞬态语义）"""
        from bedrock_server_manager.app_context import AppContext

        ctx = AppContext.instance()
        mock_console = object()
        ctx.console = mock_console
        resolved = container.resolve("console")
        self.assertIs(resolved, mock_console)

    def test_api_token_resolves(self):
        """api_token 解析为字符串"""
        from bedrock_server_manager.app_context import AppContext

        resolved = container.resolve("api_token")
        self.assertEqual(resolved, AppContext.instance().api_token)
        self.assertIsInstance(resolved, str)
        self.assertGreater(len(resolved), 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
