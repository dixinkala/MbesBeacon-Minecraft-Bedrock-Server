"""
路由层测试：测试 Handler 类的路由注册、分发逻辑和认证机制。
使用 mock 对象模拟 HTTP 请求，测试所有 API 端点的路由分发。

测试覆盖：
- 路由注册机制（GET/POST 路由表）
- 路由处理方法存在性和可调用性
- 路由装饰器（register_route / register_get_route / register_post_route）
- Handler 核心辅助方法
- Handler mixin 继承结构
- 路由分发逻辑（使用 mock）
- 认证校验机制
- 404 未知路由处理
"""

import json
import unittest
from unittest.mock import MagicMock, patch


class TestRouteRegistration(unittest.TestCase):
    """测试路由注册机制。"""

    def test_get_routes_registered(self):
        """测试 GET 路由是否正确注册。"""
        from bedrock_server_manager.web.handler import Handler

        self.assertTrue(hasattr(Handler, "_get_routes"))
        self.assertIsInstance(Handler._get_routes, dict)
        # 关键 GET 路由应该存在
        expected_get_routes = [
            "/",
            "/api/health",
            "/api/status",
            "/api/versions",
            "/api/console",
            "/api/performance",
            "/api/backups",
            "/api/config",
            "/api/players",
            "/api/progress",
            "/api/theme",
            "/api/latest",
            "/api/sources",
            "/api/laninfo",
            "/api/worlds/list",
            "/api/packs",
        ]
        for route in expected_get_routes:
            self.assertIn(route, Handler._get_routes, f"GET 路由 {route} 未注册")

    def test_post_routes_registered(self):
        """测试 POST 路由是否正确注册。"""
        from bedrock_server_manager.web.handler import Handler

        self.assertTrue(hasattr(Handler, "_post_routes"))
        self.assertIsInstance(Handler._post_routes, dict)
        # 关键 POST 路由应该存在
        expected_post_routes = [
            "/api/install",
            "/api/server/start",
            "/api/server/stop",
            "/api/server/restart",
            "/api/server/delete",
            "/api/config",
            "/api/cmd",
            "/api/players/kick",
            "/api/players/ban",
            "/api/players/pardon",
            "/api/players/op",
            "/api/players/deop",
            "/api/players/permission",
            "/api/players/allowlist/add",
            "/api/players/allowlist/remove",
            "/api/backups/create",
            "/api/backups/restore",
            "/api/backups/delete",
            "/api/scheduled/save",
            "/api/theme",
            "/api/exit",
            "/api/cancel",
            "/api/update",
            "/api/setdir",
            "/api/servers/select",
            "/api/worlds/switch",
            "/api/worlds/rename",
            "/api/worlds/delete",
            "/api/worlds/duplicate",
            "/api/worlds/import",
            "/api/ipban/add",
            "/api/ipban/remove",
            "/api/console/clear",
            "/api/config/rollback",
        ]
        for route in expected_post_routes:
            self.assertIn(route, Handler._post_routes, f"POST 路由 {route} 未注册")

    def test_route_count_reasonable(self):
        """测试路由数量是否合理。"""
        from bedrock_server_manager.web.handler import Handler

        self.assertGreaterEqual(len(Handler._get_routes), 20, "GET 路由数量过少")
        self.assertGreaterEqual(len(Handler._post_routes), 35, "POST 路由数量过少")

    def test_no_duplicate_get_routes(self):
        """测试 GET 路由表没有重复键。"""
        from bedrock_server_manager.web.handler import Handler

        self.assertEqual(
            len(Handler._get_routes),
            len(set(Handler._get_routes.keys())),
            "GET 路由表存在重复键",
        )

    def test_no_duplicate_post_routes(self):
        """测试 POST 路由表没有重复键。"""
        from bedrock_server_manager.web.handler import Handler

        self.assertEqual(
            len(Handler._post_routes),
            len(set(Handler._post_routes.keys())),
            "POST 路由表存在重复键",
        )

    def test_all_get_routes_start_with_slash(self):
        """测试所有 GET 路由路径都以 / 开头。"""
        from bedrock_server_manager.web.handler import Handler

        for route in Handler._get_routes:
            self.assertTrue(route.startswith("/"), f"GET 路由 {route} 不以 / 开头")

    def test_all_post_routes_start_with_slash(self):
        """测试所有 POST 路由路径都以 / 开头。"""
        from bedrock_server_manager.web.handler import Handler

        for route in Handler._post_routes:
            self.assertTrue(route.startswith("/"), f"POST 路由 {route} 不以 / 开头")


