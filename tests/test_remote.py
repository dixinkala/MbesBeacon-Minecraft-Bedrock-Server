"""远程管理测试：密码哈希、会话、登录限速、Host 白名单与认证分支。"""

import pathlib
import time
import unittest
from unittest.mock import patch

from bedrock_server_manager import remote
from bedrock_server_manager.web.handler import Handler

REPO = pathlib.Path(__file__).resolve().parent.parent


class FakeServer:
    server_port = 19100


class FakeHandler(Handler):
    """不触碰 socket 的 Handler 桩，只用于测试纯逻辑方法。"""

    def __init__(self, headers=None, client_ip="127.0.0.1", server_port=19100):
        self.headers = headers or {}
        self.client_address = (client_ip, 12345)
        self.server = type("S", (), {"server_port": server_port})()


class TestPassword(unittest.TestCase):
    def test_hash_and_verify(self):
        stored = remote.hash_password("s3cret-pass")
        self.assertNotIn("s3cret-pass", stored)
        self.assertTrue(remote.verify_password("s3cret-pass", stored))
        self.assertFalse(remote.verify_password("wrong", stored))

    def test_malformed_hash_is_rejected(self):
        for bad in ("", "plain", "md5$1$a$b", "pbkdf2_sha256$x$y$z", "pbkdf2_sha256$1$@@$@@"):
            self.assertFalse(remote.verify_password("whatever", bad), bad)

    def test_password_policy(self):
        self.assertFalse(remote.password_ok("short"))
        self.assertFalse(remote.password_ok("aaaaaaaa"))
        self.assertTrue(remote.password_ok("abcd1234"))

    def test_salt_is_random(self):
        self.assertNotEqual(remote.hash_password("same-password"), remote.hash_password("same-password"))


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.store = remote.SessionStore(ttl=10, max_sessions=2)

    def test_create_and_validate(self):
        token = self.store.create("1.2.3.4")
        self.assertTrue(self.store.validate(token))
        self.assertFalse(self.store.validate("nope"))

    def test_expiry(self):
        token = self.store.create()
        with patch.object(time, "time", return_value=time.time() + 11):
            self.assertFalse(self.store.validate(token))

    def test_revoke(self):
        token = self.store.create()
        self.store.revoke(token)
        self.assertFalse(self.store.validate(token))

    def test_max_sessions_evicts_oldest(self):
        first = self.store.create()
        self.store.create()
        self.store.create()
        self.assertEqual(self.store.count(), 2)
        self.assertFalse(self.store.validate(first))


class TestLoginLimiter(unittest.TestCase):
    def test_locks_after_failures(self):
        limiter = remote.LoginLimiter(window=60, max_failures=3, lock=30)
        for _ in range(3):
            limiter.record_failure("9.9.9.9")
        self.assertGreater(limiter.retry_after("9.9.9.9"), 0)
        self.assertEqual(limiter.retry_after("8.8.8.8"), 0)

    def test_success_clears(self):
        limiter = remote.LoginLimiter(window=60, max_failures=2, lock=30)
        limiter.record_failure("9.9.9.9")
        limiter.record_success("9.9.9.9")
        limiter.record_failure("9.9.9.9")
        self.assertEqual(limiter.retry_after("9.9.9.9"), 0)


class TestCookies(unittest.TestCase):
    def test_parse_cookie(self):
        self.assertEqual(remote.parse_cookie("a=1; mbes_session=abc; b=2"), "abc")
        self.assertEqual(remote.parse_cookie(""), "")

    def test_cookie_flags(self):
        value = remote.session_cookie("tok")
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, value)
        self.assertIn("Max-Age=0", remote.clear_cookie())


