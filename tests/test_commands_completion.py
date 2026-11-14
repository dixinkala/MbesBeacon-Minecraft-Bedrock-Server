"""B3/B4/B7 回归测试：命令补全、危险判定一致性、/index.html 路由。"""

import unittest


class TestCommandSuggestions(unittest.TestCase):
    """B4：玩家名补全的 off-by-one 修复"""

    def test_player_completion_after_command(self):
        """输入 "kick St" 应补全在线玩家（原实现查错参数位，永远返回空）"""
        from bedrock_server_manager.commands import get_command_suggestions

        r = get_command_suggestions("kick St", online_players=["Steve", "Alex"])
        names = [s["name"] for s in r["suggestions"]]
        self.assertEqual(names, ["Steve"], f"实际: {names}")

    def test_player_completion_no_prefix(self):
        """只输入命令名时列出全部在线玩家"""
        from bedrock_server_manager.commands import get_command_suggestions

        r = get_command_suggestions("kick", online_players=["Steve", "Alex"])
        names = sorted(s["name"] for s in r["suggestions"])
        self.assertEqual(names, ["Alex", "Steve"])

    def test_ban_alex_prefix(self):
        from bedrock_server_manager.commands import get_command_suggestions

        r = get_command_suggestions("ban Al", online_players=["Steve", "Alex"])
        self.assertEqual([s["name"] for s in r["suggestions"]], ["Alex"])

    def test_case_insensitive_prefix(self):
        from bedrock_server_manager.commands import get_command_suggestions

        r = get_command_suggestions("kick st", online_players=["Steve", "Alex"])
        self.assertEqual([s["name"] for s in r["suggestions"]], ["Steve"])

    def test_slash_prefix_tolerated(self):
        """玩家习惯输入 /kick，服务端应能识别"""
        from bedrock_server_manager.commands import get_command_suggestions

        r = get_command_suggestions("/kick St", online_players=["Steve"])
        self.assertEqual([s["name"] for s in r["suggestions"]], ["Steve"])

    def test_command_name_completion(self):
        from bedrock_server_manager.commands import get_command_suggestions

        r = get_command_suggestions("whi")
        names = [s["name"] for s in r["suggestions"]]
        self.assertIn("whitelist", names)

    def test_non_player_arg_no_player_suggestions(self):
        """gamemode 第一个参数是模式，不应给出玩家名建议"""
        from bedrock_server_manager.commands import get_command_suggestions

        r = get_command_suggestions("gamemode cr", online_players=["Steve"])
        self.assertEqual(r["suggestions"], [])


class TestDangerousConsistency(unittest.TestCase):
    """B4：commands 与 security 的危险判定必须一致（含多词命令）"""

    def test_multi_word_dangerous_now_detected(self):
        from bedrock_server_manager.commands import is_dangerous_command

        for cmd in ("whitelist remove Steve", "allowlist remove Steve"):
            self.assertTrue(is_dangerous_command(cmd), f"{cmd} 应判定为危险")

    def test_single_dangerous(self):
        from bedrock_server_manager.commands import is_dangerous_command

        for cmd in ("stop", "restart", "kill @e", "ban Steve", "deop Steve"):
            self.assertTrue(is_dangerous_command(cmd), f"{cmd} 应判定为危险")

    def test_safe_not_dangerous(self):
        from bedrock_server_manager.commands import is_dangerous_command

        for cmd in ("list", "say hi", "whitelist list", "allowlist on", "gamemode creative"):
            self.assertFalse(is_dangerous_command(cmd), f"{cmd} 不应判定为危险")

    def test_slash_prefix_consistent(self):
        from bedrock_server_manager.commands import is_dangerous_command
        from bedrock_server_manager.security import validate_command

        for cmd in ("/stop", "/whitelist remove Steve"):
            self.assertTrue(is_dangerous_command(cmd), cmd)
            _ok, _err, dangerous = validate_command(cmd)
            self.assertTrue(dangerous, f"security 未将 {cmd} 判定为危险")

    def test_matches_security_for_all_listed(self):
        """对统一清单里所有命令，两套判定结论一致"""
        from bedrock_server_manager.commands import is_dangerous_command
        from bedrock_server_manager.constants import DANGEROUS_SINGLE_COMMANDS
        from bedrock_server_manager.security import validate_command

        for name in DANGEROUS_SINGLE_COMMANDS:
            _ok, _err, sec_dangerous = validate_command(name)
            self.assertEqual(is_dangerous_command(name), sec_dangerous, f"{name} 判定不一致")


class TestIndexHtmlRoute(unittest.TestCase):
    """B7：/index.html 应有真实路由并免认证"""

    def test_route_registered(self):
        from bedrock_server_manager.web.handler import Handler

        self.assertIn("/", Handler._get_routes)
        self.assertIn("/index.html", Handler._get_routes)

    def test_both_routes_render_same_page(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from bedrock_server_manager.web.handler import Handler

        for path in ("/", "/index.html"):
            h = Handler.__new__(Handler)
            h._send = MagicMock()
            h._json = MagicMock()
            h.headers = {"Host": "127.0.0.1:19100"}
            h.client_address = ("127.0.0.1", 1234)
            h.rfile = MagicMock()
            h.wfile = MagicMock()
            h.server = SimpleNamespace(server_port=19100)
            h.path = path
            Handler._get_routes[path]
            getattr(h, Handler._get_routes[path])({})
            args = h._send.call_args[0]
            self.assertEqual(args[0], 200, path)
            self.assertIn("MbesBeacon", args[1], path)
            self.assertNotIn("__API_TOKEN__", args[1], f"{path} 未替换 token 占位符")


class TestGetRateLimit(unittest.TestCase):
    """A5：GET 端点也有限流，且与 POST 配额独立"""

    def test_get_has_default_quota(self):
        from bedrock_server_manager.ratelimit import RateLimiter

        rl = RateLimiter()
        # GET 默认 30/秒，应远高于 POST 默认 5/秒
        allowed = sum(1 for _ in range(20) if rl.allow("/api/status", method="GET")[0])
        self.assertGreater(allowed, 5)

    def test_get_and_post_buckets_independent(self):
        from bedrock_server_manager.ratelimit import RateLimiter

        rl = RateLimiter()
        # 打满 POST 配额不应影响同路径 GET
        for _ in range(10):
            rl.allow("/api/config", method="POST")
        self.assertTrue(rl.allow("/api/config", method="GET")[0])

    def test_heavy_get_endpoints_throttled(self):
        """导出世界/阻塞式玩家探测有严格配额"""
        from bedrock_server_manager.ratelimit import RateLimiter

        for ep, expected in (("/api/worlds/export", 3), ("/api/players", 5)):
            rl = RateLimiter()
            n = 0
            for _ in range(20):
                if rl.allow(ep, method="GET")[0]:
                    n += 1
                else:
                    break
            self.assertEqual(n, expected, ep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