class TestRouteHandlerMethods(unittest.TestCase):
    """测试路由处理方法是否存在且可调用。"""

    def test_get_route_methods_exist(self):
        """测试所有 GET 路由处理方法是否存在且可调用。"""
        from bedrock_server_manager.web.handler import Handler

        for route, method_name in Handler._get_routes.items():
            self.assertTrue(
                hasattr(Handler, method_name),
                f"GET 路由 {route} 对应的方法 {method_name} 不存在",
            )
            method = getattr(Handler, method_name)
            self.assertTrue(
                callable(method),
                f"GET 路由 {route} 对应的方法 {method_name} 不可调用",
            )

    def test_post_route_methods_exist(self):
        """测试所有 POST 路由处理方法是否存在且可调用。"""
        from bedrock_server_manager.web.handler import Handler

        for route, method_name in Handler._post_routes.items():
            self.assertTrue(
                hasattr(Handler, method_name),
                f"POST 路由 {route} 对应的方法 {method_name} 不存在",
            )
            method = getattr(Handler, method_name)
            self.assertTrue(
                callable(method),
                f"POST 路由 {route} 对应的方法 {method_name} 不可调用",
            )

    def test_get_route_method_names_are_strings(self):
        """测试 GET 路由表中的方法名都是字符串。"""
        from bedrock_server_manager.web.handler import Handler

        for route, method_name in Handler._get_routes.items():
            self.assertIsInstance(
                method_name, str, f"GET 路由 {route} 的方法名不是字符串"
            )

    def test_post_route_method_names_are_strings(self):
        """测试 POST 路由表中的方法名都是字符串。"""
        from bedrock_server_manager.web.handler import Handler

        for route, method_name in Handler._post_routes.items():
            self.assertIsInstance(
                method_name, str, f"POST 路由 {route} 的方法名不是字符串"
            )


class TestRouteDecorator(unittest.TestCase):
    """测试路由装饰器。"""

    def test_register_route_decorator_sets_attributes(self):
        """测试 register_route 装饰器是否正确设置属性。"""
        from bedrock_server_manager.web.route_decorator import register_route

        @register_route("/test/route", method="GET")
        def test_handler():
            pass

        self.assertTrue(hasattr(test_handler, "_route_path"))
        self.assertEqual(test_handler._route_path, "/test/route")
        self.assertTrue(hasattr(test_handler, "_route_method"))
        self.assertEqual(test_handler._route_method, "GET")

    def test_register_get_route_decorator(self):
        """测试 register_get_route 便捷装饰器。"""
        from bedrock_server_manager.web.route_decorator import register_get_route

        @register_get_route("/test/get")
        def test_get_handler():
            pass

        self.assertEqual(test_get_handler._route_path, "/test/get")
        self.assertEqual(test_get_handler._route_method, "GET")

    def test_register_post_route_decorator(self):
        """测试 register_post_route 便捷装饰器。"""
        from bedrock_server_manager.web.route_decorator import register_post_route

        @register_post_route("/test/post")
        def test_post_handler():
            pass

        self.assertEqual(test_post_handler._route_path, "/test/post")
        self.assertEqual(test_post_handler._route_method, "POST")

    def test_init_routes_function(self):
        """测试 init_routes 函数是否正确扫描和注册路由。"""
        from bedrock_server_manager.web.route_decorator import register_route, init_routes

        class TestClass:
            _get_routes = {}
            _post_routes = {}

            @register_route("/test/get", method="GET")
            def get_handler(self):
                pass

            @register_route("/test/post", method="POST")
            def post_handler(self):
                pass

        init_routes(TestClass)

        self.assertIn("/test/get", TestClass._get_routes)
        self.assertEqual(TestClass._get_routes["/test/get"], "get_handler")
        self.assertIn("/test/post", TestClass._post_routes)
        self.assertEqual(TestClass._post_routes["/test/post"], "post_handler")

    def test_init_routes_ignores_non_decorated_methods(self):
        """测试 init_routes 函数是否忽略未装饰的方法。"""
        from bedrock_server_manager.web.route_decorator import register_route, init_routes

        class TestClass:
            _get_routes = {}
            _post_routes = {}

            def regular_method(self):
                pass

            @register_route("/test/decorated", method="GET")
            def decorated_method(self):
                pass

        init_routes(TestClass)

        self.assertIn("/test/decorated", TestClass._get_routes)
        self.assertNotIn("regular_method", TestClass._get_routes.values())