class TestHostAllowlist(unittest.TestCase):
    def test_loopback_always_allowed(self):
        with patch.object(remote, "enabled", return_value=False):
            names = remote.allowed_hostnames()
        self.assertLessEqual({"127.0.0.1", "localhost", "::1"}, names)

    def test_lan_ip_allowed_only_when_enabled(self):
        with (
            patch.object(remote, "enabled", return_value=False),
            patch.object(remote, "local_ipv4_addresses", return_value={"127.0.0.1", "192.168.1.10"}),
        ):
            self.assertNotIn("192.168.1.10", remote.allowed_hostnames())
        with (
            patch.object(remote, "enabled", return_value=True),
            patch.object(remote, "local_ipv4_addresses", return_value={"127.0.0.1", "192.168.1.10"}),
        ):
            self.assertIn("192.168.1.10", remote.allowed_hostnames())

    def test_extra_hosts(self):
        with (
            patch.object(remote, "enabled", return_value=True),
            patch.object(remote, "local_ipv4_addresses", return_value={"127.0.0.1"}),
            patch.object(remote, "extra_hosts", return_value={"myhost.example.com"}),
        ):
            self.assertIn("myhost.example.com", remote.allowed_hostnames())

    def test_unknown_host_rejected_by_handler(self):
        handler = FakeHandler({"Host": "evil.example.com:19100"})
        with patch.object(remote, "enabled", return_value=False):
            self.assertFalse(handler._check_host())

    def test_lan_host_accepted_when_enabled(self):
        handler = FakeHandler({"Host": "192.168.1.10:19100"})
        with (
            patch.object(remote, "enabled", return_value=True),
            patch.object(remote, "local_ipv4_addresses", return_value={"127.0.0.1", "192.168.1.10"}),
        ):
            self.assertTrue(handler._check_host())

    def test_port_must_match(self):
        handler = FakeHandler({"Host": "127.0.0.1:1234"})
        self.assertFalse(handler._check_host())


class TestHandlerAuth(unittest.TestCase):
    def setUp(self):
        from bedrock_server_manager.app_context import AppContext

        AppContext.instance().api_token = "test-token"
        remote.sessions.clear()

    def test_token_accepted(self):
        handler = FakeHandler({"X-API-Token": "test-token"})
        self.assertTrue(handler._check_auth())

    def test_wrong_token_and_no_origin_rejected(self):
        handler = FakeHandler({"X-API-Token": "bad"})
        with patch.object(remote, "enabled", return_value=False):
            self.assertFalse(handler._check_auth())

    def test_session_cookie_accepted_when_remote_enabled(self):
        token = remote.sessions.create("192.168.1.20")
        handler = FakeHandler({"Cookie": f"mbes_session={token}"}, client_ip="192.168.1.20")
        with patch.object(remote, "enabled", return_value=True):
            self.assertTrue(handler._check_auth())

    def test_session_rejected_when_origin_mismatch(self):
        token = remote.sessions.create("192.168.1.20")
        handler = FakeHandler(
            {
                "Cookie": f"mbes_session={token}",
                "Origin": "http://evil.example.com",
                "Host": "192.168.1.10:19100",
            },
            client_ip="192.168.1.20",
        )
        with patch.object(remote, "enabled", return_value=True):
            self.assertFalse(handler._check_auth())

    def test_session_ignored_when_remote_disabled(self):
        token = remote.sessions.create("192.168.1.20")
        handler = FakeHandler({"Cookie": f"mbes_session={token}"}, client_ip="192.168.1.20")
        with patch.object(remote, "enabled", return_value=False):
            self.assertFalse(handler._check_auth())


class TestConfig(unittest.TestCase):
    def setUp(self):
        from bedrock_server_manager.app_context import AppContext

        self.settings = AppContext.instance().settings
        self._saved = dict(self.settings)
        self.settings.pop("remote_enabled", None)
        self.settings.pop("remote_password_hash", None)
        self.settings.pop("remote_hosts", None)

    def tearDown(self):
        self.settings.clear()
        self.settings.update(self._saved)

    def test_enable_requires_password(self):
        with self.assertRaises(ValueError):
            remote.set_config(True)

    def test_enable_with_password(self):
        with patch("bedrock_server_manager.utils.save_settings", return_value=True):
            result = remote.set_config(True, password="abcd1234")
        self.assertTrue(result["enabled"])
        self.assertTrue(remote.enabled())
        self.assertTrue(remote.configured())

    def test_weak_password_rejected(self):
        with self.assertRaises(ValueError):
            remote.set_config(True, password="123")

    def test_disable(self):
        with patch("bedrock_server_manager.utils.save_settings", return_value=True):
            remote.set_config(True, password="abcd1234")
            result = remote.set_config(False)
        self.assertFalse(result["enabled"])
        self.assertFalse(remote.enabled())


if __name__ == "__main__":
    unittest.main(verbosity=2)
