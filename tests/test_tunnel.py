"""外部穿透客户端测试：地址解析、命令构建、配置校验、进程启停。"""

import sys
import time
from unittest import TestCase, main, mock

from bedrock_server_manager import tunnel


class TestAddressParsing(TestCase):
    def test_playit(self):
        lines = ["[INFO] tunnel ready", "Address: demo.playit.gg:25565"]
        self.assertEqual(tunnel.detect_address(lines, "playit"), "demo.playit.gg:25565")

    def test_playit_at_subdomain(self):
        self.assertEqual(tunnel.detect_address(["demo.at.playit.gg"], "playit"), "demo.at.playit.gg")

    def test_ngrok_tcp(self):
        line = "Forwarding  tcp://6.tcp.ngrok.io:12345 -> localhost:19132"
        self.assertEqual(tunnel.detect_address([line], "ngrok"), "6.tcp.ngrok.io:12345")

    def test_ngrok_http_url(self):
        line = "Forwarding  https://abcd-1-2-3-4.ngrok-free.app -> http://localhost:19100"
        self.assertEqual(tunnel.detect_address([line], "ngrok"), "abcd-1-2-3-4.ngrok-free.app")

    def test_frp_ip_port(self):
        self.assertEqual(tunnel.detect_address(["start proxy success 203.0.113.7:19132"], "frp"), "203.0.113.7:19132")

    def test_custom_regex(self):
        line = "public endpoint => myhost.example.com:19132"
        pattern = r"endpoint => (?P<host>[^\s:]+):(?P<port>\d+)"
        self.assertEqual(tunnel.detect_address([line], "custom", pattern), "myhost.example.com:19132")

    def test_no_match(self):
        self.assertEqual(tunnel.detect_address(["starting..."], "playit"), "")

    def test_latest_line_wins(self):
        lines = ["old.playit.gg:1000", "new.playit.gg:2000"]
        self.assertEqual(tunnel.detect_address(lines, "playit"), "new.playit.gg:2000")

    def test_bad_custom_regex_is_ignored(self):
        self.assertEqual(tunnel.detect_address(["x"], "custom", "([unclosed"), "")


class TestCommandBuild(TestCase):
    def test_port_placeholder(self):
        argv = tunnel.build_command(f'"{sys.executable}" -u -c "print(1)" {{port}}', 19132)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[-1], "19132")

    def test_empty_command(self):
        with self.assertRaises(tunnel.TunnelError):
            tunnel.build_command("   ", 19132)

    def test_bad_quotes(self):
        with self.assertRaises(tunnel.TunnelError):
            tunnel.build_command('playit "unclosed', 19132)

    def test_missing_binary(self):
        with self.assertRaises(tunnel.TunnelError):
            tunnel.build_command("definitely-not-a-binary-xyz", 19132)


class TestConfig(TestCase):
    def setUp(self):
        from bedrock_server_manager.app_context import AppContext

        self.settings = AppContext.instance().settings
        self._saved = dict(self.settings)
        self.settings.pop("tunnel_config", None)

    def tearDown(self):
        self.settings.clear()
        self.settings.update(self._saved)

    def test_defaults(self):
        cfg = tunnel.config()
        self.assertEqual(cfg["provider"], "playit")
        self.assertFalse(cfg["enabled"])

    def test_save_and_read(self):
        with mock.patch("bedrock_server_manager.utils.save_settings", return_value=True):
            cfg = tunnel.save_config("ngrok", "ngrok tcp {port}", enabled=True)
        self.assertEqual(cfg["provider"], "ngrok")
        self.assertEqual(cfg["command"], "ngrok tcp {port}")
        self.assertTrue(cfg["enabled"])

    def test_unknown_provider(self):
        with self.assertRaises(tunnel.TunnelError):
            tunnel.save_config("nope", "cmd")

    def test_address_regex_saved_and_used(self):
        with mock.patch("bedrock_server_manager.utils.save_settings", return_value=True):
            cfg = tunnel.save_config("custom", "mybin", address_regex=r"public (?P<host>[^\s]+)")
        self.assertEqual(cfg["address_regex"], r"public (?P<host>[^\s]+)")
        self.assertEqual(tunnel.detect_address(["public example.com"], "custom", cfg["address_regex"]), "example.com")

    def test_bad_command(self):
        with self.assertRaises(tunnel.TunnelError):
            tunnel.save_config("custom", 'bad "quote')


class TestProcessLifecycle(TestCase):
    """用真实 Python 子进程验证启动、输出解析与停止。"""

    def setUp(self):
        tunnel.manager.stop()

    def tearDown(self):
        tunnel.manager.stop()

    def test_start_parses_address_and_stops(self):
        script = "import sys, time;print('Address: demo.playit.gg:25565', flush=True);time.sleep(30)"
        cmd = f'"{sys.executable}" -u -c "{script}"'
        status = tunnel.manager.start(19132, provider="playit", command=cmd)
        self.assertTrue(status["running"])
        deadline = time.time() + 10
        while time.time() < deadline and not tunnel.manager.status()["address"]:
            time.sleep(0.1)
        self.assertEqual(tunnel.manager.status()["address"], "demo.playit.gg:25565")
        self.assertTrue(any("demo.playit.gg" in line for line in tunnel.manager.status()["lines"]))
        stopped = tunnel.manager.stop()
        self.assertFalse(stopped["running"])

    def test_double_start_rejected(self):
        script = "import time; print('hello', flush=True); time.sleep(30)"
        cmd = f'"{sys.executable}" -u -c "{script}"'
        tunnel.manager.start(19132, provider="playit", command=cmd)
        try:
            with self.assertRaises(tunnel.TunnelError):
                tunnel.manager.start(19132, provider="playit", command=cmd)
        finally:
            tunnel.manager.stop()

    def test_start_without_command(self):
        with self.assertRaises(tunnel.TunnelError):
            tunnel.manager.start(19132, provider="playit", command="")


if __name__ == "__main__":
    main(verbosity=2)