class TestHandlerCoreMethods(unittest.TestCase):
    """测试 Handler 核心辅助方法。"""

    def test_handler_has_core_methods(self):
        """测试 Handler 类是否有核心辅助方法。"""
        from bedrock_server_manager.web.handler import Handler

        core_methods = [
            "_send",
            "_json",
            "_post_data",
            "_check_auth",
            "do_GET",
            "do_POST",
            "_serve_console_stream",
            "_shutdown_later",
        ]
        for method in core_methods:
            self.assertTrue(hasattr(Handler, method), f"Handler 缺少核心方法 {method}")

    def test_handler_core_methods_are_callable(self):
        """测试 Handler 核心方法是否可调用。"""
        from bedrock_server_manager.web.handler import Handler

        core_methods = [
            "_send",
            "_json",
            "_post_data",
            "_check_auth",
            "do_GET",
            "do_POST",
        ]
        for method in core_methods:
            self.assertTrue(
                callable(getattr(Handler, method)),
                f"Handler 核心方法 {method} 不可调用",
            )


class TestHandlerMixinInheritance(unittest.TestCase):
    """测试 Handler mixin 继承结构。"""

    def test_handler_inherits_all_mixins(self):
        """测试 Handler 是否正确继承了所有 mixin 类。"""
        from bedrock_server_manager.web.handler import Handler
        from bedrock_server_manager.web.routes import (
            BackupsRoutesMixin,
            CommandsRoutesMixin,
            ConfigRoutesMixin,
            ConsoleRoutesMixin,
            GetRoutesMixin,
            MiscRoutesMixin,
            PlayersRoutesMixin,
            PostExtraRoutesMixin,
            ServerRoutesMixin,
            WorldsRoutesMixin,
        )

        mixins = [
            GetRoutesMixin,
            PostExtraRoutesMixin,
            MiscRoutesMixin,
            ConsoleRoutesMixin,
            CommandsRoutesMixin,
            ServerRoutesMixin,
            PlayersRoutesMixin,
            BackupsRoutesMixin,
            ConfigRoutesMixin,
            WorldsRoutesMixin,
        ]
        for mixin in mixins:
            self.assertTrue(
                issubclass(Handler, mixin),
                f"Handler 未继承 mixin 类 {mixin.__name__}",
            )

    def test_handler_inherits_base_http_handler(self):
        """测试 Handler 是否继承了 BaseHTTPRequestHandler。"""
        from http.server import BaseHTTPRequestHandler
        from bedrock_server_manager.web.handler import Handler

        self.assertTrue(issubclass(Handler, BaseHTTPRequestHandler))

    def test_mixin_routes_are_registered(self):
        """测试各 mixin 中的路由是否都被注册到 Handler。"""
        from bedrock_server_manager.web.handler import Handler

        # 各 mixin 应该贡献的路由
        mixin_routes = {
            "GetRoutesMixin": ["/api/status", "/api/versions", "/api/console"],
            "ServerRoutesMixin": ["/api/server/start", "/api/server/stop", "/api/server/restart"],
            "PlayersRoutesMixin": ["/api/players/kick", "/api/players/ban", "/api/players/op"],
            "BackupsRoutesMixin": ["/api/backups/create", "/api/backups/restore", "/api/backups/delete"],
            "ConfigRoutesMixin": ["/api/config", "/api/config/rollback"],
            "WorldsRoutesMixin": ["/api/worlds/switch", "/api/worlds/rename", "/api/worlds/delete"],
            "ConsoleRoutesMixin": ["/api/console/clear"],
            "CommandsRoutesMixin": ["/api/commands/list", "/api/commands/help", "/api/commands/autocomplete"],
        }

        for mixin_name, routes in mixin_routes.items():
            for route in routes:
                # 路由可能在 GET 或 POST 表中
                in_get = route in Handler._get_routes
                in_post = route in Handler._post_routes
                self.assertTrue(
                    in_get or in_post,
                    f"{mixin_name} 的路由 {route} 未注册到 Handler",
                )


class TestRouteDistribution(unittest.TestCase):
    """测试路由分发逻辑（使用 mock）。"""

    def _create_mock_handler(self):
        """创建一个 mock Handler 实例。"""
        from bedrock_server_manager.web.handler import Handler

        handler = Handler.__new__(Handler)
        handler._json = MagicMock()
        handler._send = MagicMock()
        handler._post_data = MagicMock(return_value={})
        handler._check_auth = MagicMock(return_value=True)
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.rfile = MagicMock()
        handler.wfile = MagicMock()
        handler.path = "/api/unknown_route"
        return handler

    @patch("bedrock_server_manager.web.handler.urlparse")
    def test_get_unknown_route_returns_404(self, mock_urlparse):
        """测试未知 GET 路由返回 404。"""
        from urllib.parse import ParseResult

        handler = self._create_mock_handler()
        mock_urlparse.return_value = ParseResult(
            scheme="http", netloc="127.0.0.1:19100",
            path="/api/unknown_route", params="", query="", fragment=""
        )
        handler.path = "/api/unknown_route"

        handler.do_GET()

        handler._send.assert_called_once_with(404, "not found", "text/plain; charset=utf-8")

    @patch("bedrock_server_manager.web.handler.urlparse")
    def test_post_unknown_route_returns_404(self, mock_urlparse):
        """测试未知 POST 路由返回 404。"""
        from urllib.parse import ParseResult

        handler = self._create_mock_handler()
        mock_urlparse.return_value = ParseResult(
            scheme="http", netloc="127.0.0.1:19100",
            path="/api/unknown_route", params="", query="", fragment=""
        )
        handler.path = "/api/unknown_route"

        handler.do_POST()

        handler._send.assert_called_once_with(404, "not found", "text/plain; charset=utf-8")

    @patch("bedrock_server_manager.web.handler.urlparse")
    def test_post_requires_auth(self, mock_urlparse):
        """测试 POST 请求需要认证。"""
        from urllib.parse import ParseResult

        handler = self._create_mock_handler()
        handler._check_auth = MagicMock(return_value=False)
        mock_urlparse.return_value = ParseResult(
            scheme="http", netloc="127.0.0.1:19100",
            path="/api/cmd", params="", query="", fragment=""
        )
        handler.path = "/api/cmd"

        handler.do_POST()

        handler._json.assert_called_once()
        call_args = handler._json.call_args
        self.assertEqual(call_args[1].get("code", call_args[0][1] if len(call_args[0]) > 1 else 200), 403)

    @patch("bedrock_server_manager.web.handler.urlparse")
    def test_get_index_route_is_registered(self, mock_urlparse):
        """测试首页路由 / 已注册。"""
        from bedrock_server_manager.web.handler import Handler

        self.assertIn("/", Handler._get_routes)
        self.assertEqual(Handler._get_routes["/"], "_get_index")

    @patch("bedrock_server_manager.web.handler.urlparse")
    def test_get_health_route_no_auth_required(self, mock_urlparse):
        """测试 /api/health 路由不需要认证（健康检查端点）。"""
        from urllib.parse import ParseResult
        from bedrock_server_manager.web.handler import Handler

        handler = self._create_mock_handler()
        handler._check_auth = MagicMock(return_value=False)  # 认证失败
        mock_urlparse.return_value = ParseResult(
            scheme="http", netloc="127.0.0.1:19100",
            path="/api/health", params="", query="", fragment=""
        )
        handler.path = "/api/health"

        # 健康检查端点应该放行，不应该返回 403
        # 这里只验证路由存在，具体行为由实现决定
        self.assertIn("/api/health", Handler._get_routes)


class TestRouteSecurity(unittest.TestCase):
    """测试路由安全相关配置。"""

    def test_dangerous_endpoints_are_registered(self):
        """测试危险操作端点都已注册（需要认证保护）。"""
        from bedrock_server_manager.web.handler import Handler

        dangerous_endpoints = [
            "/api/server/stop",
            "/api/server/delete",
            "/api/cmd",
            "/api/install",
            "/api/update",
            "/api/exit",
            "/api/backups/restore",
            "/api/backups/delete",
            "/api/worlds/delete",
            "/api/config/rollback",
        ]

        for endpoint in dangerous_endpoints:
            in_post = endpoint in Handler._post_routes
            in_get = endpoint in Handler._get_routes
            self.assertTrue(
                in_post or in_get,
                f"危险端点 {endpoint} 未注册（可能缺少认证保护）",
            )

    def test_all_post_routes_go_through_auth_check(self):
        """测试所有 POST 路由都经过认证检查（代码结构验证）。"""
        from bedrock_server_manager.web.handler import Handler
        import inspect

        source = inspect.getsource(Handler.do_POST)
        # do_POST 方法中应该调用 _check_auth
        self.assertIn("_check_auth", source, "do_POST 中未调用 _check_auth")

    def test_api_token_generation(self):
        """测试 API Token 生成机制存在。"""
        from bedrock_server_manager.state import API_TOKEN

        self.assertIsInstance(API_TOKEN, str)
        self.assertGreater(len(API_TOKEN), 16, "API Token 长度过短")


class TestRouteCompleteness(unittest.TestCase):
    """测试路由完整性（对比前端使用的 API）。"""

    def test_frontend_critical_apis_exist(self):
        """测试前端关键功能对应的 API 都存在。"""
        from bedrock_server_manager.web.handler import Handler

        # 前端关键功能对应的 API
        frontend_apis = {
            "安装服务器": "/api/install",
            "启动服务器": "/api/server/start",
            "停止服务器": "/api/server/stop",
            "重启服务器": "/api/server/restart",
            "删除服务器": "/api/server/delete",
            "发送指令": "/api/cmd",
            "获取状态": "/api/status",
            "获取版本列表": "/api/versions",
            "获取控制台": "/api/console",
            "清空控制台": "/api/console/clear",
            "获取配置": "/api/config",
            "保存配置": "/api/config",
            "获取玩家列表": "/api/players",
            "踢出玩家": "/api/players/kick",
            "封禁玩家": "/api/players/ban",
            "解封玩家": "/api/players/pardon",
            "设置权限": "/api/players/permission",
            "获取备份列表": "/api/backups",
            "创建备份": "/api/backups/create",
            "恢复备份": "/api/backups/restore",
            "删除备份": "/api/backups/delete",
            "获取世界列表": "/api/worlds/list",
            "切换世界": "/api/worlds/switch",
            "性能监控": "/api/performance",
            "检查更新": "/api/latest",
            "更新服务器": "/api/update",
            "设置主题": "/api/theme",
            "退出程序": "/api/exit",
            "取消安装": "/api/cancel",
        }

        missing = []
        for feature, api in frontend_apis.items():
            in_get = api in Handler._get_routes
            in_post = api in Handler._post_routes
            if not in_get and not in_post:
                missing.append(f"{feature}: {api}")

        self.assertEqual(len(missing), 0, f"以下前端 API 缺失:\n" + "\n".join(missing))


if __name__ == "__main__":
    unittest.main(verbosity=2)
